"""Real ECMWF Open Data ingestion client for IFS total precipitation (tp).

This module implements ``EcmwfOpenDataProviderClient`` which satisfies the
``ForecastProviderClient`` Protocol from ``app.ingestion.providers``. It fetches
IFS ``tp`` GRIB2 files from the ECMWF Open Data CDN, clips them to the Phase 1
bounding box, converts metres to mm, and computes per-window rainfall from
the run-accumulated ECMWF ``tp`` field.

**ECMWF tp accumulation semantics differ from GFS APCP.**

GFS APCP is a step-interval accumulation: each file contains the precipitation
only for that 6-hour window (e.g. f006 = 0–6 h, f012 = 6–12 h).

ECMWF ``tp`` is a run-total accumulation: each file contains precipitation
accumulated from the start of the model run (T+0) to the forecast step. This
means:

- f006 raw value = total mm from T+0 to T+6 h
- f012 raw value = total mm from T+0 to T+12 h
- window rainfall for 6–12 h = tp[f012] - tp[f006]
- window rainfall for 0–6 h  = tp[f006] (no prior step to subtract)

The window derivation is performed inside ``fetch_run`` so that downstream
normalizer and risk code never receive run-total values. Each
``ProviderFrameArtifact`` carries only the window accumulation in mm together
with a verbatim ``provider_accumulation_semantics`` string that records the
step range used for the derivation.

Attribution and license follow the ECMWF Open Data terms:
    https://apps.ecmwf.int/datasets/licences/general/
License: Creative Commons Attribution 4.0 International (CC BY 4.0)
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

# Public CDN for ECMWF Open Data (IFS real-time forecasts at 0.25 degree).
# URL pattern: {BASE}/{YYYYMMDD}/{HH}z/ifs/0p25/oper/{YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2
ECMWF_OPEN_DATA_BASE_URL = "https://data.ecmwf.int/forecasts"

# IFS runs twice a day. ECMWF publishes results roughly 9-12 hours after the
# nominal run time; we use a 13-hour freshness window to allow for this delay.
DEFAULT_ECMWF_CYCLE_HOURS: tuple[int, ...] = (0, 12)
DEFAULT_ECMWF_FRESHNESS_THRESHOLD_HOURS = 13
DEFAULT_ECMWF_LICENSE = "CC-BY-4.0"
DEFAULT_ECMWF_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
# Per docs/data-sources.md ("License Notes" production gate), each provider
# record must carry attribution, license/terms URL, and a redistribution and
# caching decision. ECMWF Open Data permits redistribution of derived rainfall
# maps when CC-BY-4.0 attribution is preserved; this string records that
# decision so the production gate has an auditable provenance trail.
DEFAULT_ECMWF_REDISTRIBUTION_NOTE = (
    "Redistribution of derived rainfall maps permitted under CC-BY-4.0 with "
    "attribution to 'ECMWF Open Data — IFS Forecast' and a link to the license; "
    "caching of clipped subsets for the Hat Yai/U-Tapao basin permitted. "
    "Refer to https://apps.ecmwf.int/datasets/licences/general/ for the full terms."
)
DEFAULT_ECMWF_ATTRIBUTION = "ECMWF Open Data — IFS Forecast"
DEFAULT_ECMWF_PRODUCT = "ifs/0p25/oper"
DEFAULT_ECMWF_MODEL = "ifs"

# How many candidate cycles to probe when the most-recent cycle is not yet
# published (covers typical ECMWF dissemination delays).
RUN_DISCOVERY_LOOKBACK_CYCLES = 3
HTTP_DEFAULT_TIMEOUT_SECONDS = 120.0
HTTP_DEFAULT_RETRIES = 2


class EcmwfIngestionError(RuntimeError):
    """Raise for recoverable ECMWF ingestion failures with operator-visible context."""


@dataclass(frozen=True)
class EcmwfBoundingBox:
    """Describe the lat/lon clip box applied to ECMWF GRIB2 messages after download.

    ECMWF Open Data does not offer a server-side filter endpoint like NOMADS.
    We download the full 0.25-degree global file and clip to the bbox in memory
    using the GRIB2 grid lat/lon coordinates.
    """

    west: float
    south: float
    east: float
    north: float

    def contains(self, lat: float, lon: float) -> bool:
        """Return True if the point falls within this bounding box.

        Args:
            lat (float): Latitude in degrees.
            lon (float): Longitude in degrees.

        Returns:
            ``True`` when the point lies inside the bbox (inclusive), else ``False``.
        """
        return self.south <= lat <= self.north and self.west <= lon <= self.east


HttpClientFactory = Callable[[], httpx.Client]


def _default_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_DEFAULT_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "hatyai-flood-warning/0.1 (+ecmwf-ingestion)"},
    )


@dataclass(frozen=True)
class EcmwfOpenDataProviderClient:
    """Fetch real ECMWF IFS tp forecast artifacts from the ECMWF Open Data CDN.

    ECMWF ``tp`` (total precipitation) is published as accumulated values from
    the model run start (T+0). This client converts them to per-window mm
    values by computing the difference between consecutive steps before
    returning ``ProviderFrameArtifact`` records. See module docstring for
    accumulation semantics.

    Attributes:
        forecast_hours: Forecast lead-times (hours) to retrieve, e.g. (6, 12).
        bbox: Lat/lon bounding box for in-memory clipping after download.
        cycle_hours: IFS publication cycles in UTC hours of day (0, 12).
        freshness_threshold_hours: Provider freshness window for run discovery.
        product: Provider product label recorded with each frame.
        model: Model name recorded with each frame.
        license: SPDX license identifier for the data (CC-BY-4.0).
        license_url: URL of the license text.
        attribution: Public attribution string for every frame.
        base_url: Base CDN URL; replaceable in tests.
        retries: Number of additional retries for transient HTTP errors.
        http_client_factory: Callable returning a configured ``httpx.Client``.
    """

    forecast_hours: tuple[int, ...]
    bbox: EcmwfBoundingBox
    cycle_hours: tuple[int, ...] = DEFAULT_ECMWF_CYCLE_HOURS
    freshness_threshold_hours: int = DEFAULT_ECMWF_FRESHNESS_THRESHOLD_HOURS
    product: str = DEFAULT_ECMWF_PRODUCT
    model: str = DEFAULT_ECMWF_MODEL
    license: str = DEFAULT_ECMWF_LICENSE
    license_url: str = DEFAULT_ECMWF_LICENSE_URL
    redistribution_note: str = DEFAULT_ECMWF_REDISTRIBUTION_NOTE
    attribution: str = DEFAULT_ECMWF_ATTRIBUTION
    base_url: str = ECMWF_OPEN_DATA_BASE_URL
    retries: int = HTTP_DEFAULT_RETRIES
    http_client_factory: HttpClientFactory = field(default=_default_http_client)

    def discover_latest_run(self, now: datetime) -> ProviderRunRef:
        """Probe the ECMWF CDN for the most recent published IFS 0.25-degree cycle.

        Walks backward from ``now`` through scheduled 00Z/12Z run times and
        HEADs the file for the first forecast hour. The first run that responds
        with HTTP 200 and a valid GRIB2 header is selected, provided it is
        within the freshness threshold.

        Args:
            now: Current UTC reference time used to identify candidate cycles.

        Returns:
            ProviderRunRef for the most recently published run.

        Raises:
            EcmwfIngestionError: When no run is available within the freshness
                window after checking ``RUN_DISCOVERY_LOOKBACK_CYCLES`` cycles.
        """
        resolved_now = now.astimezone(UTC)
        first_hour = self.forecast_hours[0]

        with self.http_client_factory() as client:
            for candidate in self._candidate_run_times(resolved_now):
                age_hours = (resolved_now - candidate).total_seconds() / 3600
                if age_hours > self.freshness_threshold_hours:
                    break
                if self._run_available(client, candidate, first_hour):
                    return ProviderRunRef(
                        provider=ForecastProvider.ECMWF_OPEN_DATA,
                        model=self.model,
                        product=self.product,
                        run_time=candidate,
                        cycle_hours=self.cycle_hours,
                        freshness_threshold_hours=self.freshness_threshold_hours,
                        license=self.license,
                        attribution=self.attribution,
                        license_url=self.license_url,
                        redistribution_note=self.redistribution_note,
                    )

        msg = (
            "no ECMWF Open Data run available within freshness window "
            f"({self.freshness_threshold_hours}h); checked "
            f"{RUN_DISCOVERY_LOOKBACK_CYCLES} candidate cycles"
        )
        raise EcmwfIngestionError(msg)

    def fetch_run(self, run_ref: ProviderRunRef) -> list[ProviderFrameArtifact]:
        """Download and decode each forecast hour into window-accumulation artifacts.

        ECMWF ``tp`` values are run-accumulated from T+0. This method downloads
        each requested step, stores the raw run-total grids in metres, then
        computes the per-window accumulation:

        - Step 0 (first step): window = tp[step_0] (T+0 baseline is 0)
        - Step N (subsequent): window = tp[step_N] - tp[step_N-1]

        The result is converted to mm (multiply by 1000) and negative values
        (possible floating-point rounding artefacts) are clamped to zero.

        Args:
            run_ref: The run reference returned by ``discover_latest_run``.

        Returns:
            List of ``ProviderFrameArtifact`` records with window-accumulation
            values in mm, ready for the normalizer.

        Raises:
            EcmwfIngestionError: When ``run_ref.provider`` is not
                ``ECMWF_OPEN_DATA``, or when a download fails after retries.
        """
        if run_ref.provider is not ForecastProvider.ECMWF_OPEN_DATA:
            msg = f"EcmwfOpenDataProviderClient cannot fetch provider {run_ref.provider}"
            raise EcmwfIngestionError(msg)

        # Download run-total grids for all requested steps.
        run_totals: dict[int, _EcmwfDecodedMessage] = {}
        with self.http_client_factory() as client:
            for forecast_hour in self.forecast_hours:
                grib_bytes = self._download_file(client, run_ref.run_time, forecast_hour)
                decoded = decode_tp_message(grib_bytes, forecast_hour=forecast_hour, bbox=self.bbox)
                run_totals[forecast_hour] = decoded

        # Derive per-window accumulations from the run-total grids.
        artifacts: list[ProviderFrameArtifact] = []
        sorted_hours = sorted(run_totals)
        for idx, forecast_hour in enumerate(sorted_hours):
            decoded = run_totals[forecast_hour]
            if idx == 0:
                # First requested step: window starts at T+0 (run total = window total).
                prior_step = 0
                prior_values_m = tuple(0.0 for _ in decoded.values_m)
            else:
                prior_hour = sorted_hours[idx - 1]
                prior_step = prior_hour
                prior_values_m = run_totals[prior_hour].values_m

            window_values_mm = tuple(
                max(0.0, round((curr - prior) * 1000.0, 4))
                for curr, prior in zip(decoded.values_m, prior_values_m, strict=True)
            )
            accumulation_hours = forecast_hour - prior_step

            semantics = (
                f"ECMWF IFS tp run-accumulated; "
                f"window derived as tp[step={forecast_hour}h] - tp[step={prior_step}h]; "
                f"raw stepRange={decoded.step_range}; units=m (converted to mm)"
            )

            artifacts.append(
                ProviderFrameArtifact(
                    source_url=self._public_source_url(run_ref.run_time, forecast_hour),
                    raw_artifact_ref=self._raw_artifact_ref(run_ref.run_time, forecast_hour),
                    forecast_hour=forecast_hour,
                    accumulation_hours=accumulation_hours,
                    provider_accumulation_semantics=semantics,
                    values_mm=window_values_mm,
                    grid_width=decoded.width,
                    grid_height=decoded.height,
                    grid_resolution_degrees=decoded.resolution_degrees,
                )
            )

        return artifacts

    def _candidate_run_times(self, now: datetime) -> list[datetime]:
        """Return scheduled 00Z/12Z run times not in the future, newest first."""
        sorted_cycles = sorted(self.cycle_hours)
        candidates: list[datetime] = []
        for days_back in range(3):
            day = (now - timedelta(days=days_back)).date()
            for cycle_hour in sorted_cycles:
                run_dt = datetime(day.year, day.month, day.day, cycle_hour, tzinfo=UTC)
                if run_dt <= now:
                    candidates.append(run_dt)
        candidates.sort(reverse=True)
        return candidates[:RUN_DISCOVERY_LOOKBACK_CYCLES]

    def _run_available(self, client: httpx.Client, run_time: datetime, forecast_hour: int) -> bool:
        """Return True if the GRIB2 file for the cycle exists on the CDN.

        Uses a range-limited GET (first 16 bytes) to check for the GRIB magic
        header without downloading the full file.
        """
        url = self._file_url(run_time, forecast_hour)
        try:
            response = client.get(url, headers={"Range": "bytes=0-15"})
        except httpx.HTTPError as exc:
            logger.debug(
                "ECMWF availability probe failed for %s f%03d: %s", run_time, forecast_hour, exc
            )
            return False
        if response.status_code in (200, 206):
            return response.content[:4] == b"GRIB"
        return False

    def _download_file(
        self,
        client: httpx.Client,
        run_time: datetime,
        forecast_hour: int,
    ) -> bytes:
        """Download a full ECMWF tp GRIB2 file with bounded retries.

        ECMWF Open Data does not support server-side bbox filtering, so we
        download the complete global file and clip in-memory inside
        ``decode_tp_message``. Files are small relative to GFS global files
        because ECMWF publishes clipped regional products alongside the global
        ones. For 0.25-degree global tp at one step the file is typically 1–3 MB.
        """
        url = self._file_url(run_time, forecast_hour)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "ECMWF download attempt %d/%d failed for run=%s f%03d: %s",
                    attempt + 1,
                    self.retries + 1,
                    run_time.isoformat(),
                    forecast_hour,
                    exc,
                )
                continue
            if response.status_code != 200:
                last_error = EcmwfIngestionError(
                    f"ECMWF CDN returned HTTP {response.status_code} for f{forecast_hour:03d}"
                )
                logger.warning(
                    "ECMWF download attempt %d/%d non-200 for run=%s f%03d: HTTP %d",
                    attempt + 1,
                    self.retries + 1,
                    run_time.isoformat(),
                    forecast_hour,
                    response.status_code,
                )
                continue
            content = response.content
            if not content.startswith(b"GRIB"):
                last_error = EcmwfIngestionError(
                    f"ECMWF response for f{forecast_hour:03d} is not a GRIB2 message"
                )
                continue
            return content
        msg = (
            f"failed to download ECMWF tp for run={run_time.isoformat()} "
            f"f{forecast_hour:03d} after {self.retries + 1} attempts"
        )
        raise EcmwfIngestionError(msg) from last_error

    def _file_url(self, run_time: datetime, forecast_hour: int) -> str:
        date_str = run_time.strftime("%Y%m%d")
        hour_str = run_time.strftime("%H")
        filename = f"{date_str}{hour_str}0000-{forecast_hour}h-oper-fc.grib2"
        return f"{self.base_url}/{date_str}/{hour_str}z/ifs/0p25/oper/{filename}"

    def _public_source_url(self, run_time: datetime, forecast_hour: int) -> str:
        return self._file_url(run_time, forecast_hour)

    def _raw_artifact_ref(self, run_time: datetime, forecast_hour: int) -> str:
        date_str = run_time.strftime("%Y%m%d")
        hour_str = run_time.strftime("%H")
        return f"ecmwf_open_data/ifs/{date_str}/{hour_str}/tp/f{forecast_hour:03d}.grib2"


@dataclass(frozen=True)
class _EcmwfDecodedMessage:
    """Hold values and metadata from a decoded ECMWF tp GRIB2 message."""

    values_m: tuple[float, ...]
    width: int
    height: int
    resolution_degrees: float
    step_range: str
    start_step: int
    end_step: int
    units: str


class _EccodesMessage(Protocol):
    """Minimal typed view of an eccodes message."""

    def get(self, key: str) -> object:
        """Get a scalar value from the GRIB2 message.

        Args:
            key (str): Message key to retrieve.

        Returns:
            The raw value associated with ``key``; type depends on the message.
        """
        ...

    def get_array(self, key: str) -> object:
        """Get an array value from the GRIB2 message.

        Args:
            key (str): Message key to retrieve.

        Returns:
            The raw array (typically a numpy ndarray) associated with ``key``.
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
    """Return ``message[key]`` as a ``list[float]`` at the eccodes boundary."""

    class _SupportsTolist(Protocol):
        def tolist(self) -> list[float]: ...

    return cast(_SupportsTolist, message.get_array(key)).tolist()


