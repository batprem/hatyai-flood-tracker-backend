"""Citizen flood-report endpoints: submission, public read, photo, moderation.

Public surface (HFT-73 / consumed by HFT-74):

- ``POST /api/reports`` — submit a report (multipart form *or* JSON body),
  optionally with a photo. Rate-limited per IP. Returns ``pending``.
- ``GET /api/reports`` — list approved reports only (newest first, capped).
- ``GET /api/reports/{id}/photo`` — stream an *approved* report's photo.

Moderation surface (bearer-token protected, mirroring ALERTS_TEST_TOKEN):

- ``GET /api/reports/moderation/pending`` — list pending reports.
- ``GET /api/reports/moderation/{id}/photo`` — stream any report's photo.
- ``POST /api/reports/moderation/{id}/approve`` — approve a report.
- ``POST /api/reports/moderation/{id}/reject`` — reject a report.

Privacy: no name/phone/identifier is accepted; submitter IP is hashed (salted)
only to key the rate limiter and is never written to a report document. Photos
are re-encoded to strip all EXIF/GPS metadata before storage.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.api.deps import get_app_settings, get_photo_storage, get_report_repository
from app.core.config import Settings
from app.ingestion.report_repository import DEFAULT_APPROVED_LIMIT, ReportRepository
from app.schemas.reports import (
    CitizenReport,
    ReportListResponse,
    ReportStatus,
    ReportSubmission,
    ReportSubmissionResponse,
)
from app.services.photo_storage import PhotoNotFoundError, PhotoStorage
from app.services.report_location import is_within_basin
from app.services.report_photos import PhotoValidationError, sanitize_photo

router = APIRouter(prefix="/reports", tags=["reports"])

# Cap a list request so a malicious ``limit`` cannot force a huge scan.
_MAX_LIST_LIMIT = 200


def _client_ip(request: Request) -> str:
    """Return the best-effort client IP for transient rate limiting.

    Honors a single ``X-Forwarded-For`` hop (Railway terminates TLS at a proxy)
    and falls back to the socket peer. The value is only ever hashed for the
    rate-limit key and is never persisted.

    Args:
        request: The incoming request.

    Returns:
        The client IP string, or ``"unknown"`` when it cannot be determined.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _hash_ip(ip: str, salt: str) -> str:
    """Return a salted SHA-256 hex digest of an IP for the rate-limit key.

    Hashing means the persisted rate-limit document cannot be reversed to the
    raw IP, satisfying the PDPA data-minimization requirement.

    Args:
        ip: Raw client IP (used only here, never stored).
        salt: Deployment salt mixed into the digest.

    Returns:
        The hex SHA-256 digest of ``salt + ip``.
    """
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def _with_photo_url(report: CitizenReport, *, moderation: bool = False) -> CitizenReport:
    """Return a copy of the report with ``photo_url`` resolved when a photo exists.

    Args:
        report: The stored report.
        moderation: When ``True``, point at the moderation photo path so a
            moderator can view pending photos; otherwise the public path.

    Returns:
        The report with ``photo_url`` populated when ``has_photo`` is set.
    """
    if not report.has_photo:
        return report
    if moderation:
        url = f"/api/reports/moderation/{report.id}/photo"
    else:
        url = f"/api/reports/{report.id}/photo"
    return report.model_copy(update={"photo_url": url})


def _require_moderation_token(authorization: str | None, settings: Settings) -> None:
    """Authorize a moderation request using the configured bearer token.

    Mirrors the alerts-test-token pattern: a request is authorized only when
    ``REPORTS_MODERATION_TOKEN`` is configured and the
    ``Authorization: Bearer <token>`` header matches it via constant-time
    comparison. An unset token rejects every request.

    Args:
        authorization: Raw ``Authorization`` header value, or ``None``.
        settings: Application settings carrying ``reports_moderation_token``.

    Raises:
        HTTPException: 403 when the token is unset, missing, or mismatched.
    """
    configured = settings.reports_moderation_token
    presented = ""
    if authorization is not None and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :].strip()
    if not configured or not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing reports moderation token.",
        )


