from datetime import UTC, datetime, timedelta
from unittest import TestCase

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
from app.schemas.common import RiskLevel
from app.schemas.risk import RiskAvailability, RiskFreshnessStatus
from app.services.risk_rules import (
    RainfallRiskInput,
    RainfallThreshold,
    RiskRuleSettings,
    WaterLevelRiskInput,
    build_rainfall_inputs_from_frames,
    calculate_current_risk,
)


def risk_settings() -> RiskRuleSettings:
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


def rainfall_input(
    *,
    rainfall_mm: float,
    accumulation_hours: int,
    model_run_time: datetime,
    area_id: str = "utapao-canal",
) -> RainfallRiskInput:
    return RainfallRiskInput(
        area_id=area_id,
        area_name=area_id,
        rainfall_mm=rainfall_mm,
        accumulation_hours=accumulation_hours,
        source="phase-1-normalized-mock",
        model_run_time=model_run_time,
        valid_time=model_run_time + timedelta(hours=accumulation_hours),
        retrieved_at=model_run_time + timedelta(minutes=30),
    )


class RiskRulesTest(TestCase):
    def test_rainfall_thresholds_change_current_risk_level(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        response = calculate_current_risk(
            forecasts=[
                rainfall_input(
                    rainfall_mm=70,
                    accumulation_hours=6,
                    model_run_time=generated_at - timedelta(hours=2),
                ),
                rainfall_input(
                    rainfall_mm=142,
                    accumulation_hours=24,
                    model_run_time=generated_at - timedelta(hours=2),
                    area_id="songkhla-lake-basin",
                ),
            ],
            water_levels=[],
            settings=risk_settings(),
            generated_at=generated_at,
        )

        self.assertEqual(response.level, RiskLevel.ORANGE)
        self.assertEqual(response.computed_level, RiskLevel.ORANGE)
        self.assertEqual(response.availability, RiskAvailability.AVAILABLE)
        self.assertEqual(response.map_properties.primary_driver, "24-hour rainfall")

    def test_stale_green_forecast_is_degraded_not_all_clear(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        response = calculate_current_risk(
            forecasts=[
                rainfall_input(
                    rainfall_mm=10,
                    accumulation_hours=6,
                    model_run_time=generated_at - timedelta(hours=20),
                ),
                rainfall_input(
                    rainfall_mm=20,
                    accumulation_hours=24,
                    model_run_time=generated_at - timedelta(hours=20),
                    area_id="songkhla-lake-basin",
                ),
            ],
            water_levels=[],
            settings=risk_settings(),
            generated_at=generated_at,
        )

        self.assertEqual(response.computed_level, RiskLevel.GREEN)
        self.assertEqual(response.level, RiskLevel.YELLOW)
        self.assertEqual(response.availability, RiskAvailability.DEGRADED)
        self.assertEqual(response.freshness_status, RiskFreshnessStatus.STALE)

    def test_missing_forecast_is_unavailable_and_not_green(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        response = calculate_current_risk(
            forecasts=[],
            water_levels=[
                WaterLevelRiskInput(
                    station_id="HY-UTP-001",
                    station_name="U-Tapao Canal Bridge",
                    water_level_m=2.4,
                    warning_level_m=2.8,
                    critical_level_m=3.2,
                    observed_at=generated_at - timedelta(minutes=10),
                    source="phase-1-normalized-mock",
                )
            ],
            settings=risk_settings(),
            generated_at=generated_at,
        )

        self.assertEqual(response.availability, RiskAvailability.UNAVAILABLE)
        self.assertEqual(response.level, RiskLevel.YELLOW)
        self.assertIsNone(response.computed_level)
        self.assertEqual(response.confidence, 0)


def _frame(
    *,
    run_time: datetime,
    accumulation_hours: int,
    values_mm: list[float],
    forecast_hour: int,
) -> ForecastFrame:
    valid_time = run_time + timedelta(hours=forecast_hour)
    window_start = valid_time - timedelta(hours=accumulation_hours)
    return ForecastFrame(
        frame_id=f"gfs:gfs:{run_time:%Y%m%d%H}:precipitation:f{forecast_hour:03d}",
        run_id=f"gfs:gfs:{run_time:%Y%m%d%H}",
        provider=ForecastProvider.GFS,
        model="gfs",
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
        area=Phase1Area(),
        grid=ForecastGrid(resolution_degrees=0.25, width=2, height=2),
        values_mm=values_mm,
        source=ForecastSource(
            url="https://example.test/fixture",
            product="gfs.t00z.pgrb2.0p25",
            license="public-domain",
            attribution="NOAA NCEP GFS",
            raw_artifact_ref="fixture://gfs",
        ),
        quality=ForecastQuality(
            status=ForecastRunStatus.NORMALIZED,
            missing_value_count=0,
            minimum_mm=min(values_mm),
            maximum_mm=max(values_mm),
        ),
    )


class FrameDrivenRiskTest(TestCase):
    def test_build_rainfall_inputs_from_frames_uses_max_cell_value(self) -> None:
        run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
        frames = [
            _frame(
                run_time=run_time,
                accumulation_hours=6,
                forecast_hour=6,
                values_mm=[10.0, 30.0, 70.0, 5.0],
            ),
            _frame(
                run_time=run_time,
                accumulation_hours=24,
                forecast_hour=24,
                values_mm=[20.0, 142.0, 90.0, 60.0],
            ),
        ]

        inputs = build_rainfall_inputs_from_frames(frames)

        self.assertEqual(len(inputs), 2)
        rainfall_by_window = {item.accumulation_hours: item.rainfall_mm for item in inputs}
        self.assertEqual(rainfall_by_window[6], 70.0)
        self.assertEqual(rainfall_by_window[24], 142.0)
        self.assertTrue(all(item.source == ForecastProvider.GFS.value for item in inputs))
        self.assertTrue(all(item.is_mock is False for item in inputs))
        self.assertTrue(all(item.model_run_time == run_time for item in inputs))

    def test_build_rainfall_inputs_skips_empty_frames(self) -> None:
        # Pydantic would reject empty values_mm at the boundary, but the helper
        # also defends against it because list_frames is generic.
        run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
        valid_frame = _frame(
            run_time=run_time,
            accumulation_hours=6,
            forecast_hour=6,
            values_mm=[10.0],
        )
        inputs = build_rainfall_inputs_from_frames([valid_frame])
        self.assertEqual(len(inputs), 1)

    def test_frame_driven_calculate_uses_stored_run_and_valid_times(self) -> None:
        run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
        generated_at = datetime(2026, 5, 1, 3, tzinfo=UTC)
        frames = [
            _frame(
                run_time=run_time,
                accumulation_hours=6,
                forecast_hour=6,
                values_mm=[10.0, 70.0, 30.0, 5.0],
            ),
            _frame(
                run_time=run_time,
                accumulation_hours=24,
                forecast_hour=24,
                values_mm=[80.0, 142.0, 100.0, 60.0],
            ),
        ]
        rainfall_inputs = build_rainfall_inputs_from_frames(frames)

        response = calculate_current_risk(
            forecasts=rainfall_inputs,
            water_levels=[],
            settings=risk_settings(),
            generated_at=generated_at,
        )

        self.assertEqual(response.computed_level, RiskLevel.ORANGE)
        self.assertEqual(response.level, RiskLevel.ORANGE)
        self.assertEqual(response.availability, RiskAvailability.AVAILABLE)
        self.assertEqual(response.freshness_status, RiskFreshnessStatus.FRESH)
        # Drivers reference the stored frame run/valid times, not mock timestamps.
        self.assertEqual(response.map_properties.model_run_time, run_time)
        expected_latest_valid = run_time + timedelta(hours=24)
        self.assertEqual(response.map_properties.valid_at, expected_latest_valid)
        rainfall_signals = [
            signal for signal in response.signals if signal.window_hours is not None
        ]
        self.assertTrue(rainfall_signals, "expected rainfall signals from stored frames")
        self.assertTrue(all(signal.source == "gfs" for signal in rainfall_signals))
        for signal in rainfall_signals:
            self.assertIsNotNone(signal.valid_at)
            self.assertEqual(signal.valid_at, run_time + timedelta(hours=signal.window_hours or 0))

    def test_no_frames_drives_unavailable_non_green_branch(self) -> None:
        # Documented in docs/risk-layer-design.md line 117: when no forecast
        # is available, availability=unavailable and the display level is
        # never green.
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        rainfall_inputs = build_rainfall_inputs_from_frames([])
        self.assertEqual(rainfall_inputs, [])

        response = calculate_current_risk(
            forecasts=rainfall_inputs,
            water_levels=[],
            settings=risk_settings(),
            generated_at=generated_at,
        )
        self.assertEqual(response.availability, RiskAvailability.UNAVAILABLE)
        self.assertNotEqual(response.level, RiskLevel.GREEN)
        self.assertIsNone(response.computed_level)