def _get_latlons(message: _EccodesMessage) -> tuple[list[float], list[float]]:
    """Return (latitudes, longitudes) arrays from a GRIB2 message."""

    class _SupportsTolist(Protocol):
        def tolist(self) -> list[float]: ...

    lats = cast(_SupportsTolist, message.get_array("latitudes")).tolist()
    lons = cast(_SupportsTolist, message.get_array("longitudes")).tolist()
    return lats, lons


def _infer_grid_shape(
    lats: list[float],
    lons: list[float],
    *,
    resolution: float,
) -> tuple[int, int]:
    """Infer (width, height) for a clipped regular lat/lon scan.

    ECMWF Open Data IFS 0.25-degree files are stored on a regular lat/lon
    grid. After clipping to a bounding box, ``width`` equals the number of
    unique longitudes and ``height`` equals the number of unique latitudes.
    Coordinate equality is matched at half the grid resolution to absorb
    floating-point noise in the raw GRIB coordinate arrays.

    Raises:
        EcmwfIngestionError: When the clipped point count does not equal
            width * height. That mismatch indicates the assumption of a
            regular scan no longer holds and downstream consumers cannot
            treat the values as a 2D grid.
    """
    tolerance = max(resolution / 2.0, 1e-6)
    unique_lats = _unique_coords(lats, tolerance=tolerance)
    unique_lons = _unique_coords(lons, tolerance=tolerance)
    width = len(unique_lons)
    height = len(unique_lats)
    expected = width * height
    if expected != len(lats):
        msg = (
            "clipped ECMWF tp grid is not a regular rectangular scan: "
            f"unique_lons={width}, unique_lats={height}, "
            f"points={len(lats)} (expected {expected}); "
            "downstream consumers assume 2D grid dimensions"
        )
        raise EcmwfIngestionError(msg)
    return width, height