@router.post(
    "",
    response_model=ReportSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_report(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    storage: Annotated[PhotoStorage, Depends(get_photo_storage)],
    longitude: Annotated[float | None, Form()] = None,
    latitude: Annotated[float | None, Form()] = None,
    water_depth: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    photo: UploadFile | None = None,
) -> ReportSubmissionResponse:
    """Submit a citizen flood report, optionally with a photo.

    Accepts either a ``multipart/form-data`` body (the fields below, plus an
    optional ``photo`` file) or a JSON body matching :class:`ReportSubmission`.
    The location must fall inside or near the U-Tapao basin. Any photo is
    re-encoded to strip EXIF/GPS metadata before storage. Submissions are
    rate-limited per IP; the new report starts in ``pending`` and is not
    publicly visible until approved.

    Args:
        request: The incoming request, used for the JSON body and client IP.
        settings: Application settings injected via dependency.
        repository: Citizen-report repository injected via dependency.
        storage: Report photo storage backend injected via dependency.
        longitude: Multipart longitude field. Defaults to ``None``.
        latitude: Multipart latitude field. Defaults to ``None``.
        water_depth: Multipart water-depth category field. Defaults to ``None``.
        note: Multipart optional note field. Defaults to ``None``.
        photo: Optional uploaded photo file. Defaults to ``None``.

    Returns:
        The new report's id, ``pending`` status, photo flag, and timestamp.

    Raises:
        HTTPException: 422 when the body is invalid, 400 when the location is
            outside the basin or the photo is invalid, 429 when the per-IP rate
            limit is exceeded.
    """
    submission = await _resolve_submission(
        request,
        longitude=longitude,
        latitude=latitude,
        water_depth=water_depth,
        note=note,
    )

    if not is_within_basin(submission.longitude, submission.latitude):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report location is outside the U-Tapao basin coverage area.",
        )

    now = datetime.now(UTC)
    ip_hash = _hash_ip(_client_ip(request), settings.reports_ip_hash_salt)
    allowed = await repository.register_submission(
        ip_hash, now=now, max_per_window=settings.reports_rate_limit_per_hour
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many reports submitted from this address. Try again later.",
        )

    photo_key: str | None = None
    if photo is not None:
        raw = await photo.read()
        if raw:
            try:
                sanitized = await sanitize_photo(raw, content_type=photo.content_type)
            except PhotoValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            photo_key = await storage.save(data=sanitized, content_type="image/jpeg")

    report = await repository.create_report(
        longitude=submission.longitude,
        latitude=submission.latitude,
        water_depth=submission.water_depth,
        note=submission.note,
        photo_key=photo_key,
        created_at=now,
    )
    return ReportSubmissionResponse(
        id=report.id,
        status=report.status,
        has_photo=report.has_photo,
        created_at=report.created_at,
    )


async def _resolve_submission(
    request: Request,
    *,
    longitude: float | None,
    latitude: float | None,
    water_depth: str | None,
    note: str | None,
) -> ReportSubmission:
    """Build a validated :class:`ReportSubmission` from multipart or JSON.

    Multipart fields take precedence when present; otherwise the JSON body is
    parsed. Either path is validated through the Pydantic model so field caps
    and the water-depth enum are enforced consistently.

    Args:
        request: The incoming request, used to read a JSON body when needed.
        longitude: Multipart longitude field, or ``None``.
        latitude: Multipart latitude field, or ``None``.
        water_depth: Multipart water-depth field, or ``None``.
        note: Multipart note field, or ``None``.

    Returns:
        The validated submission.

    Raises:
        HTTPException: 422 when required fields are missing or invalid.
    """
    if longitude is not None and latitude is not None and water_depth is not None:
        payload: dict[str, object] = {
            "longitude": longitude,
            "latitude": latitude,
            "water_depth": water_depth,
            "note": note,
        }
    else:
        try:
            body = await request.json()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide report fields as multipart form data or a JSON body.",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="JSON body must be an object with report fields.",
            )
        payload = body

    try:
        return ReportSubmission.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors()
        ) from exc


@router.get("", response_model=ReportListResponse)
async def list_reports(
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    limit: int = DEFAULT_APPROVED_LIMIT,
) -> ReportListResponse:
    """Return the most recent approved citizen reports, newest first.

    Only approved reports are ever returned; pending and rejected reports are
    never exposed publicly. Each report carries a relative ``photo_url`` when a
    photo is attached.

    Args:
        repository: Citizen-report repository injected via dependency.
        limit: Maximum reports to return. Clamped to a safe ceiling. Defaults to
            the repository default.

    Returns:
        The approved reports with resolved photo URLs and a count.
    """
    safe_limit = max(1, min(limit, _MAX_LIST_LIMIT))
    reports = await repository.list_approved(limit=safe_limit)
    resolved = [_with_photo_url(report) for report in reports]
    return ReportListResponse(reports=resolved, count=len(resolved))


@router.get("/{report_id}/photo")
async def get_report_photo(
    report_id: str,
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    storage: Annotated[PhotoStorage, Depends(get_photo_storage)],
) -> Response:
    """Stream the photo for an approved report.

    Pending and rejected reports never expose their photo on this public path;
    moderators use the moderation photo path instead.

    Args:
        report_id: Opaque report identifier.
        repository: Citizen-report repository injected via dependency.
        storage: Report photo storage backend injected via dependency.

    Returns:
        The photo bytes as a streaming response.

    Raises:
        HTTPException: 404 when the report is not approved, has no photo, or the
            photo is missing from storage.
    """
    report = await repository.get_report(report_id)
    if report is None or report.status is not ReportStatus.APPROVED or not report.has_photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found.")
    return await _stream_photo(repository, storage, report_id)


