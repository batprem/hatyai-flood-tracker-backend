"""ThaiWater / HAII public station observation client.

This module exposes:

- :class:`StationObservation` — internal Pydantic record normalized from any
  station provider; it is the contract every implementation of
  :class:`StationObservationClient` must satisfy.
- :class:`StationObservationClient` — Protocol that future providers
  (TMD, RID Smart Data, municipal feeds) can implement so the rest of the
  backend can swap providers without touching API routes or risk inputs.
- :class:`ThaiwaterStationClient` — async HTTP client for the public
  ThaiWater / HAII (Hydro Informatics Institute) endpoints. Phase 1 fetches
  the most recent water-level reading for a seed list of U-Tapao canal
  stations.

Design notes:

- Raw provider payloads are never returned to callers. The provider response
  shape is parsed inside :meth:`ThaiwaterStationClient.fetch_latest_water_levels`
  and normalized into :class:`StationObservation` records. Public response
  shape stability is preserved per the project conventions.
- Phase 1 station metadata (display names in Thai and English, canal/lake
  attribution, location, warning/critical thresholds) is curated from
  ONWR/RID reports referenced in ``docs/data-sources.md``. ThaiWater
  surfaces the same stations under codes ``X.173A`` (Ban Muang Kong),
  ``X.44`` (Ban Hat Yai), and ``X.174`` (Khlong Wa).
- The client uses an injected ``httpx.AsyncClient`` so the FastAPI lifespan
  can own its connection pool. Tests inject a stub transport via
  ``httpx.MockTransport`` to keep network access out of the unit suite.
- Provider freshness is enforced in two places: the HTTP call has a short
  timeout (fail fast on outages), and per-record freshness is validated by
  comparing ``observed_at`` against ``max_age_hours``. Stale records are
  dropped before they reach the public API so the risk engine sees
  ``RiskFreshnessStatus.STALE`` cleanly rather than mixing aged values.

Attribution and license:
    ThaiWater data is published by HAII (Hydro Informatics Institute,
    https://www.haii.or.th/). Public reuse and redistribution terms must be
    confirmed before production launch per ``docs/data-sources.md``. Until
    then, every record carries ``license_note='review-required'`` so the
    backend can audit usage before public commitments.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public attribution constants
# ---------------------------------------------------------------------------

THAIWATER_PROVIDER_NAME = "thaiwater-haii"
THAIWATER_ATTRIBUTION = "ThaiWater / HAII (Hydro Informatics Institute)"
THAIWATER_LICENSE_NOTE = "review-required"
THAIWATER_HOMEPAGE = "https://www.thaiwater.net/"

# ThaiWater publishes water-level readings in Asia/Bangkok local time without
# an explicit offset, so we localise raw timestamps with the project timezone
# before converting to UTC for storage and API responses.
THAILAND_TZ = ZoneInfo("Asia/Bangkok")


# ---------------------------------------------------------------------------
# Variable taxonomy
# ---------------------------------------------------------------------------


class StationVariable(StrEnum):
    """Identify which physical variable a station record represents."""

    WATER_LEVEL = "water_level"
    RAINFALL = "rainfall"


class StationQualityFlag(StrEnum):
    """Coarse quality flag for observations.

    ThaiWater does not consistently expose a numeric quality control level
    on the public read endpoints, so the client maps presence/freshness into
    a small set of flags that the risk engine and frontend can interpret
    without provider-specific knowledge.
    """

    OK = "ok"
    STALE = "stale"
    SUSPECT = "suspect"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Internal normalized record
# ---------------------------------------------------------------------------


class StationGeoPoint(BaseModel):
    """Model a station's WGS84 location as a GeoJSON Point.

    Stored as a GeoJSON-shaped sub-document so MongoDB ``2dsphere`` indexes
    can be applied to ``station_observations.location`` without re-encoding
    at write time.
    """

    type: str = "Point"
    coordinates: tuple[float, float] = Field(
        description="(longitude, latitude) in EPSG:4326.",
    )

    @field_validator("coordinates")
    @classmethod
    def _validate_lon_lat(cls, value: tuple[float, float]) -> tuple[float, float]:
        lon, lat = value
        if not -180 <= lon <= 180:
            msg = f"longitude must be within [-180, 180]; got {lon!r}"
            raise ValueError(msg)
        if not -90 <= lat <= 90:
            msg = f"latitude must be within [-90, 90]; got {lat!r}"
            raise ValueError(msg)
        return value


class StationObservation(BaseModel):
    """Normalized station observation record.

    Every provider client must produce this shape. Field naming follows the
    docs/data-sources.md guidance for observed rainfall and water-level
    records: provider/source provenance, station id, variable, value, unit,
    observed/retrieved times in UTC, quality flag, and a license note.

    Provider-specific raw payloads are intentionally absent. Only normalized
    values may flow downstream into the API or the risk engine.
    """

    provider: str = Field(description="Stable provider identifier, e.g. 'thaiwater-haii'.")
    source_system: str = Field(
        description="Access path used: 'api', 'bulk', 'data-sharing', or 'manual'."
    )
    station_id: str = Field(description="Provider-stable station code, e.g. 'X.173A'.")
    station_name_th: str
    station_name_en: str
    canal_or_lake_th: str
    canal_or_lake_en: str
    location: StationGeoPoint
    variable: StationVariable
    value: float
    unit: str
    observed_at: datetime = Field(description="Measurement time (UTC, timezone-aware).")
    retrieved_at: datetime = Field(description="When this system fetched the record (UTC).")
    quality_flag: StationQualityFlag = StationQualityFlag.UNKNOWN
    warning_level_m: float | None = Field(
        default=None,
        gt=0,
        description="Curated warning threshold for water-level stations (metres).",
    )
    critical_level_m: float | None = Field(
        default=None,
        gt=0,
        description="Curated critical/danger threshold for water-level stations (metres).",
    )
    provenance_url: str = Field(description="Source URL that served this record.")
    license_note: str = THAIWATER_LICENSE_NOTE
    attribution: str = THAIWATER_ATTRIBUTION

    model_config = ConfigDict(extra="forbid")

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "station timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    def is_fresh(self, *, now: datetime, max_age: timedelta) -> bool:
        """Return True when ``observed_at`` is within ``max_age`` of ``now``."""
        return (now - self.observed_at) <= max_age


# ---------------------------------------------------------------------------
# Seed station registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StationSeed:
    """Curated metadata for a Phase 1 station.

    ThaiWater records expose station codes and latest readings but the
    project owns the canonical display strings and risk thresholds.
    Centralising them in a seed list keeps the API contract stable even if
    the provider renames or reclassifies stations.
    """

    station_id: str
    name_th: str
    name_en: str
    canal_or_lake_th: str
    canal_or_lake_en: str
    longitude: float
    latitude: float
    warning_level_m: float
    critical_level_m: float


SONGKHLA_BASIN_CODE = "21"
SONGKHLA_PROVINCE_CODE = "90"

PHASE1_STATION_SEEDS: tuple[StationSeed, ...] = (
    StationSeed(
        station_id="X.173A",
        name_th="บ้านม่วงก็อง คลองอู่ตะเภา",
        name_en="Ban Muang Kong, U-Tapao Canal",
        canal_or_lake_th="คลองอู่ตะเภา",
        canal_or_lake_en="U-Tapao Canal",
        longitude=100.5006,
        latitude=6.8242,
        warning_level_m=7.5,
        critical_level_m=8.5,
    ),
    StationSeed(
        station_id="X.44",
        name_th="บ้านหาดใหญ่ คลองอู่ตะเภา",
        name_en="Ban Hat Yai, U-Tapao Canal",
        canal_or_lake_th="คลองอู่ตะเภา",
        canal_or_lake_en="U-Tapao Canal",
        longitude=100.4708,
        latitude=7.0167,
        warning_level_m=6.0,
        critical_level_m=7.0,
    ),
    StationSeed(
        station_id="X.174",
        name_th="คลองวาด",
        name_en="Khlong Wa",
        canal_or_lake_th="คลองวาด",
        canal_or_lake_en="Khlong Wa Canal",
        longitude=100.4275,
        latitude=6.9525,
        warning_level_m=8.0,
        critical_level_m=9.0,
    ),
)


# ---------------------------------------------------------------------------
# Protocol for pluggable providers
# ---------------------------------------------------------------------------


class StationObservationClient(Protocol):
    """Fetch normalized station observations from a provider.

    Implementations must be async and must never expose raw provider
    payloads in their return value. They may apply per-record freshness
    filtering before returning, but the source-of-truth freshness threshold
    is owned by the caller.
    """

    provider: str

    async def fetch_latest_water_levels(self) -> list[StationObservation]:
        """Return the latest water-level reading per seed station."""
        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ThaiwaterIngestionError(RuntimeError):
    """Raise for recoverable ThaiWater ingestion failures with operator context."""


# ---------------------------------------------------------------------------
# ThaiWater client
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ThaiwaterStationClient:
    """Async client for the public ThaiWater / HAII station API.

    The client expects an injected ``httpx.AsyncClient`` so the FastAPI
    lifespan owns its connection pool and tests can swap in a
    ``httpx.MockTransport`` without monkey-patching.

    Attributes:
        http_client: Lifespan-managed async HTTP client.
        base_url: ThaiWater public API base URL.
        seeds: Phase 1 station seed metadata. Defaults to
            :data:`PHASE1_STATION_SEEDS`.
        max_age: Maximum age of an observation before it is treated as
            stale and dropped from the returned list.
        basin_code: ThaiWater basin filter (Songkhla = "21").
        province_code: ThaiWater province filter (Songkhla = "90").
        api_key: Optional bearer token. The public read-only endpoints
            currently used do not require credentials, but the value is
            forwarded as ``Authorization: Bearer ...`` when present so
            future credentialed tiers work without code changes.
        provider: Provider identifier recorded on each observation.
        attribution: Public attribution string.
        license_note: License/redistribution review state.
    """

    http_client: httpx.AsyncClient
    base_url: str = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public"
    seeds: Sequence[StationSeed] = field(default_factory=lambda: PHASE1_STATION_SEEDS)
    max_age: timedelta = timedelta(hours=3)
    basin_code: str = SONGKHLA_BASIN_CODE
    province_code: str = SONGKHLA_PROVINCE_CODE
    api_key: str | None = None
    provider: str = THAIWATER_PROVIDER_NAME
    attribution: str = THAIWATER_ATTRIBUTION
    license_note: str = THAIWATER_LICENSE_NOTE

    def _waterlevel_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/waterlevel_load"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def fetch_latest_water_levels(
        self,
        *,
        now: datetime | None = None,
    ) -> list[StationObservation]:
        """Fetch and normalize the latest water-level reading per seed station.

        The ThaiWater ``waterlevel_load`` endpoint returns the most recent
        observation for every active water-level station, filtered by basin
        and province. The response payload is intentionally treated as
        opaque ``JsonValue`` here: only the fields needed for normalization
        are extracted, and unknown fields are ignored so provider schema
        drift does not break the public API.

        Args:
            now: Optional override for "current UTC time"; tests inject a
                deterministic value to exercise the freshness filter.

        Returns:
            A list of :class:`StationObservation` records, one per seed
            station that has a matching fresh reading. Stations missing
            from the provider response, or whose latest reading is older
            than ``self.max_age``, are omitted; callers can compare the
            returned ids against the seed list to detect gaps.

        Raises:
            ThaiwaterIngestionError: When the HTTP call fails after the
                client's configured retries or the response is not
                parseable JSON.
        """
        resolved_now = now or datetime.now(UTC)
        payload = await self._get_json(self._waterlevel_url())
        raw_records = _extract_record_list(payload)
        seeds_by_id = {seed.station_id: seed for seed in self.seeds}

        latest_by_seed: dict[str, _ParsedRecord] = {}
        for raw in raw_records:
            parsed = _parse_waterlevel_record(raw)
            if parsed is None:
                continue
            seed = seeds_by_id.get(parsed.station_code)
            if seed is None:
                continue
            existing = latest_by_seed.get(seed.station_id)
            if existing is None or parsed.observed_at > existing.observed_at:
                latest_by_seed[seed.station_id] = parsed

        observations: list[StationObservation] = []
        for seed in self.seeds:
            parsed = latest_by_seed.get(seed.station_id)
            if parsed is None:
                logger.debug("ThaiWater returned no record for seed station %s", seed.station_id)
                continue
            observation = _build_observation(
                seed=seed,
                parsed=parsed,
                provider=self.provider,
                attribution=self.attribution,
                license_note=self.license_note,
                provenance_url=self._waterlevel_url(),
                retrieved_at=resolved_now,
            )
            if not observation.is_fresh(now=resolved_now, max_age=self.max_age):
                logger.info(
                    "Dropping stale ThaiWater observation for %s (observed_at=%s, age=%.1fh)",
                    seed.station_id,
                    observation.observed_at.isoformat(),
                    (resolved_now - observation.observed_at).total_seconds() / 3600,
                )
                continue
            observations.append(observation)
        return observations

    async def _get_json(self, url: str) -> object:
        try:
            response = await self.http_client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            msg = f"ThaiWater request to {url} failed: {exc}"
            raise ThaiwaterIngestionError(msg) from exc
        if response.status_code != 200:
            msg = f"ThaiWater returned HTTP {response.status_code} for {url}"
            raise ThaiwaterIngestionError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"ThaiWater response for {url} is not valid JSON"
            raise ThaiwaterIngestionError(msg) from exc


# ---------------------------------------------------------------------------
# Provider response parsing helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedRecord:
    """Internal parse intermediate before mapping to :class:`StationObservation`."""

    station_code: str
    value_m: float
    observed_at: datetime


def _extract_record_list(payload: object) -> Iterable[object]:
    """Locate the records list in a ThaiWater JSON envelope.

    The HAII APIs typically wrap arrays inside ``{"data": [...]}`` but a few
    legacy endpoints return a bare list. The helper supports both so the
    parsing path does not need provider-version branches.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        payload_dict = cast(dict[str, object], payload)
        data = payload_dict.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            data_dict = cast(dict[str, object], data)
            inner = data_dict.get("data") or data_dict.get("items")
            if isinstance(inner, list):
                return inner
    return ()