def _unique_coords(values: list[float], *, tolerance: float) -> list[float]:
    """Return sorted unique coordinates collapsing points within ``tolerance``."""
    if not values:
        return []
    sorted_values = sorted(values)
    unique: list[float] = [sorted_values[0]]
    for value in sorted_values[1:]:
        if abs(value - unique[-1]) > tolerance:
            unique.append(value)
    return unique


def decode_tp_message(
    grib_bytes: bytes,
    forecast_hour: int,
    bbox: EcmwfBoundingBox | None = None,
) -> _EcmwfDecodedMessage:
    """Decode the tp accumulation message for ``forecast_hour`` from GRIB2 bytes.

    ECMWF IFS ``tp`` files contain a single accumulated-from-run-start message
    per file (stepRange = ``0-{forecast_hour}``). The function validates the
    step metadata and, when a ``bbox`` is supplied, clips the grid to only the
    cells whose lat/lon falls inside the box.

    Args:
        grib_bytes: Raw bytes of one ECMWF IFS tp GRIB2 file.
        forecast_hour: Expected forecast end step (hours); used for validation.
        bbox: Optional clipping box; when None the full grid is returned.

    Returns:
        _EcmwfDecodedMessage with run-total values in metres.

    Raises:
        EcmwfIngestionError: When no tp accumulation message is found for the
            given forecast hour, units are not metres, or the grid is empty
            after clipping.
    """

    @dataclass
    class _TpCandidate:
        start_step: int
        end_step: int
        step_range: str
        ni: int
        nj: int
        resolution: float
        units: str
        all_values: list[float]
        all_lats: list[float]
        all_lons: list[float]

    candidates: list[_TpCandidate] = []
    with eccodes.MemoryReader(grib_bytes) as reader:
        for raw_message in reader:
            message = cast(_EccodesMessage, raw_message)
            short_name = _get_str(message, "shortName")
            step_type = _get_str(message, "stepType")
            if short_name != "tp":
                continue
            if step_type != "accum":
                continue
            end_step = _get_int(message, "endStep")
            if end_step != forecast_hour:
                continue
            lats, lons = _get_latlons(message)
            candidates.append(
                _TpCandidate(
                    start_step=_get_int(message, "startStep"),
                    end_step=end_step,
                    step_range=_get_str(message, "stepRange"),
                    ni=_get_int(message, "Ni"),
                    nj=_get_int(message, "Nj"),
                    resolution=_get_float(message, "iDirectionIncrementInDegrees"),
                    units=_get_str(message, "units"),
                    all_values=_get_float_list(message, "values"),
                    all_lats=lats,
                    all_lons=lons,
                )
            )

    if not candidates:
        msg = f"no ECMWF tp accumulation message found for forecast hour f{forecast_hour:03d}"
        raise EcmwfIngestionError(msg)

    # ECMWF IFS tp files have exactly one tp message per file. If multiple
    # are present, pick the one with startStep=0 (run-total from T+0).
    candidates.sort(key=lambda c: c.start_step)
    chosen = candidates[0]
    start_step = chosen.start_step
    end_step = chosen.end_step
    step_range = chosen.step_range
    ni = chosen.ni
    nj = chosen.nj
    resolution = chosen.resolution
    units = chosen.units
    all_values = chosen.all_values
    all_lats = chosen.all_lats
    all_lons = chosen.all_lons

    if units != "m":
        msg = (
            f"unexpected ECMWF tp units; expected 'm' (metres) but got {units!r} "
            f"for f{forecast_hour:03d}"
        )
        raise EcmwfIngestionError(msg)

    # Clip to bbox when provided.
    if bbox is not None:
        clipped_values: list[float] = []
        clipped_lats: list[float] = []
        clipped_lons: list[float] = []
        for v, lat, lon in zip(all_values, all_lats, all_lons, strict=True):
            if bbox.contains(lat, lon):
                clipped_values.append(v)
                clipped_lats.append(lat)
                clipped_lons.append(lon)
        if not clipped_values:
            msg = (
                f"no grid points remain after clipping to bbox "
                f"[{bbox.west},{bbox.south},{bbox.east},{bbox.north}] "
                f"for f{forecast_hour:03d}"
            )
            raise EcmwfIngestionError(msg)
        values_m = tuple(round(v, 7) for v in clipped_values)
        clipped_width, clipped_height = _infer_grid_shape(
            clipped_lats, clipped_lons, resolution=resolution
        )
    else:
        values_m = tuple(round(v, 7) for v in all_values)
        clipped_width = ni
        clipped_height = nj

    if any(v < 0 for v in values_m):
        msg = (
            f"ECMWF tp message for f{forecast_hour:03d} contained negative values "
            "before window derivation (source data issue)"
        )
        raise EcmwfIngestionError(msg)

    return _EcmwfDecodedMessage(
        values_m=values_m,
        width=clipped_width,
        height=clipped_height,
        resolution_degrees=resolution,
        step_range=step_range,
        start_step=start_step,
        end_step=end_step,
        units=units,
    )


