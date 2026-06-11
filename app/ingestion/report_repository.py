"""MongoDB persistence for citizen flood reports and per-IP rate limiting.

Two regular (non time-series) collections back this feature:

- ``citizen_reports`` holds reports. A regular collection is required because a
  report's ``status`` mutates through the moderation lifecycle
  (pending -> approved/rejected); MongoDB time-series collections forbid
  in-place updates, so they are the wrong tool here. A ``2dsphere`` index on
  ``location`` supports map-driven geo queries, and a ``(status, created_at)``
  index serves the public "recent approved" read cheaply.
- ``report_rate_limits`` holds per-IP-hour counters. The submitter IP is never
  stored in clear text: the key is a salted SHA-256 hash, so the document
  cannot be reversed to an IP. A TTL index expires stale buckets automatically,
  keeping the limiter dependency-free (no cron).

For Phase 4 the repository is optional: a :class:`DryRunReportRepository` keeps
everything in memory so the API can be exercised without a Mongo instance,
matching the forecast/station/subscription repositories.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from pymongo import ASCENDING, DESCENDING, GEOSPHERE

from app.schemas.common import Coordinates
from app.schemas.reports import CitizenReport, ReportStatus, WaterDepthCategory

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

logger = logging.getLogger(__name__)


CITIZEN_REPORTS_COLLECTION = "citizen_reports"
REPORT_RATE_LIMITS_COLLECTION = "report_rate_limits"

# Public read defaults.
DEFAULT_APPROVED_LIMIT = 100
# Rate-limit window and ceiling.
RATE_LIMIT_WINDOW = timedelta(hours=1)
DEFAULT_RATE_LIMIT_MAX = 5


def _document_to_report(document: dict[str, object]) -> CitizenReport:
    """Build a :class:`CitizenReport` from a stored Mongo document.

    Args:
        document: Raw Mongo document with a GeoJSON ``location`` sub-document.

    Returns:
        The normalized citizen report (without a resolved ``photo_url``).
    """
    location = cast("dict[str, object]", document["location"])
    coordinates = cast("list[float]", location["coordinates"])
    photo_key = cast("str | None", document.get("photo_key"))
    return CitizenReport(
        id=str(document["_id"]),
        location=Coordinates(longitude=coordinates[0], latitude=coordinates[1]),
        water_depth=WaterDepthCategory(cast("str", document["water_depth"])),
        note=cast("str | None", document.get("note")),
        status=ReportStatus(cast("str", document["status"])),
        has_photo=photo_key is not None,
        photo_url=None,
        created_at=cast("datetime", document["created_at"]),
    )


class ReportRepository(Protocol):
    """Persist citizen reports, moderate them, and enforce per-IP rate limits."""

    async def ensure_indexes(self) -> None:
        """Create report and rate-limit indexes used by Phase 4."""
        ...

    async def create_report(
        self,
        *,
        longitude: float,
        latitude: float,
        water_depth: WaterDepthCategory,
        note: str | None,
        photo_key: str | None,
        created_at: datetime,
    ) -> CitizenReport:
        """Insert a new pending report and return it.

        Args:
            longitude: WGS84 longitude of the reported flooding.
            latitude: WGS84 latitude of the reported flooding.
            water_depth: Coarse water-depth category.
            note: Optional free-text note.
            photo_key: Opaque storage key for an attached photo, or ``None``.
            created_at: UTC submission timestamp.

        Returns:
            The stored report with status ``pending``.
        """
        ...

    async def list_approved(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return the most recent approved reports, newest first.

        Args:
            limit: Maximum number of reports to return. Defaults to
                :data:`DEFAULT_APPROVED_LIMIT`.

        Returns:
            Approved reports ordered newest first.
        """
        ...

    async def list_pending(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return the most recent pending reports for moderation.

        Args:
            limit: Maximum number of reports to return. Defaults to
                :data:`DEFAULT_APPROVED_LIMIT`.

        Returns:
            Pending reports ordered newest first.
        """
        ...

    async def get_report(self, report_id: str) -> CitizenReport | None:
        """Return a single report by id, or ``None`` when absent.

        Args:
            report_id: Opaque report identifier.

        Returns:
            The report, or ``None`` when no report matches.
        """
        ...

    async def set_status(self, report_id: str, status: ReportStatus) -> CitizenReport | None:
        """Transition a report to a new moderation status.

        Args:
            report_id: Opaque report identifier.
            status: New moderation status to set.

        Returns:
            The updated report, or ``None`` when no report matches.
        """
        ...

    async def register_submission(
        self, ip_hash: str, *, now: datetime, max_per_window: int
    ) -> bool:
        """Record a submission attempt and report whether it is within the limit.

        Args:
            ip_hash: Salted hash of the submitter IP (never the raw IP).
            now: Current UTC time used to bucket the rate-limit window.
            max_per_window: Maximum submissions allowed per window.

        Returns:
            ``True`` when the submission is allowed, ``False`` when it exceeds
            the per-IP limit.
        """
        ...

    async def photo_key_for(self, report_id: str) -> str | None:
        """Return the opaque storage key of a report's photo, or ``None``.

        Args:
            report_id: Opaque report identifier.

        Returns:
            The stored photo key, or ``None`` when the report has no photo.
        """
        ...


class DryRunReportRepository:
    """Keep citizen reports and rate-limit counters in memory.

    Used by the dry-run backend and unit tests so the API can be exercised
    without a Mongo instance. Behavior mirrors the Mongo-backed repository.
    """

    def __init__(self) -> None:
        self._reports: dict[str, dict[str, object]] = {}
        self._counter = 0
        self._rate: dict[str, list[datetime]] = {}

    async def ensure_indexes(self) -> None:
        """No-op for the in-memory backend."""

    async def create_report(
        self,
        *,
        longitude: float,
        latitude: float,
        water_depth: WaterDepthCategory,
        note: str | None,
        photo_key: str | None,
        created_at: datetime,
    ) -> CitizenReport:
        """Insert a new pending report into the in-memory store.

        Args:
            longitude: WGS84 longitude of the reported flooding.
            latitude: WGS84 latitude of the reported flooding.
            water_depth: Coarse water-depth category.
            note: Optional free-text note.
            photo_key: Opaque storage key for an attached photo, or ``None``.
            created_at: UTC submission timestamp.

        Returns:
            The stored report with status ``pending``.
        """
        self._counter += 1
        report_id = f"{self._counter:024x}"
        document: dict[str, object] = {
            "_id": report_id,
            "location": {"type": "Point", "coordinates": [longitude, latitude]},
            "water_depth": water_depth.value,
            "note": note,
            "status": ReportStatus.PENDING.value,
            "photo_key": photo_key,
            "created_at": created_at,
        }
        self._reports[report_id] = document
        return _document_to_report(document)

    async def list_approved(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return approved in-memory reports newest first.

        Args:
            limit: Maximum number of reports to return.

        Returns:
            Approved reports ordered newest first.
        """
        return self._filter_by_status(ReportStatus.APPROVED, limit)

    async def list_pending(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return pending in-memory reports newest first.

        Args:
            limit: Maximum number of reports to return.

        Returns:
            Pending reports ordered newest first.
        """
        return self._filter_by_status(ReportStatus.PENDING, limit)

    def _filter_by_status(self, status: ReportStatus, limit: int) -> list[CitizenReport]:
        """Return reports matching a status, newest first, capped at ``limit``.

        Args:
            status: Moderation status to filter on.
            limit: Maximum number of reports to return.

        Returns:
            Matching reports ordered newest first.
        """
        matches = [doc for doc in self._reports.values() if doc["status"] == status.value]
        matches.sort(key=lambda doc: doc["created_at"], reverse=True)  # type: ignore[arg-type,return-value]
        return [_document_to_report(doc) for doc in matches[:limit]]

    async def get_report(self, report_id: str) -> CitizenReport | None:
        """Return a single in-memory report by id.

        Args:
            report_id: Opaque report identifier.

        Returns:
            The report, or ``None`` when no report matches.
        """
        document = self._reports.get(report_id)
        return _document_to_report(document) if document is not None else None

    async def set_status(self, report_id: str, status: ReportStatus) -> CitizenReport | None:
        """Update an in-memory report's status.

        Args:
            report_id: Opaque report identifier.
            status: New moderation status to set.

        Returns:
            The updated report, or ``None`` when no report matches.
        """
        document = self._reports.get(report_id)
        if document is None:
            return None
        document["status"] = status.value
        return _document_to_report(document)

    async def register_submission(
        self, ip_hash: str, *, now: datetime, max_per_window: int
    ) -> bool:
        """Record a submission and report whether it is within the limit.

        Args:
            ip_hash: Salted hash of the submitter IP.
            now: Current UTC time used to bucket the rate-limit window.
            max_per_window: Maximum submissions allowed per window.

        Returns:
            ``True`` when the submission is allowed, ``False`` otherwise.
        """
        cutoff = now - RATE_LIMIT_WINDOW
        recent = [stamp for stamp in self._rate.get(ip_hash, []) if stamp > cutoff]
        if len(recent) >= max_per_window:
            self._rate[ip_hash] = recent
            return False
        recent.append(now)
        self._rate[ip_hash] = recent
        return True

    async def photo_key_for(self, report_id: str) -> str | None:
        """Return the stored photo key for an in-memory report, if any.

        Args:
            report_id: Opaque report identifier.

        Returns:
            The stored photo key, or ``None``.
        """
        document = self._reports.get(report_id)
        if document is None:
            return None
        return cast("str | None", document.get("photo_key"))


class MongoReportRepository:
    """Persist citizen reports and rate-limit counters in MongoDB."""

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._reports: AsyncIOMotorCollection = database[CITIZEN_REPORTS_COLLECTION]
        self._rate_limits: AsyncIOMotorCollection = database[REPORT_RATE_LIMITS_COLLECTION]

    @property
    def reports(self) -> AsyncIOMotorCollection:
        """Return the underlying reports collection handle."""
        return self._reports

    async def ensure_indexes(self) -> None:
        """Create the report and rate-limit indexes used by Phase 4.

        Creates a ``2dsphere`` index on ``location`` for geo queries, a
        ``(status, created_at desc)`` index for the public recent-approved read,
        and a TTL index on the rate-limit bucket so stale per-IP counters expire
        without a cron job.
        """
        await self._reports.create_index(
            [("status", ASCENDING), ("created_at", DESCENDING)],
            name="status_created_at",
        )
        try:
            await self._reports.create_index(
                [("location", GEOSPHERE)],
                name="location_2dsphere",
            )
        except NotImplementedError:
            # Some mock backends do not support 2dsphere; the route does not
            # depend on it, so we degrade silently.
            pass
        try:
            await self._rate_limits.create_index(
                [("expires_at", ASCENDING)],
                name="rate_limit_ttl",
                expireAfterSeconds=0,
            )
        except NotImplementedError:
            pass

    async def create_report(
        self,
        *,
        longitude: float,
        latitude: float,
        water_depth: WaterDepthCategory,
        note: str | None,
        photo_key: str | None,
        created_at: datetime,
    ) -> CitizenReport:
        """Insert a new pending report document and return it.

        Args:
            longitude: WGS84 longitude of the reported flooding.
            latitude: WGS84 latitude of the reported flooding.
            water_depth: Coarse water-depth category.
            note: Optional free-text note.
            photo_key: Opaque storage key for an attached photo, or ``None``.
            created_at: UTC submission timestamp.

        Returns:
            The stored report with status ``pending``.
        """
        document: dict[str, object] = {
            "location": {"type": "Point", "coordinates": [longitude, latitude]},
            "water_depth": water_depth.value,
            "note": note,
            "status": ReportStatus.PENDING.value,
            "photo_key": photo_key,
            "created_at": created_at,
        }
        result = await self._reports.insert_one(document)
        document["_id"] = result.inserted_id
        return _document_to_report(document)

    async def list_approved(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return the most recent approved reports.

        Args:
            limit: Maximum number of reports to return.

        Returns:
            Approved reports ordered newest first.
        """
        return await self._list_by_status(ReportStatus.APPROVED, limit)

    async def list_pending(self, *, limit: int = DEFAULT_APPROVED_LIMIT) -> list[CitizenReport]:
        """Return the most recent pending reports.

        Args:
            limit: Maximum number of reports to return.

        Returns:
            Pending reports ordered newest first.
        """
        return await self._list_by_status(ReportStatus.PENDING, limit)

    async def _list_by_status(self, status: ReportStatus, limit: int) -> list[CitizenReport]:
        """Return reports with a status, newest first, capped at ``limit``.

        Args:
            status: Moderation status to filter on.
            limit: Maximum number of reports to return.

        Returns:
            Matching reports ordered newest first.
        """
        cursor = (
            self._reports.find({"status": status.value})
            .sort([("created_at", DESCENDING)])
            .limit(limit)
        )
        results: list[CitizenReport] = []
        async for document in cursor:
            results.append(_document_to_report(document))
        return results

    async def get_report(self, report_id: str) -> CitizenReport | None:
        """Return a single report by id.

        Args:
            report_id: Opaque report identifier (Mongo ObjectId hex string).

        Returns:
            The report, or ``None`` when the id is malformed or absent.
        """
        object_id = self._to_object_id(report_id)
        if object_id is None:
            return None
        document = await self._reports.find_one({"_id": object_id})
        return _document_to_report(document) if document is not None else None

    async def set_status(self, report_id: str, status: ReportStatus) -> CitizenReport | None:
        """Transition a report to a new moderation status.

        Args:
            report_id: Opaque report identifier (Mongo ObjectId hex string).
            status: New moderation status to set.

        Returns:
            The updated report, or ``None`` when the id is malformed or absent.
        """
        object_id = self._to_object_id(report_id)
        if object_id is None:
            return None
        document = await self._reports.find_one_and_update(
            {"_id": object_id},
            {"$set": {"status": status.value}},
            return_document=True,
        )
        return _document_to_report(document) if document is not None else None

    async def register_submission(
        self, ip_hash: str, *, now: datetime, max_per_window: int
    ) -> bool:
        """Atomically increment a per-IP-hour counter and check the ceiling.

        The counter is keyed on ``(ip_hash, window_start)`` so each one-hour
        bucket is independent. A TTL index on ``expires_at`` reaps old buckets.

        Args:
            ip_hash: Salted hash of the submitter IP.
            now: Current UTC time used to bucket the rate-limit window.
            max_per_window: Maximum submissions allowed per window.

        Returns:
            ``True`` when the submission is allowed, ``False`` otherwise.
        """
        window_start = now.replace(minute=0, second=0, microsecond=0)
        key = f"{ip_hash}:{window_start.isoformat()}"
        document = await self._rate_limits.find_one_and_update(
            {"_id": key},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {"expires_at": window_start + RATE_LIMIT_WINDOW},
            },
            upsert=True,
            return_document=True,
        )
        count = int(document.get("count", 1)) if document is not None else 1
        return count <= max_per_window

    @staticmethod
    def _to_object_id(report_id: str) -> object | None:
        """Parse a report id into a BSON ObjectId, or ``None`` when malformed.

        Args:
            report_id: Opaque report identifier hex string.

        Returns:
            The parsed ObjectId, or ``None`` when the id is not a valid ObjectId.
        """
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            return ObjectId(report_id)
        except (InvalidId, TypeError):
            return None

    async def photo_key_for(self, report_id: str) -> str | None:
        """Return the stored photo key for a report, if any.

        Args:
            report_id: Opaque report identifier.

        Returns:
            The stored photo key, or ``None`` when absent.
        """
        object_id = self._to_object_id(report_id)
        if object_id is None:
            return None
        document = await self._reports.find_one(
            {"_id": object_id}, projection={"photo_key": 1}
        )
        if document is None:
            return None
        return document.get("photo_key")


def build_mongo_report_repository(
    client: AsyncIOMotorClient, database_name: str
) -> MongoReportRepository:
    """Create a :class:`MongoReportRepository` bound to ``database_name``.

    Args:
        client: Motor async MongoDB client.
        database_name: Name of the MongoDB database.

    Returns:
        A configured MongoReportRepository instance.
    """
    return MongoReportRepository(client[database_name])


__all__ = [
    "CITIZEN_REPORTS_COLLECTION",
    "DEFAULT_APPROVED_LIMIT",
    "DEFAULT_RATE_LIMIT_MAX",
    "REPORT_RATE_LIMITS_COLLECTION",
    "DryRunReportRepository",
    "MongoReportRepository",
    "ReportRepository",
    "build_mongo_report_repository",
]