def _parse_waterlevel_record(raw: object) -> _ParsedRecord | None:
    """Parse one ThaiWater water-level record into normalized fields.

    The endpoint returns objects keyed roughly as:
        {
            "station": {"tele_station_oldcode": "X.173A", ...},
            "waterlevel_msl": 7.42,
            "waterlevel_datetime": "2026-05-17 14:00:00",
            ...
        }

    Older deployments embed the code at the top level instead of nested
    under ``station``. This helper tolerates both shapes and returns
    ``None`` when required fields are missing, so a partial record does
    not poison the rest of the batch.
    """
    if not isinstance(raw, dict):
        return None
    raw_dict = cast(dict[str, object], raw)
    station_code = _extract_station_code(raw_dict)
    if not station_code:
        return None
    value = _coerce_float(
        raw_dict.get("waterlevel_msl")
        or raw_dict.get("water_level_msl")
        or raw_dict.get("waterlevel")
        or raw_dict.get("value")
    )
    if value is None:
        return None
    observed_raw = (
        raw_dict.get("waterlevel_datetime")
        or raw_dict.get("waterlevel_time")
        or raw_dict.get("datetime")
        or raw_dict.get("measureTime")
        or raw_dict.get("observedAt")
    )
    observed_at = _parse_thai_datetime(observed_raw)
    if observed_at is None:
        return None
    return _ParsedRecord(
        station_code=station_code,
        value_m=value,
        observed_at=observed_at,
    )


