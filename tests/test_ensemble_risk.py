from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, TestCase

from app.ingestion.models import (
    ForecastFrame,
    ForecastGrid,
    ForecastProvider,
    ForecastQuality,
    ForecastRunStatus,
    ForecastSource,
    ForecastStatistic,
    ForecastVariable,
    Phase1Area,
)
from app.ingestion.repository import DryRunForecastRepository
from app.schemas.common import RiskLevel
from app.schemas.risk import RiskAvailability, RiskFreshnessStatus
from app.services.risk_rules import (
    RainfallThreshold,
    RiskRuleSettings,
    combine_ensemble_risk,
    compute_provider_risk,
)

GFS = ForecastProvider.GFS.value
ECMWF = ForecastProvider.ECMWF_OPEN_DATA.value

# Bounding box whose 2x2 0.25-degree grid cells all fall inside the committed
# U-Tapao basin polygon, so frame-driven fixtures survive basin clipping.
_BASIN_INSIDE_BBOX: tuple[float, float, float, float] = (100.30, 6.70, 100.55, 6.95)


def _settings() -> RiskRuleSettings:
    return RiskRuleSettings(
        rainfall_thresholds={
            6: RainfallThreshold(window_hours=6, yellow_mm=60, orange_mm=100, red_mm=150),
            24: RainfallThreshold(window_hours=24, yellow_mm=80, orange_mm=130, red_mm=200),
        },
        fresh_run_max_age_hours=12,
        aging_run_max_age_hours=18,
        expected_forecast_cells=2,
        expected_water_stations=1,
        minimum_coverage_ratio=0.6,
        water_watch_ratio=0.8,
        water_station_max_age_hours=3,
        allow_mock_water_level_raise=False,
    )


def _frame(
    *,
    provider: ForecastProvider,
    run_time: datetime,
    accumulation_hours: int,
    forecast_hour: int,
    values_mm: list[float],
) -> ForecastFrame:
    model = "gfs" if provider is ForecastProvider.GFS else "ifs"
    valid_time = run_time + timedelta(hours=forecast_hour)
    window_start = valid_time - timedelta(hours=accumulation_hours)
    return ForecastFrame(
        frame_id=f"{provider.value}:{model}:{run_time:%Y%m%d%H}:precipitation:f{forecast_hour:03d}",
        run_id=f"{provider.value}:{model}:{run_time:%Y%m%d%H}",
        provider=provider,
        model=model,
        variable=ForecastVariable.PRECIPITATION,
        statistic=ForecastStatistic.ACCUMULATION,
        unit="mm",
        run_time=run_time,
        valid_time=valid_time,
        window_start=window_start,
        window_end=valid_time,
        accumulation_hours=accumulation_hours,
        provider_accumulation_semantics="window_accumulation_mm",
        forecast_hour=forecast_hour,
        retrieved_at=run_time + timedelta(minutes=30),
        processed_at=run_time + timedelta(minutes=30),
        area=Phase1Area(bbox=_BASIN_INSIDE_BBOX),
        grid=ForecastGrid(resolution_degrees=0.25, width=2, height=2),
        values_mm=values_mm,
        source=ForecastSource(
            url="https://example.test/fixture",
            product=f"{provider.value}.fixture",
            license="public-domain",
            attribution=f"{provider.value} fixture",
            raw_artifact_ref=f"fixture://{provider.value}",
        ),
        quality=ForecastQuality(
            status=ForecastRunStatus.NORMALIZED,
            missing_value_count=0,
            minimum_mm=min(values_mm),
            maximum_mm=max(values_mm),
        ),
    )


def _provider_frames(
    *,
    provider: ForecastProvider,
    run_time: datetime,
    peak_mm: float,
) -> list[ForecastFrame]:
    return [
        _frame(
            provider=provider,
            run_time=run_time,
            accumulation_hours=24,
            forecast_hour=24,
            values_mm=[10.0, peak_mm, 5.0, 1.0],
        )
    ]


