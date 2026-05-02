from datetime import UTC, datetime, timedelta
from unittest import TestCase

from app.schemas.common import RiskLevel
from app.schemas.risk import RiskAvailability, RiskFreshnessStatus
from app.services.risk_rules import (
    RainfallRiskInput,
    RainfallThreshold,
    RiskRuleSettings,
    WaterLevelRiskInput,
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