def _extract_station_code(raw: dict[str, object]) -> str | None:
    station = raw.get("station")
    if isinstance(station, dict):
        station_dict = cast(dict[str, object], station)
        for key in ("tele_station_oldcode", "station_oldcode", "station_code", "code"):
            value = station_dict.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("tele_station_oldcode", "station_oldcode", "station_code", "stationId", "code"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_thai_datetime(value: object) -> datetime | None:
    """Parse a ThaiWater timestamp string into a UTC datetime.

    ThaiWater records use Asia/Bangkok local time without an explicit
    offset. ISO 8601 strings with an explicit ``Z`` or offset are honored
    as-is; naive strings are localised to Asia/Bangkok before conversion
    to UTC.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    candidates = (raw, raw.replace(" ", "T"))
    for candidate in candidates:
        normalised = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=THAILAND_TZ)
        return parsed.astimezone(UTC)
    return None


def _build_observation(
    *,
    seed: StationSeed,
    parsed: _ParsedRecord,
    provider: str,
    attribution: str,
    license_note: str,
    provenance_url: str,
    retrieved_at: datetime,
) -> StationObservation:
    """Combine seed metadata + parsed record into a normalized observation."""
    return StationObservation(
        provider=provider,
        source_system="api",
        station_id=seed.station_id,
        station_name_th=seed.name_th,
        station_name_en=seed.name_en,
        canal_or_lake_th=seed.canal_or_lake_th,
        canal_or_lake_en=seed.canal_or_lake_en,
        location=StationGeoPoint(coordinates=(seed.longitude, seed.latitude)),
        variable=StationVariable.WATER_LEVEL,
        value=parsed.value_m,
        unit="m",
        observed_at=parsed.observed_at,
        retrieved_at=retrieved_at,
        quality_flag=StationQualityFlag.OK,
        warning_level_m=seed.warning_level_m,
        critical_level_m=seed.critical_level_m,
        provenance_url=provenance_url,
        license_note=license_note,
        attribution=attribution,
    )


# ---------------------------------------------------------------------------
# Factory used by the FastAPI lifespan
# ---------------------------------------------------------------------------


def build_thaiwater_client(
    *,
    http_client: httpx.AsyncClient,
    base_url: str,
    api_key: str | None,
    max_age_hours: float,
    seeds: Sequence[StationSeed] | None = None,
) -> ThaiwaterStationClient:
    """Construct a :class:`ThaiwaterStationClient` with project defaults.

    Args:
        http_client: Lifespan-managed async HTTP client.
        base_url: ThaiWater public API base URL (from settings).
        api_key: Optional bearer token (from settings).
        max_age_hours: Maximum observation age in hours before it is
            treated as stale and dropped.
        seeds: Override the Phase 1 seed list; defaults to
            :data:`PHASE1_STATION_SEEDS`.

    Returns:
        Configured :class:`ThaiwaterStationClient`.

    Raises:
        ValueError: When ``max_age_hours`` is not positive.
    """
    if max_age_hours <= 0:
        msg = "max_age_hours must be positive"
        raise ValueError(msg)
    return ThaiwaterStationClient(
        http_client=http_client,
        base_url=base_url,
        seeds=tuple(seeds) if seeds is not None else PHASE1_STATION_SEEDS,
        max_age=timedelta(hours=max_age_hours),
        api_key=api_key,
    )


__all__ = [
    "PHASE1_STATION_SEEDS",
    "SONGKHLA_BASIN_CODE",
    "SONGKHLA_PROVINCE_CODE",
    "THAIWATER_ATTRIBUTION",
    "THAIWATER_LICENSE_NOTE",
    "THAIWATER_PROVIDER_NAME",
    "StationGeoPoint",
    "StationObservation",
    "StationObservationClient",
    "StationQualityFlag",
    "StationSeed",
    "StationVariable",
    "ThaiwaterIngestionError",
    "ThaiwaterStationClient",
    "build_thaiwater_client",
]