def _combine(
    frames_by_provider: dict[str, Sequence[ForecastFrame]],
    generated_at: datetime,
):
    return combine_ensemble_risk(
        frames_by_provider=frames_by_provider,
        providers=(GFS, ECMWF),
        water_levels=[],
        settings=_settings(),
        generated_at=generated_at,
    )


def _by_provider(response) -> dict[str, RiskLevel]:
    return {result.provider: result.computed_risk_level for result in response.providers}


class EnsembleCombinationTest(TestCase):
    def test_both_fresh_same_level_combines_without_warning(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        run_time = generated_at - timedelta(hours=2)
        response = _combine(
            {
                GFS: _provider_frames(provider=ForecastProvider.GFS, run_time=run_time, peak_mm=90),
                ECMWF: _provider_frames(
                    provider=ForecastProvider.ECMWF_OPEN_DATA, run_time=run_time, peak_mm=90
                ),
            },
            generated_at,
        )

        self.assertEqual(response.computed_level, RiskLevel.YELLOW)
        self.assertFalse(response.single_provider_warning)
        self.assertEqual(len(response.providers), 2)
        levels = _by_provider(response)
        self.assertEqual(levels[GFS], RiskLevel.YELLOW)
        self.assertEqual(levels[ECMWF], RiskLevel.YELLOW)

    def test_max_wins_when_providers_disagree(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        run_time = generated_at - timedelta(hours=2)
        response = _combine(
            {
                # 24h >= 130mm -> orange for GFS, >= 80mm -> yellow for ECMWF.
                GFS: _provider_frames(
                    provider=ForecastProvider.GFS, run_time=run_time, peak_mm=140
                ),
                ECMWF: _provider_frames(
                    provider=ForecastProvider.ECMWF_OPEN_DATA, run_time=run_time, peak_mm=90
                ),
            },
            generated_at,
        )

        self.assertEqual(response.computed_level, RiskLevel.ORANGE)
        self.assertFalse(response.single_provider_warning)
        levels = _by_provider(response)
        self.assertEqual(levels[GFS], RiskLevel.ORANGE)
        self.assertEqual(levels[ECMWF], RiskLevel.YELLOW)

    def test_one_stale_uses_fresh_with_single_provider_warning(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        fresh_run = generated_at - timedelta(hours=2)
        stale_run = generated_at - timedelta(hours=30)
        response = _combine(
            {
                # GFS is stale (well past aging threshold) so it must not contribute.
                GFS: _provider_frames(
                    provider=ForecastProvider.GFS, run_time=stale_run, peak_mm=200
                ),
                ECMWF: _provider_frames(
                    provider=ForecastProvider.ECMWF_OPEN_DATA, run_time=fresh_run, peak_mm=90
                ),
            },
            generated_at,
        )

        self.assertTrue(response.single_provider_warning)
        # ECMWF (yellow) drives the level, not the stale GFS red signal.
        self.assertEqual(response.computed_level, RiskLevel.YELLOW)
        results = {result.provider: result for result in response.providers}
        self.assertEqual(results[GFS].freshness_status, RiskFreshnessStatus.STALE)
        self.assertEqual(results[ECMWF].freshness_status, RiskFreshnessStatus.FRESH)

    def test_both_stale_is_unavailable_without_warning(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        stale_run = generated_at - timedelta(hours=30)
        response = _combine(
            {
                GFS: _provider_frames(
                    provider=ForecastProvider.GFS, run_time=stale_run, peak_mm=200
                ),
                ECMWF: _provider_frames(
                    provider=ForecastProvider.ECMWF_OPEN_DATA, run_time=stale_run, peak_mm=200
                ),
            },
            generated_at,
        )

        self.assertEqual(response.availability, RiskAvailability.UNAVAILABLE)
        self.assertNotEqual(response.level, RiskLevel.GREEN)
        self.assertFalse(response.single_provider_warning)
        self.assertIsNone(response.computed_level)

    def test_zero_frames_provider_is_failed_and_not_contributing(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        run_time = generated_at - timedelta(hours=2)
        response = _combine(
            {
                GFS: [],
                ECMWF: _provider_frames(
                    provider=ForecastProvider.ECMWF_OPEN_DATA, run_time=run_time, peak_mm=140
                ),
            },
            generated_at,
        )

        self.assertTrue(response.single_provider_warning)
        self.assertEqual(response.computed_level, RiskLevel.ORANGE)
        results = {result.provider: result for result in response.providers}
        self.assertEqual(results[GFS].freshness_status, RiskFreshnessStatus.FAILED)
        self.assertEqual(results[GFS].computed_risk_level, RiskLevel.GREEN)
        self.assertEqual(results[GFS].frame_count, 0)
        self.assertIsNone(results[GFS].model_run_time)


class ComputeProviderRiskTest(TestCase):
    def test_dominant_window_reflects_highest_scoring_window(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        run_time = generated_at - timedelta(hours=2)
        frames = [
            _frame(
                provider=ForecastProvider.GFS,
                run_time=run_time,
                accumulation_hours=6,
                forecast_hour=6,
                values_mm=[10.0, 70.0, 5.0, 1.0],  # 6h yellow
            ),
            _frame(
                provider=ForecastProvider.GFS,
                run_time=run_time,
                accumulation_hours=24,
                forecast_hour=24,
                values_mm=[10.0, 140.0, 5.0, 1.0],  # 24h orange (dominant)
            ),
        ]
        result = compute_provider_risk(
            provider=GFS,
            frames=frames,
            water_levels=[],
            settings=_settings(),
            generated_at=generated_at,
        )

        self.assertEqual(result.computed_risk_level, RiskLevel.ORANGE)
        self.assertEqual(result.dominant_window, "24h")
        self.assertEqual(result.frame_count, 2)
        self.assertEqual(result.model_run_time, run_time)


class LatestFramesPerProviderTest(IsolatedAsyncioTestCase):
    async def test_returns_latest_run_frames_per_provider(self) -> None:
        repository = DryRunForecastRepository()
        old_run = datetime(2026, 5, 1, 0, tzinfo=UTC)
        new_run = datetime(2026, 5, 1, 12, tzinfo=UTC)
        await repository.upsert_frames(
            [
                _frame(
                    provider=ForecastProvider.GFS,
                    run_time=old_run,
                    accumulation_hours=24,
                    forecast_hour=24,
                    values_mm=[1.0, 2.0, 3.0, 4.0],
                ),
                _frame(
                    provider=ForecastProvider.GFS,
                    run_time=new_run,
                    accumulation_hours=24,
                    forecast_hour=24,
                    values_mm=[5.0, 6.0, 7.0, 8.0],
                ),
                _frame(
                    provider=ForecastProvider.ECMWF_OPEN_DATA,
                    run_time=new_run,
                    accumulation_hours=24,
                    forecast_hour=24,
                    values_mm=[9.0, 10.0, 11.0, 12.0],
                ),
            ]
        )

        result = await repository.get_latest_frames_per_provider(
            area_name=Phase1Area().name,
            providers=[GFS, ECMWF],
        )

        self.assertEqual(len(result[GFS]), 1)
        self.assertEqual(result[GFS][0].run_time, new_run)
        self.assertEqual(len(result[ECMWF]), 1)
        self.assertEqual(result[ECMWF][0].run_time, new_run)

    async def test_missing_provider_returns_empty_list(self) -> None:
        repository = DryRunForecastRepository()
        result = await repository.get_latest_frames_per_provider(
            area_name=Phase1Area().name,
            providers=[GFS, ECMWF],
        )
        self.assertEqual(result[GFS], [])
        self.assertEqual(result[ECMWF], [])
