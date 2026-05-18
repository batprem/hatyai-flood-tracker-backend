"""Real GFS forecast ingestion client backed by NOAA NOMADS.

This module implements ``GfsForecastProviderClient`` which satisfies the
``ForecastProviderClient`` Protocol from ``app.ingestion.providers``. It fetches
small accumulated-precipitation (APCP) GRIB2 subsets clipped to the configured
Hat Yai/U-Tapao bounding box from NOAA NOMADS and returns ``ProviderFrameArtifact``
records ready for the existing normalizer and repository.

Network calls use ``httpx`` with bounded retries and timeouts. GRIB2 decoding
uses raw ``eccodes`` (the C library wrapper) so this module does not require
xarray/cfgrib. Both the Python ``eccodes`` package and the underlying C
``libeccodes`` runtime must be available on the host (Homebrew ``eccodes`` on
macOS, ``apt`` package ``libeccodes-dev`` or equivalent Nix packages on Linux,
including Railway deployments).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import eccodes
import httpx

from app.ingestion.models import ForecastProvider
from app.ingestion.providers import ProviderFrameArtifact, ProviderRunRef

logger = logging.getLogger(__name__)

# NOMADS filter endpoint for GFS 0.25-degree files. The filter form supports
# variable selection (var_APCP=on) and bounding-box subsetting (subregion=,
# leftlon=, rightlon=, toplat=, bottomlat=) so the response is a tiny GRIB2.
NOMADS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
NOMADS_RUN_DIRECTORY_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"

DEFAULT_GFS_CYCLE_HOURS: tuple[int, ...] = (0, 6, 12, 18)
DEFAULT_GFS_FRESHNESS_THRESHOLD_HOURS = 7
DEFAULT_GFS_LICENSE = (
    "NOAA public data; attribution and redistribution review required before production"
)
DEFAULT_GFS_ATTRIBUTION = "NOAA/NCEP Global Forecast System (GFS)"
DEFAULT_GFS_PRODUCT = "pgrb2.0p25.apcp"
DEFAULT_GFS_MODEL = "gfs"
# How far back to search for an available cycle if the most recent expected
# cycle has not yet been published. Three cycles (~18 hours) keeps the search
# bounded while still covering common provider delays.
RUN_DISCOVERY_LOOKBACK_CYCLES = 3
HTTP_DEFAULT_TIMEOUT_SECONDS = 60.0
HTTP_DEFAULT_RETRIES = 2


class GfsIngestionError(RuntimeError):
    """Raise for recoverable ingestion failures with operator-visible context."""


@dataclass(frozen=True)
class GfsBoundingBox:
    """Describe the lat/lon clip box passed to the NOMADS subregion filter."""

    west: float
    south: float
    east: float
    north: float

    def as_filter_params(self) -> dict[str, str]:
        """Return the NOMADS query-string fragment for this bounding box."""
        return {
            "subregion": "",
            "leftlon": f"{self.west:g}",
            "rightlon": f"{self.east:g}",
            "toplat": f"{self.north:g}",
            "bottomlat": f"{self.south:g}",
        }


HttpClientFactory = Callable[[], httpx.Client]


def _default_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_DEFAULT_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "hatyai-flood-warning/0.1 (+gfs-ingestion)"},
    )


@dataclass(frozen=True)
class GfsForecastProviderClient:
    """Fetch real GFS APCP forecast artifacts from NOAA NOMADS.

    Attributes:
        forecast_hours: Forecast lead-times (hours) to retrieve, e.g. (6, 12).
        bbox: Lat/lon bounding box used for the NOMADS subregion subset.
        cycle_hours: GFS publication cycles in UTC hours of day.
        freshness_threshold_hours: Provider freshness window for run discovery.
        product: Provider product label recorded with each frame.
        model: Model name recorded with each frame.
        license: License/attribution review note stored on every frame.
        attribution: Public attribution string for every frame.
        base_directory_url: Base path used to compose the source URL recorded in provenance.
        filter_url: NOMADS filter endpoint URL used for server-side subsetting.
        retries: Number of additional retries for transient HTTP errors.
        http_client_factory: Callable returning a configured ``httpx.Client``.
    """

    forecast_hours: tuple[int, ...]
    bbox: GfsBoundingBox
    cycle_hours: tuple[int, ...] = DEFAULT_GFS_CYCLE_HOURS
    freshness_threshold_hours: int = DEFAULT_GFS_FRESHNESS_THRESHOLD_HOURS
    product: str = DEFAULT_GFS_PRODUCT
    model: str = DEFAULT_GFS_MODEL
    license: str = DEFAULT_GFS_LICENSE
    attribution: str = DEFAULT_GFS_ATTRIBUTION
    base_directory_url: str = NOMADS_RUN_DIRECTORY_URL
    filter_url: str = NOMADS_FILTER_URL
    retries: int = HTTP_DEFAULT_RETRIES
    http_client_factory: HttpClientFactory = field(default=_default_http_client)

    def discover_latest_run(self, now: datetime) -> ProviderRunRef:
        """Probe NOMADS for the most recent published GFS cycle.

        Walks backward from ``now`` through scheduled cycle hours and HEADs the
        first forecast-hour subset of each candidate cycle. The first cycle that
        responds with HTTP 200 is selected, provided it is still within the
        freshness threshold. This avoids selecting a future or unpublished
        cycle.

        Args:
            now: Current UTC reference time used to identify candidate cycles.

        Returns:
            ProviderRunRef for the most recently published GFS run.

        Raises:
            GfsIngestionError: When no run is available within the freshness
                window after checking ``RUN_DISCOVERY_LOOKBACK_CYCLES`` cycles.
        """
        resolved_now = now.astimezone(UTC)
        first_hour = self.forecast_hours[0]

        with self.http_client_factory() as client:
            for candidate in self._candidate_run_times(resolved_now):
                age_hours = (resolved_now - candidate).total_seconds() / 3600
                if age_hours > self.freshness_threshold_hours:
                    # Stop walking back once we exceed the freshness window so
                    # we never silently return a stale run as the latest.
                    break
                if self._run_available(client, candidate, first_hour):
                    return ProviderRunRef(
                        provider=ForecastProvider.GFS,
                        model=self.model,
                        product=self.product,
                        run_time=candidate,
                        cycle_hours=self.cycle_hours,
                        freshness_threshold_hours=self.freshness_threshold_hours,
                        license=self.license,
                        attribution=self.attribution,
                    )

        msg = (
            "no GFS run available within freshness window "
            f"({self.freshness_threshold_hours}h); checked "
            f"{RUN_DISCOVERY_LOOKBACK_CYCLES} candidate cycles"
        )
        raise GfsIngestionError(msg)

    def fetch_run(self, run_ref: ProviderRunRef) -> list[ProviderFrameArtifact]:
        """Download and decode each forecast hour into a frame artifact.

        Args:
            run_ref: Reference to the forecast run to fetch.

        Returns:
            List of ``ProviderFrameArtifact`` records with APCP window accumulation
            values in mm, ready for the normalizer.

        Raises:
            GfsIngestionError: When ``run_ref.provider`` is not GFS, or when a
                download or decode fails after retries.
        """
        if run_ref.provider is not ForecastProvider.GFS:
            msg = f"GfsForecastProviderClient cannot fetch provider {run_ref.provider}"
            raise GfsIngestionError(msg)

        artifacts: list[ProviderFrameArtifact] = []
        with self.http_client_factory() as client:
            for forecast_hour in self.forecast_hours:
                grib_bytes = self._download_subset(client, run_ref.run_time, forecast_hour)
                decoded = decode_apcp_message(grib_bytes, forecast_hour=forecast_hour)
                artifacts.append(
                    ProviderFrameArtifact(
                        source_url=self._public_source_url(run_ref.run_time, forecast_hour),
                        raw_artifact_ref=self._raw_artifact_ref(run_ref.run_time, forecast_hour),
                        forecast_hour=forecast_hour,
                        accumulation_hours=decoded.accumulation_hours,
                        provider_accumulation_semantics=decoded.semantics,
                        values_mm=decoded.values_mm,
                        grid_width=decoded.width,
                        grid_height=decoded.height,
                        grid_resolution_degrees=decoded.resolution_degrees,
                    )
                )
        return artifacts

    def _candidate_run_times(self, now: datetime) -> list[datetime]:
        """Return scheduled run times not in the future, newest first."""
        sorted_cycles = sorted(self.cycle_hours)
        candidates: list[datetime] = []
        # Walk back through enough days to cover the lookback window.
        for days_back in range(0, 3):
            day = (now - timedelta(days=days_back)).date()
            for cycle_hour in sorted_cycles:
                run_dt = datetime(day.year, day.month, day.day, cycle_hour, tzinfo=UTC)
                if run_dt <= now:
                    candidates.append(run_dt)
        candidates.sort(reverse=True)
        return candidates[:RUN_DISCOVERY_LOOKBACK_CYCLES]

    def _run_available(self, client: httpx.Client, run_time: datetime, forecast_hour: int) -> bool:
        """Return ``True`` if the smallest forecast file for the cycle exists."""
        params = self._filter_params(run_time, forecast_hour)
        url = self.filter_url
        # NOMADS does not honor HEAD on the filter endpoint reliably, so a
        # range-limited GET (Range: bytes=0-15) is the cheapest way to check
        # availability without downloading the whole subset.
        try:
            response = client.get(
                url,
                params=params,
                headers={"Range": "bytes=0-15"},
            )
        except httpx.HTTPError as exc:
            logger.debug("availability probe failed for %s: %s", run_time, exc)
            return False
        if response.status_code in (200, 206):
            head = response.content[:4]
            return head == b"GRIB"
        return False

    def _download_subset(
        self,
        client: httpx.Client,
        run_time: datetime,
        forecast_hour: int,
    ) -> bytes:
        """Download a clipped APCP GRIB2 subset with bounded retries."""
        params = self._filter_params(run_time, forecast_hour)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = client.get(self.filter_url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "GFS download attempt %d/%d failed for run=%s f%03d: %s",
                    attempt + 1,
                    self.retries + 1,
                    run_time.isoformat(),
                    forecast_hour,
                    exc,
                )
                continue
            if response.status_code != 200:
                last_error = GfsIngestionError(
                    f"NOMADS returned HTTP {response.status_code} for f{forecast_hour:03d}"
                )
                logger.warning(
                    "GFS download attempt %d/%d non-200 for run=%s f%03d: %s",
                    attempt + 1,
                    self.retries + 1,
                    run_time.isoformat(),
                    forecast_hour,
                    response.status_code,
                )
                continue
            content = response.content
            if not content.startswith(b"GRIB"):
                last_error = GfsIngestionError(
                    f"NOMADS response for f{forecast_hour:03d} is not a GRIB2 message"
                )
                continue
            return content
        msg = (
            f"failed to download GFS APCP subset for run={run_time.isoformat()} "
            f"f{forecast_hour:03d} after {self.retries + 1} attempts"
        )
        raise GfsIngestionError(msg) from last_error

    def _filter_params(self, run_time: datetime, forecast_hour: int) -> dict[str, str]:
        params: dict[str, str] = {
            "dir": f"/gfs.{run_time.strftime('%Y%m%d')}/{run_time.strftime('%H')}/atmos",
            "file": (f"gfs.t{run_time.strftime('%H')}z.pgrb2.0p25.f{forecast_hour:03d}"),
            "var_APCP": "on",
        }
        params.update(self.bbox.as_filter_params())
        return params

    def _public_source_url(self, run_time: datetime, forecast_hour: int) -> str:
        return (
            f"{self.base_directory_url}/gfs.{run_time.strftime('%Y%m%d')}/"
            f"{run_time.strftime('%H')}/atmos/"
            f"gfs.t{run_time.strftime('%H')}z.pgrb2.0p25.f{forecast_hour:03d}"
        )

    def _raw_artifact_ref(self, run_time: datetime, forecast_hour: int) -> str:
        return (
            f"gfs/{run_time.strftime('%Y%m%d')}/{run_time.strftime('%H')}/"
            f"pgrb2.0p25.apcp/f{forecast_hour:03d}.grib2"
        )


@dataclass(frozen=True)
class DecodedApcpMessage:
    """Hold the values and metadata picked from a GRIB2 APCP message."""

    values_mm: tuple[float, ...]
    width: int
    height: int
    resolution_degrees: float
    accumulation_hours: int
    semantics: str
    units: str


class _EccodesMessage(Protocol):
    """Minimal typed view of an eccodes message used by this module.

    The real ``eccodes`` Python binding exposes ``get`` and ``get_array`` whose
    return types depend on the requested key, so the stubs report ``object``.
    Wrapping the calls in this protocol plus the narrow ``_get_*`` helpers
    below keeps the cast to a concrete type confined to the eccodes boundary.
    """

    def get(self, key: str) -> object:
        """Get a scalar value from the GRIB2 message.

        Args:
            key: Message key to retrieve.

        Returns:
            Scalar value from the GRIB2 message.
        """
        ...

    def get_array(self, key: str) -> object:
        """Get an array value from the GRIB2 message.

        Args:
            key: Message key to retrieve.

        Returns:
            Array value from the GRIB2 message.
        """
        ...


def _get_int(message: _EccodesMessage, key: str) -> int:
    """Return ``message[key]`` as an ``int`` at the eccodes boundary."""
    return int(cast(int, message.get(key)))


def _get_float(message: _EccodesMessage, key: str) -> float:
    """Return ``message[key]`` as a ``float`` at the eccodes boundary."""
    return float(cast(float, message.get(key)))


def _get_str(message: _EccodesMessage, key: str) -> str:
    """Return ``message[key]`` as a ``str`` at the eccodes boundary."""
    return str(message.get(key))


def _get_float_list(message: _EccodesMessage, key: str) -> list[float]:
    """Return ``message[key]`` as a ``list[float]`` at the eccodes boundary.

    The real binding returns a NumPy array exposing ``.tolist()``; we cast to
    a small protocol so the call site stays precisely typed without pulling
    NumPy into the type surface.
    """

    class _SupportsTolist(Protocol):
        def tolist(self) -> list[float]: ...

    return cast(_SupportsTolist, message.get_array(key)).tolist()


@dataclass(frozen=True)
class _ApcpCandidate:
    """Typed view of one APCP accumulation message picked from a GRIB2 file."""

    start_step: int
    end_step: int
    step_range: str
    ni: int
    nj: int
    resolution: float
    units: str
    values: list[float]


def decode_apcp_message(grib_bytes: bytes, forecast_hour: int) -> DecodedApcpMessage:
    """Pick the interval-accumulation APCP message that matches ``forecast_hour``.

    NOMADS GFS APCP files contain one or more APCP records. From f012 onward
    each file contains both an interval record (e.g. ``stepRange=6-12``) and a
    run-total record (e.g. ``stepRange=0-12``). We always select the interval
    record whose ``endStep`` matches the requested forecast hour and whose
    window length is the GFS interval (typically 6 hours). The actual GRIB
    ``stepRange`` string is preserved verbatim in
    ``providerAccumulationSemantics`` so downstream totals never silently mix
    incompatible accumulation periods.

    Args:
        grib_bytes: Raw bytes of one GFS APCP GRIB2 file.
        forecast_hour: Expected forecast end step (hours); used for validation.

    Returns:
        DecodedApcpMessage with window accumulation values in mm.

    Raises:
        GfsIngestionError: When no APCP message is found for the forecast hour,
            units are unexpected, values are negative, or the accumulation window is invalid.
    """
    candidates: list[_ApcpCandidate] = []
    with eccodes.MemoryReader(grib_bytes) as reader:
        for raw_message in reader:
            message = cast(_EccodesMessage, raw_message)
            short_name = _get_str(message, "shortName")
            step_type = _get_str(message, "stepType")
            if short_name not in {"tp", "APCP"}:
                continue
            if step_type != "accum":
                continue
            start_step = _get_int(message, "startStep")
            end_step = _get_int(message, "endStep")
            if end_step != forecast_hour:
                continue
            candidates.append(
                _ApcpCandidate(
                    start_step=start_step,
                    end_step=end_step,
                    step_range=_get_str(message, "stepRange"),
                    ni=_get_int(message, "Ni"),
                    nj=_get_int(message, "Nj"),
                    resolution=_get_float(message, "iDirectionIncrementInDegrees"),
                    units=_get_str(message, "units"),
                    values=_get_float_list(message, "values"),
                )
            )

    if not candidates:
        msg = f"no APCP accumulation message found for forecast hour f{forecast_hour:03d}"
        raise GfsIngestionError(msg)

    # Prefer the shortest window (interval record) when both interval and
    # run-total are present. Then prefer non-zero startStep to avoid the
    # single-message fallback at f006 where 0-6 is the only record.
    candidates.sort(
        key=lambda candidate: (
            candidate.end_step - candidate.start_step,
            -candidate.start_step,
        )
    )
    chosen = candidates[0]

    units = chosen.units
    if units not in {"kg m**-2", "kg m-2", "mm"}:
        msg = (
            "unexpected APCP units; expected kg m**-2 or mm but got "
            f"{units!r} for f{forecast_hour:03d}"
        )
        raise GfsIngestionError(msg)

    # 1 kg/m^2 of liquid water depth = 1 mm; both unit aliases map directly.
    values_mm = tuple(round(value, 4) for value in chosen.values)
    if any(value < 0 for value in values_mm):
        msg = (
            f"APCP message for f{forecast_hour:03d} contained negative values "
            "after unit normalization"
        )
        raise GfsIngestionError(msg)

    accumulation_hours = chosen.end_step - chosen.start_step
    if accumulation_hours <= 0:
        msg = (
            f"APCP message for f{forecast_hour:03d} has non-positive accumulation window: "
            f"{chosen.step_range}"
        )
        raise GfsIngestionError(msg)

    semantics = (
        f"GFS APCP stepRange={chosen.step_range} "
        f"(startStep={chosen.start_step}, endStep={chosen.end_step}, "
        f"units={units})"
    )

    return DecodedApcpMessage(
        values_mm=values_mm,
        width=chosen.ni,
        height=chosen.nj,
        resolution_degrees=chosen.resolution,
        accumulation_hours=accumulation_hours,
        semantics=semantics,
        units=units,
    )


def build_gfs_client(
    forecast_hours: Sequence[int],
    bbox: GfsBoundingBox,
    *,
    cycle_hours: Sequence[int] | None = None,
    freshness_threshold_hours: int | None = None,
    http_client_factory: HttpClientFactory | None = None,
) -> GfsForecastProviderClient:
    """Build a ``GfsForecastProviderClient`` with sensible defaults.

    Args:
        forecast_hours: Forecast lead-times (hours) to retrieve.
        bbox: Lat/lon bounding box for subregion subset.
        cycle_hours: Override GFS cycle hours. Defaults to ``None``.
        freshness_threshold_hours: Override freshness window. Defaults to ``None``.
        http_client_factory: Override HTTP client. Defaults to ``None``.

    Returns:
        Configured ``GfsForecastProviderClient``.

    Raises:
        ValueError: When no forecast hours are provided or any hour is not positive.
    """
    hours = tuple(sorted({int(hour) for hour in forecast_hours}))
    if not hours:
        msg = "at least one forecast hour is required"
        raise ValueError(msg)
    if any(hour <= 0 for hour in hours):
        msg = "forecast hours must be positive integers"
        raise ValueError(msg)

    return GfsForecastProviderClient(
        forecast_hours=hours,
        bbox=bbox,
        cycle_hours=tuple(cycle_hours) if cycle_hours else DEFAULT_GFS_CYCLE_HOURS,
        freshness_threshold_hours=(
            freshness_threshold_hours
            if freshness_threshold_hours is not None
            else DEFAULT_GFS_FRESHNESS_THRESHOLD_HOURS
        ),
        http_client_factory=http_client_factory or _default_http_client,
    )
