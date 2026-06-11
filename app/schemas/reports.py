"""Pydantic models for the citizen flood-report contract (HFT-73).

The public contract is intentionally privacy-minimal: a report carries a
location, a coarse water-depth category, an optional free-text note, and an
optional photo reference. It never carries a name, phone number, or any other
personal identifier, and the submitter IP is never stored on the report
document (it is used transiently for rate limiting only). See the PDPA note in
the backend README.

Response shapes are additive-friendly: new optional fields can be appended
without breaking the frontend (HFT-74).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Coordinates


class WaterDepthCategory(StrEnum):
    """Model the coarse, body-relative water-depth categories a citizen reports.

    Categories are body-relative rather than numeric because untrained
    observers estimate depth against their own body far more reliably than in
    centimetres. Ordered shallow to deep.
    """

    ANKLE = "ankle"
    KNEE = "knee"
    WAIST = "waist"
    ABOVE_WAIST = "above_waist"


class ReportStatus(StrEnum):
    """Model the moderation lifecycle state of a citizen report."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReportSubmission(BaseModel):
    """Model the validated body of a citizen flood-report submission.

    Used for the JSON submission path and as the normalized representation of
    the multipart form fields. The photo is handled separately as an upload, so
    it is not part of this model.
    """

    longitude: float = Field(
        ge=-180,
        le=180,
        description="WGS84 longitude of the reported flooding.",
    )
    latitude: float = Field(
        ge=-90,
        le=90,
        description="WGS84 latitude of the reported flooding.",
    )
    water_depth: WaterDepthCategory = Field(
        description="Coarse, body-relative water-depth category.",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Optional free-text note (length-capped, no personal data requested).",
    )


class CitizenReport(BaseModel):
    """Model a normalized citizen flood report as stored and served.

    This is the canonical internal record and the basis of the public read
    shape. It deliberately omits any submitter identifier. ``photo_url`` is a
    relative API path the frontend resolves against ``VITE_API_URL``; it is
    populated only when a photo was attached.
    """

    id: str = Field(description="Opaque report identifier (Mongo ObjectId hex string).")
    location: Coordinates = Field(description="Reported flooding location in WGS84.")
    water_depth: WaterDepthCategory = Field(
        description="Coarse, body-relative water-depth category."
    )
    note: str | None = Field(default=None, description="Optional free-text note, when provided.")
    status: ReportStatus = Field(description="Moderation lifecycle state.")
    has_photo: bool = Field(description="Whether a photo is attached to this report.")
    photo_url: str | None = Field(
        default=None,
        description=(
            "Relative API path to stream the photo, or null when no photo is attached. "
            "Resolve against the API base URL."
        ),
    )
    created_at: datetime = Field(description="UTC submission timestamp (ISO 8601).")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "665f1c2a9b1e4a0012ab34cd",
                "location": {"longitude": 100.474, "latitude": 6.997},
                "water_depth": "knee",
                "note": "Water rising near the market footbridge.",
                "status": "approved",
                "has_photo": True,
                "photo_url": "/api/reports/665f1c2a9b1e4a0012ab34cd/photo",
                "created_at": "2026-06-12T03:21:00Z",
            }
        }
    )


class ReportSubmissionResponse(BaseModel):
    """Model the response returned after a successful report submission.

    The submitter receives the new report id and its ``pending`` status so a
    client can show a "submitted, awaiting review" state. The report is not
    publicly visible until a moderator approves it.
    """

    id: str = Field(description="Opaque identifier of the newly created report.")
    status: ReportStatus = Field(description="Always ``pending`` for a fresh submission.")
    has_photo: bool = Field(description="Whether a photo was attached and stored.")
    created_at: datetime = Field(description="UTC submission timestamp (ISO 8601).")


class ReportListResponse(BaseModel):
    """Model the public list of approved citizen reports.

    Only approved reports ever appear here. The list is bounded to the most
    recent ``count`` reports so the public endpoint stays cheap on mobile.
    """

    reports: list[CitizenReport] = Field(description="Approved reports, newest first.")
    count: int = Field(description="Number of reports in this response.")


__all__ = [
    "CitizenReport",
    "ReportListResponse",
    "ReportStatus",
    "ReportSubmission",
    "ReportSubmissionResponse",
    "WaterDepthCategory",
]
