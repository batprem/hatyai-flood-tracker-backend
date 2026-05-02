from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.ingestion.models import ForecastProvider


@dataclass(frozen=True)
class ProviderRunRef:
    """Reference a provider model run discovered from provider metadata."""

    provider: ForecastProvider
    model: str
    product: str
    run_time: datetime
    cycle_hours: tuple[int, ...]
    freshness_threshold_hours: int
    license: str
    attribution: str


@dataclass(frozen=True)
class ProviderFrameArtifact:
    """Represent a small fetched forecast artifact before normalization."""

    source_url: str
    raw_artifact_ref: str
    forecast_hour: int
    accumulation_hours: int
    provider_accumulation_semantics: str
    values_mm: tuple[float, ...]
    grid_width: int
    grid_height: int
    grid_resolution_degrees: float


class ForecastProviderClient(Protocol):
    """Discover and fetch forecast model runs."""

    def discover_latest_run(self, now: datetime) -> ProviderRunRef:
        """Return the latest provider run available for ingestion."""
        ...

    def fetch_run(self, run_ref: ProviderRunRef) -> list[ProviderFrameArtifact]:
        """Fetch forecast artifacts for a discovered run."""
        ...


@dataclass(frozen=True)
class FixtureForecastProviderClient:
    """Provide deterministic provider-shaped forecast artifacts for the POC."""

    provider: ForecastProvider
    model: str
    product: str
    cycle_hours: tuple[int, ...]
    freshness_threshold_hours: int
    forecast_hours: tuple[int, ...]
    base_url: str
    license: str
    attribution: str
    provider_accumulation_semantics: str

    def discover_latest_run(self, now: datetime) -> ProviderRunRef:
        """Resolve the latest scheduled run without calling an external service."""
        resolved_now = now.astimezone(UTC)
        cycle_hour = max(
            (hour for hour in self.cycle_hours if hour <= resolved_now.hour),
            default=None,
        )
        run_date = resolved_now.date()
        if cycle_hour is None:
            cycle_hour = self.cycle_hours[-1]
            run_date = (resolved_now - timedelta(days=1)).date()

        run_time = datetime(
            run_date.year,
            run_date.month,
            run_date.day,
            cycle_hour,
            tzinfo=UTC,
        )
        return ProviderRunRef(
            provider=self.provider,
            model=self.model,
            product=self.product,
            run_time=run_time,
            cycle_hours=self.cycle_hours,
            freshness_threshold_hours=self.freshness_threshold_hours,
            license=self.license,
            attribution=self.attribution,
        )

    def fetch_run(self, run_ref: ProviderRunRef) -> list[ProviderFrameArtifact]:
        """Return tiny clipped precipitation grids shaped like provider artifacts."""
        artifacts: list[ProviderFrameArtifact] = []
        for forecast_hour in self.forecast_hours:
            artifacts.append(
                ProviderFrameArtifact(
                    source_url=self._source_url(run_ref, forecast_hour),
                    raw_artifact_ref=self._artifact_ref(run_ref, forecast_hour),
                    forecast_hour=forecast_hour,
                    accumulation_hours=self._accumulation_hours(forecast_hour),
                    provider_accumulation_semantics=self.provider_accumulation_semantics,
                    values_mm=self._fixture_values(forecast_hour),
                    grid_width=2,
                    grid_height=2,
                    grid_resolution_degrees=0.25,
                )
            )
        return artifacts

    def _source_url(self, run_ref: ProviderRunRef, forecast_hour: int) -> str:
        cycle = run_ref.run_time.strftime("%Y%m%d%H")
        return f"{self.base_url}/{cycle}/{self.product}/f{forecast_hour:03d}"

    def _artifact_ref(self, run_ref: ProviderRunRef, forecast_hour: int) -> str:
        cycle = run_ref.run_time.strftime("%Y%m%d/%H")
        return f"{run_ref.provider}/{run_ref.model}/{cycle}/{self.product}/f{forecast_hour:03d}"

    @staticmethod
    def _accumulation_hours(forecast_hour: int) -> int:
        return min(6, forecast_hour) if forecast_hour > 0 else 1

    def _fixture_values(self, forecast_hour: int) -> tuple[float, ...]:
        provider_offset = 1.5 if self.provider is ForecastProvider.ECMWF_OPEN_DATA else 0.0
        base = (forecast_hour / 6) * 4.0 + provider_offset
        return (round(base, 2), round(base + 3.2, 2), round(base + 5.6, 2), round(base + 1.1, 2))


def build_provider_client(
    provider: ForecastProvider,
    forecast_hours: Sequence[int],
    *,
    use_fixtures: bool = False,
) -> ForecastProviderClient:
    """Build a real or fixture-backed provider client for the given provider.

    Args:
        provider: Forecast provider to target.
        forecast_hours: Forecast hours to ingest.
        use_fixtures: When True, return the deterministic fixture client even
            for providers that have a real network-backed client. Use this for
            offline tests and CI.
    """
    hours = tuple(sorted(set(forecast_hours)))
    if not hours:
        msg = "at least one forecast hour is required"
        raise ValueError(msg)

    if provider is ForecastProvider.GFS:
        if use_fixtures:
            return _build_gfs_fixture_client(hours)
        # Import locally so tests and offline workflows do not need eccodes
        # available to import the providers module.
        from app.ingestion.gfs_client import GfsBoundingBox, build_gfs_client
        from app.ingestion.models import Phase1Area

        west, south, east, north = Phase1Area().bbox
        return build_gfs_client(
            forecast_hours=hours,
            bbox=GfsBoundingBox(west=west, south=south, east=east, north=north),
        )

    if provider is ForecastProvider.ECMWF_OPEN_DATA:
        return FixtureForecastProviderClient(
            provider=ForecastProvider.ECMWF_OPEN_DATA,
            model="ifs",
            product="open-data-tp",
            cycle_hours=(0, 12),
            freshness_threshold_hours=13,
            forecast_hours=hours,
            base_url="https://data.ecmwf.int/forecasts",
            license="ECMWF Open Data terms must be reviewed before production public display",
            attribution="ECMWF Open Data",
            provider_accumulation_semantics=(
                "POC treats total precipitation as an accumulation over the previous "
                "forecast interval; "
                "real client must preserve ECMWF step metadata."
            ),
        )

    msg = f"unsupported forecast provider: {provider}"
    raise ValueError(msg)


def _build_gfs_fixture_client(
    forecast_hours: tuple[int, ...],
) -> FixtureForecastProviderClient:
    """Return the deterministic GFS fixture client for offline tests and CI."""
    return FixtureForecastProviderClient(
        provider=ForecastProvider.GFS,
        model="gfs",
        product="pgrb2.0p25.apcp",
        cycle_hours=(0, 6, 12, 18),
        freshness_threshold_hours=7,
        forecast_hours=forecast_hours,
        base_url="https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod",
        license=(
            "NOAA public data; attribution and redistribution review required before production"
        ),
        attribution="NOAA/NCEP Global Forecast System (GFS)",
        provider_accumulation_semantics=(
            "POC treats APCP as an accumulation over the previous forecast interval; "
            "real client must preserve GRIB step range semantics."
        ),
    )