def build_ecmwf_client(
    forecast_hours: Sequence[int],
    bbox: EcmwfBoundingBox,
    *,
    cycle_hours: Sequence[int] | None = None,
    freshness_threshold_hours: int | None = None,
    base_url: str | None = None,
    http_client_factory: HttpClientFactory | None = None,
    license_identifier: str | None = None,
    license_url: str | None = None,
    redistribution_note: str | None = None,
    attribution: str | None = None,
) -> EcmwfOpenDataProviderClient:
    """Build an ``EcmwfOpenDataProviderClient`` with sensible defaults.

    Args:
        forecast_hours: Forecast lead-times (hours) to retrieve.
        bbox: Lat/lon clipping box applied after download.
        cycle_hours: Override IFS cycle hours; default is (0, 12).
        freshness_threshold_hours: Override freshness window; default is 13.
        base_url: Override CDN base URL (useful for tests).
        http_client_factory: Override the HTTP client factory.
        license_identifier: Override the short SPDX/terms identifier
            recorded on every frame and run.
        license_url: Override the license terms URL recorded on every frame
            and run (production gate requirement).
        redistribution_note: Override the redistribution/caching decision
            string recorded on every frame and run (production gate
            requirement).
        attribution: Override the public attribution string.

    Returns:
        Configured ``EcmwfOpenDataProviderClient``.

    Raises:
        ValueError: When no forecast hours are provided or any hour is not positive.
    """
    hours = tuple(sorted({int(h) for h in forecast_hours}))
    if not hours:
        msg = "at least one forecast hour is required"
        raise ValueError(msg)
    if any(h <= 0 for h in hours):
        msg = "forecast hours must be positive integers"
        raise ValueError(msg)

    return EcmwfOpenDataProviderClient(
        forecast_hours=hours,
        bbox=bbox,
        cycle_hours=tuple(cycle_hours) if cycle_hours else DEFAULT_ECMWF_CYCLE_HOURS,
        freshness_threshold_hours=(
            freshness_threshold_hours
            if freshness_threshold_hours is not None
            else DEFAULT_ECMWF_FRESHNESS_THRESHOLD_HOURS
        ),
        license=license_identifier or DEFAULT_ECMWF_LICENSE,
        license_url=license_url or DEFAULT_ECMWF_LICENSE_URL,
        redistribution_note=redistribution_note or DEFAULT_ECMWF_REDISTRIBUTION_NOTE,
        attribution=attribution or DEFAULT_ECMWF_ATTRIBUTION,
        base_url=base_url or ECMWF_OPEN_DATA_BASE_URL,
        http_client_factory=http_client_factory or _default_http_client,
    )