@router.get("/moderation/pending", response_model=ReportListResponse)
async def list_pending_reports(
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    authorization: Annotated[str | None, Header()] = None,
    limit: int = DEFAULT_APPROVED_LIMIT,
) -> ReportListResponse:
    """Return pending reports for moderation (bearer-token protected).

    Args:
        settings: Application settings injected via dependency.
        repository: Citizen-report repository injected via dependency.
        authorization: ``Authorization`` header carrying the bearer token.
            Defaults to ``None``.
        limit: Maximum reports to return. Clamped to a safe ceiling.

    Returns:
        The pending reports with moderation photo URLs and a count. Responds 403
        when the moderation token is unset or mismatched.
    """
    _require_moderation_token(authorization, settings)
    safe_limit = max(1, min(limit, _MAX_LIST_LIMIT))
    reports = await repository.list_pending(limit=safe_limit)
    resolved = [_with_photo_url(report, moderation=True) for report in reports]
    return ReportListResponse(reports=resolved, count=len(resolved))


@router.get("/moderation/{report_id}/photo")
async def get_moderation_photo(
    report_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    storage: Annotated[PhotoStorage, Depends(get_photo_storage)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Stream any report's photo for moderation review (token protected).

    Args:
        report_id: Opaque report identifier.
        settings: Application settings injected via dependency.
        repository: Citizen-report repository injected via dependency.
        storage: Report photo storage backend injected via dependency.
        authorization: ``Authorization`` header carrying the bearer token.
            Defaults to ``None``.

    Returns:
        The photo bytes as a streaming response.

    Raises:
        HTTPException: 403 when the token is invalid; 404 when the report has no
            photo or the photo is missing from storage.
    """
    _require_moderation_token(authorization, settings)
    report = await repository.get_report(report_id)
    if report is None or not report.has_photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found.")
    return await _stream_photo(repository, storage, report_id)


@router.post("/moderation/{report_id}/approve", response_model=CitizenReport)
async def approve_report(
    report_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> CitizenReport:
    """Approve a pending report so it becomes publicly visible (token protected).

    Args:
        report_id: Opaque report identifier.
        settings: Application settings injected via dependency.
        repository: Citizen-report repository injected via dependency.
        authorization: ``Authorization`` header carrying the bearer token.
            Defaults to ``None``.

    Returns:
        The updated report with public photo URL resolved. Responds 403 when the
        token is invalid and 404 when no report matches.
    """
    return await _moderate(report_id, ReportStatus.APPROVED, settings, repository, authorization)


@router.post("/moderation/{report_id}/reject", response_model=CitizenReport)
async def reject_report(
    report_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[ReportRepository, Depends(get_report_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> CitizenReport:
    """Reject a report so it stays hidden from the public (token protected).

    Args:
        report_id: Opaque report identifier.
        settings: Application settings injected via dependency.
        repository: Citizen-report repository injected via dependency.
        authorization: ``Authorization`` header carrying the bearer token.
            Defaults to ``None``.

    Returns:
        The updated report. Responds 403 when the token is invalid and 404 when
        no report matches.
    """
    return await _moderate(report_id, ReportStatus.REJECTED, settings, repository, authorization)


async def _moderate(
    report_id: str,
    new_status: ReportStatus,
    settings: Settings,
    repository: ReportRepository,
    authorization: str | None,
) -> CitizenReport:
    """Authorize and apply a moderation status transition.

    Args:
        report_id: Opaque report identifier.
        new_status: Target moderation status.
        settings: Application settings carrying the moderation token.
        repository: Citizen-report repository.
        authorization: Raw ``Authorization`` header value, or ``None``.

    Returns:
        The updated report with its photo URL resolved.

    Raises:
        HTTPException: 403 when the token is invalid; 404 when no report matches.
    """
    _require_moderation_token(authorization, settings)
    updated = await repository.set_status(report_id, new_status)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found.")
    moderation = new_status is not ReportStatus.APPROVED
    return _with_photo_url(updated, moderation=moderation)


async def _stream_photo(
    repository: ReportRepository,
    storage: PhotoStorage,
    report_id: str,
) -> StreamingResponse:
    """Read a report's photo from storage and return it as a streaming response.

    Args:
        repository: Citizen-report repository.
        storage: Report photo storage backend.
        report_id: Opaque report identifier.

    Returns:
        The photo bytes as a streaming response with the stored content type.

    Raises:
        HTTPException: 404 when the report has no key or the photo is missing.
    """
    key = await repository.photo_key_for(report_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found.")
    try:
        stored = await storage.open(key)
    except PhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found."
        ) from exc
    import io

    return StreamingResponse(
        io.BytesIO(stored.data),
        media_type=stored.content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
