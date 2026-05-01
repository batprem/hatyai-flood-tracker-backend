from datetime import UTC, datetime, timedelta

from app.schemas.common import Coordinates, DataFreshness, LocalizedName, RiskLevel
from app.schemas.forecast import RainfallForecastPoint, RainfallForecastResponse
from app.schemas.map_layers import MapLayer, MapLayersResponse, MapLayerType
from app.schemas.risk import CurrentRiskResponse, RiskSignal
from app.schemas.stations import WaterLevelResponse, WaterLevelTrend, WaterStationLevel

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
            rainfall_mm=34.5,
            accumulation_hours=6,
            risk_level=RiskLevel.YELLOW,
        ),
        RainfallForecastPoint(
            basin_id="songkhla-lake-basin",
            basin_name=LocalizedName(th="ลุ่มน้ำทะเลสาบสงขลา", en="Songkhla Lake Basin"),
            centroid=Coordinates(latitude=7.1966, longitude=100.5951),
            forecast_time=valid_at + timedelta(hours=12),
            lead_time_hours=12,
            rainfall_mm=58.2,
            accumulation_hours=12,
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


def get_current_risk() -> CurrentRiskResponse:
    """Return the Phase 1 rule-based mock flood risk summary."""
    generated_at = utc_now()

    signals = [
        RiskSignal(
            name="6-hour rainfall",
            value="34.5 mm",
            level=RiskLevel.YELLOW,
            detail="Rainfall exceeds the Phase 1 watch threshold for U-Tapao Canal.",
        ),
        RiskSignal(
            name="12-hour basin rainfall",
            value="58.2 mm",
            level=RiskLevel.ORANGE,
            detail="Songkhla Lake basin forecast suggests heavier rain bands may develop.",
        ),
        RiskSignal(
            name="U-Tapao Canal level",
            value="2.4 m and rising",
            level=RiskLevel.YELLOW,
            detail="Current mock level remains below warning level but trend is rising.",
        ),
    ]

    return CurrentRiskResponse(
        freshness=DataFreshness(
            generated_at=generated_at,
            valid_at=generated_at,
            source=MOCK_SOURCE,
        ),
        level=RiskLevel.ORANGE,
        headline="เฝ้าระวังฝนหนักและระดับน้ำคลองอู่ตะเภา",
        summary=(
            "Mock Phase 1 rules indicate elevated flood awareness because basin rainfall is "
            "increasing while the U-Tapao Canal trend is rising."
        ),
        recommended_action=(
            "Monitor official updates, avoid flood-prone shortcuts, and prepare to move "
            "belongings from low-lying areas."
        ),
        confidence=0.7,
        signals=signals,
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
            description="Rule-based current risk status for public alert display.",
            layer_type=MapLayerType.POINT,
            visible_by_default=True,
            min_zoom=7,
            max_zoom=18,
            source_url="/api/risk/current",
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
