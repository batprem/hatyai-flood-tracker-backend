from datetime import UTC, datetime, timedelta

from app.schemas.common import Coordinates, DataFreshness, LocalizedName, RiskLevel
from app.schemas.forecast import RainfallForecastPoint, RainfallForecastResponse
from app.schemas.map_layers import MapLayer, MapLayersResponse, MapLayerType
from app.schemas.risk import CurrentRiskResponse
from app.schemas.stations import WaterLevelResponse, WaterLevelTrend, WaterStationLevel
from app.services.risk_rules import (
    RainfallRiskInput,
    RiskRuleSettings,
    WaterLevelRiskInput,
    calculate_current_risk,
)

MOCK_SOURCE = "phase-1-normalized-mock"


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def get_rainfall_forecast() -> RainfallForecastResponse:
    """Return normalized mock rainfall forecasts for target basins."""
    generated_at = utc_now()
    valid_at = generated_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    forecasts = [
        RainfallForecastPoint(
            basin_id="utapao-canal",
            basin_name=LocalizedName(th="คลองอู่ตะเภา", en="U-Tapao Canal"),
            centroid=Coordinates(latitude=7.0084, longitude=100.4747),
            forecast_time=valid_at + timedelta(hours=6),
            lead_time_hours=6,
            rainfall_mm=72.0,
            accumulation_hours=6,
            risk_level=RiskLevel.YELLOW,
        ),
        RainfallForecastPoint(
            basin_id="songkhla-lake-basin",
            basin_name=LocalizedName(th="ลุ่มน้ำทะเลสาบสงขลา", en="Songkhla Lake Basin"),
            centroid=Coordinates(latitude=7.1966, longitude=100.5951),
            forecast_time=valid_at + timedelta(hours=24),
            lead_time_hours=24,
            rainfall_mm=142.0,
            accumulation_hours=24,
            risk_level=RiskLevel.ORANGE,
        ),
    ]

    return RainfallForecastResponse(
        freshness=DataFreshness(
            generated_at=generated_at,
            valid_at=valid_at,
            source=MOCK_SOURCE,
        ),
        forecasts=forecasts,
    )


def get_water_levels() -> WaterLevelResponse:
    """Return normalized mock water-level observations."""
    generated_at = utc_now()
    observed_at = generated_at - timedelta(minutes=5)

    stations = [
        WaterStationLevel(
            station_id="HY-UTP-001",
            station_name=LocalizedName(
                th="สะพานคลองอู่ตะเภา หาดใหญ่",
                en="U-Tapao Canal Bridge, Hat Yai",
            ),
            canal_or_lake=LocalizedName(th="คลองอู่ตะเภา", en="U-Tapao Canal"),
            location=Coordinates(latitude=7.0167, longitude=100.4708),
            observed_at=observed_at,
            water_level_m=2.4,
            warning_level_m=2.8,
            critical_level_m=3.2,
            trend=WaterLevelTrend.RISING,
            risk_level=RiskLevel.YELLOW,
        ),
        WaterStationLevel(
            station_id="SKL-LAKE-001",
            station_name=LocalizedName(th="จุดวัดระดับน้ำทะเลสาบสงขลา", en="Songkhla Lake Gauge"),
            canal_or_lake=LocalizedName(th="ทะเลสาบสงขลา", en="Songkhla Lake"),
            location=Coordinates(latitude=7.1872, longitude=100.5948),
            observed_at=observed_at,
            water_level_m=0.9,
            warning_level_m=1.4,
            critical_level_m=1.8,
            trend=WaterLevelTrend.STEADY,
            risk_level=RiskLevel.GREEN,
        ),
    ]

    return WaterLevelResponse(
        freshness=DataFreshness(
            generated_at=generated_at,
            valid_at=observed_at,
            source=MOCK_SOURCE,
        ),
        stations=stations,
    )


def get_current_risk(settings: RiskRuleSettings) -> CurrentRiskResponse:
    """Calculate the Phase 1 rule-based risk summary from normalized mock inputs.

    Args:
        settings (RiskRuleSettings): Risk rule configuration.
    """
    rainfall = get_rainfall_forecast()
    water_levels = get_water_levels()
    forecasts = [
        RainfallRiskInput(
            area_id=forecast.basin_id,
            area_name=forecast.basin_name.en,
            rainfall_mm=forecast.rainfall_mm,
            accumulation_hours=forecast.accumulation_hours,
            source=rainfall.freshness.source,
            model_run_time=rainfall.freshness.generated_at,
            valid_time=forecast.forecast_time,
            retrieved_at=rainfall.freshness.generated_at,
            is_mock=rainfall.freshness.is_mock,
        )
        for forecast in rainfall.forecasts
    ]
    stations = [
        WaterLevelRiskInput(
            station_id=station.station_id,
            station_name=station.station_name.en,
            water_level_m=station.water_level_m,
            warning_level_m=station.warning_level_m,
            critical_level_m=station.critical_level_m,
            observed_at=station.observed_at,
            source=water_levels.freshness.source,
            is_mock=water_levels.freshness.is_mock,
        )
        for station in water_levels.stations
    ]
    return calculate_current_risk(
        forecasts=forecasts,
        water_levels=stations,
        settings=settings,
        generated_at=max(rainfall.freshness.generated_at, water_levels.freshness.generated_at),
    )


def get_map_layers() -> MapLayersResponse:
    """Return public map layer descriptors for the frontend."""
    generated_at = utc_now()

    layers = [
        MapLayer(
            layer_id="water-stations",
            title="Water level stations",
            description="Mock water-level stations for U-Tapao Canal and Songkhla Lake.",
            layer_type=MapLayerType.POINT,
            visible_by_default=True,
            min_zoom=8,
            max_zoom=18,
            source_url="/api/stations/water-level",
        ),
        MapLayer(
            layer_id="rainfall-basins",
            title="Rainfall forecast basins",
            description="Normalized mock rainfall forecast coverage for Phase 1 basins.",
            layer_type=MapLayerType.POLYGON,
            visible_by_default=True,
            min_zoom=7,
            max_zoom=14,
            source_url="/api/forecast/rainfall",
        ),
        MapLayer(
            layer_id="risk-summary",
            title="Current risk summary",
            description=(
                "Rule-based current risk status with map-compatible freshness, source, "
                "uncertainty, and official-warning properties."
            ),
            layer_type=MapLayerType.POINT,
            visible_by_default=True,
            min_zoom=7,
            max_zoom=18,
            source_url="/api/risk/current",
            metadata_fields=[
                "map_properties.area_id",
                "map_properties.level",
                "map_properties.score",
                "map_properties.primary_driver",
                "map_properties.availability",
                "map_properties.freshness_status",
                "map_properties.uncertainty_level",
                "map_properties.source",
                "map_properties.model_run_time",
                "map_properties.latest_source_retrieved_at",
                "map_properties.is_official_warning",
            ],
        ),
    ]

    return MapLayersResponse(
        freshness=DataFreshness(
            generated_at=generated_at,
            valid_at=generated_at,
            source=MOCK_SOURCE,
        ),
        layers=layers,
    )
