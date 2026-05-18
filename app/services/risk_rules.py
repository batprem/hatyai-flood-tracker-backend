from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.ingestion.models import ForecastFrame
from app.schemas.common import DataFreshness, RiskLevel
from app.schemas.risk import (
    CurrentRiskResponse,
    RiskAvailability,
    RiskCoverage,
    RiskFreshnessStatus,
    RiskMapProperties,
    RiskSignal,
    RiskUncertainty,
    RiskUncertaintyLevel,
)

LEVEL_SCORES: Mapping[RiskLevel, int] = {
    RiskLevel.GREEN: 0,
    RiskLevel.YELLOW: 1,
    RiskLevel.ORANGE: 2,
    RiskLevel.RED: 3,
}
SCORE_LEVELS: Mapping[int, RiskLevel] = {score: level for level, score in LEVEL_SCORES.items()}


@dataclass(frozen=True, slots=True)
class RainfallThreshold:
    """Configure rainfall accumulation thresholds for one window."""

    window_hours: int
    yellow_mm: float
    orange_mm: float
    red_mm: float


@dataclass(frozen=True, slots=True)
class RiskRuleSettings:
    """Configure rule thresholds and data quality behavior."""

    rainfall_thresholds: Mapping[int, RainfallThreshold]
    fresh_run_max_age_hours: float
    aging_run_max_age_hours: float
    expected_forecast_cells: int
    expected_water_stations: int
    minimum_coverage_ratio: float
    water_watch_ratio: float
    water_station_max_age_hours: float
    allow_mock_water_level_raise: bool


@dataclass(frozen=True, slots=True)
class RainfallRiskInput:
    """Model normalized rainfall input for risk calculation."""

    area_id: str
    area_name: str
    rainfall_mm: float
    accumulation_hours: int
    source: str
    model_run_time: datetime
    valid_time: datetime
    retrieved_at: datetime
    is_mock: bool = True


@dataclass(frozen=True, slots=True)
class WaterLevelRiskInput:
    """Model normalized station input for risk calculation."""

    station_id: str
    station_name: str
    water_level_m: float
    warning_level_m: float
    critical_level_m: float
    observed_at: datetime
    source: str
    is_mock: bool = True


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp.

    Returns:
        Current UTC datetime.
    """
    return datetime.now(UTC)


def score_to_level(score: int) -> RiskLevel:
    """Convert a bounded risk score to its public level.

    Args:
        score: Numeric risk score (0-3).

    Returns:
        Public risk level (GREEN, YELLOW, ORANGE, or RED).
    """
    return SCORE_LEVELS[max(0, min(3, score))]


def level_to_score(level: RiskLevel) -> int:
    """Convert a public risk level to its numeric score.

    Args:
        level: Public risk level.

    Returns:
        Numeric risk score (0-3).
    """
    return LEVEL_SCORES[level]


def score_rainfall(rainfall_mm: float, threshold: RainfallThreshold) -> RiskLevel:
    """Score rainfall using inclusive lower bounds for each threshold.

    Args:
        rainfall_mm: Total accumulated rainfall in mm.
        threshold: Threshold configuration for the accumulation window.

    Returns:
        Risk level based on rainfall thresholds.
    """
    if rainfall_mm >= threshold.red_mm:
        return RiskLevel.RED
    if rainfall_mm >= threshold.orange_mm:
        return RiskLevel.ORANGE
    if rainfall_mm >= threshold.yellow_mm:
        return RiskLevel.YELLOW
    return RiskLevel.GREEN


def build_rainfall_inputs_from_frames(
    frames: Iterable[ForecastFrame],
) -> list[RainfallRiskInput]:
    """Derive rainfall risk inputs from stored forecast frames.

    Each frame becomes one input where ``rainfall_mm`` is the maximum cell value
    across the basin grid. This conservatively follows the rule that the public
    status takes the maximum risk across relevant basin cells (see
    ``docs/risk-layer-design.md`` lines 22-43 and 60-62). The ``area_id`` is
    composed of provider, model, valid time, and accumulation window so each
    frame is a distinct scored unit and downstream coverage counts reflect the
    actual distinct basin observations rather than collapsing windows.

    Args:
        frames: Normalized forecast frames persisted in MongoDB.

    Returns:
        A list of ``RainfallRiskInput`` ready for ``calculate_current_risk``.
    """
    inputs: list[RainfallRiskInput] = []
    for frame in frames:
        if not frame.values_mm:
            continue
        rainfall_mm = max(frame.values_mm)
        valid_iso = frame.valid_time.astimezone(UTC).strftime("%Y%m%dT%H%MZ")
        area_id = f"{frame.provider.value}:{frame.model}:{frame.accumulation_hours}h:{valid_iso}"
        area_name = (
            f"{frame.area.name} ({frame.accumulation_hours}h accumulation "
            f"valid {frame.valid_time.astimezone(UTC).isoformat()})"
        )
        inputs.append(
            RainfallRiskInput(
                area_id=area_id,
                area_name=area_name,
                rainfall_mm=rainfall_mm,
                accumulation_hours=frame.accumulation_hours,
                source=frame.provider.value,
                model_run_time=frame.run_time,
                valid_time=frame.valid_time,
                retrieved_at=frame.retrieved_at,
                is_mock=False,
            )
        )
    return inputs


def score_water_level(
    water_level_m: float,
    warning_level_m: float,
    critical_level_m: float,
    watch_ratio: float,
) -> RiskLevel:
    """Score station water level with a derived watch threshold.

    Args:
        water_level_m: Observed water level in metres.
        warning_level_m: Warning threshold in metres.
        critical_level_m: Critical/danger threshold in metres.
        watch_ratio: Ratio to derive watch threshold from warning level.

    Returns:
        Risk level based on water-level thresholds.
    """
    watch_level_m = warning_level_m * watch_ratio
    if water_level_m >= critical_level_m:
        return RiskLevel.RED
    if water_level_m >= warning_level_m:
        return RiskLevel.ORANGE
    if water_level_m >= watch_level_m:
        return RiskLevel.YELLOW
    return RiskLevel.GREEN


def calculate_current_risk(
    *,
    forecasts: Sequence[RainfallRiskInput],
    water_levels: Sequence[WaterLevelRiskInput],
    settings: RiskRuleSettings,
    generated_at: datetime | None = None,
) -> CurrentRiskResponse:
    """Calculate current flood risk from normalized rainfall and station inputs.

    Args:
        forecasts: Rainfall forecast inputs.
        water_levels: Station observation inputs.
        settings: Risk rule configuration and thresholds.
        generated_at: Timestamp of risk generation.
            Defaults to ``None``.

    Returns:
        Comprehensive risk response with level, signals, coverage, and public messaging.
    """
    generated_at = generated_at or utc_now()
    sources = sorted(
        {forecast.source for forecast in forecasts} | {station.source for station in water_levels}
    )
    source = ",".join(sources) if sources else "none"

    if not forecasts:
        return _unavailable_response(
            generated_at=generated_at,
            source=source,
            settings=settings,
            water_levels=water_levels,
        )

    rainfall_signals: list[RiskSignal] = []
    water_signals: list[RiskSignal] = []
    uncertainty_reasons: list[str] = []
    scored_levels: list[RiskLevel] = []

    for forecast in forecasts:
        threshold = settings.rainfall_thresholds.get(forecast.accumulation_hours)
        if threshold is None:
            uncertainty_reasons.append(
                f"No configured rainfall threshold for {forecast.accumulation_hours}h accumulation."
            )
            continue

        level = score_rainfall(forecast.rainfall_mm, threshold)
        scored_levels.append(level)
        rainfall_signals.append(
            RiskSignal(
                name=f"{forecast.accumulation_hours}-hour rainfall",
                value=f"{forecast.rainfall_mm:.1f} mm",
                level=level,
                detail=(
                    f"{forecast.area_name} forecast uses configured {forecast.accumulation_hours}h "
                    "rainfall thresholds."
                ),
                source=forecast.source,
                valid_at=forecast.valid_time,
                window_hours=forecast.accumulation_hours,
            )
        )

    latest_model_run = max(forecast.model_run_time for forecast in forecasts)
    latest_retrieved_at = max(forecast.retrieved_at for forecast in forecasts)
    latest_valid_at = max(forecast.valid_time for forecast in forecasts)
    freshness_status = _freshness_status(generated_at, latest_model_run, settings)

    reliable_water_levels = 0
    for station in water_levels:
        water_age = generated_at - station.observed_at
        is_fresh_station = water_age <= timedelta(hours=settings.water_station_max_age_hours)
        can_raise_risk = is_fresh_station and (
            not station.is_mock or settings.allow_mock_water_level_raise
        )
        level = score_water_level(
            water_level_m=station.water_level_m,
            warning_level_m=station.warning_level_m,
            critical_level_m=station.critical_level_m,
            watch_ratio=settings.water_watch_ratio,
        )
        detail = (
            "Observed water level scored against station thresholds."
            if can_raise_risk
            else "Mock, stale, or supporting-only station data is not allowed to raise public risk."
        )
        water_signals.append(
            RiskSignal(
                name=f"{station.station_name} water level",
                value=f"{station.water_level_m:.2f} m",
                level=level,
                detail=detail,
                source=station.source,
                observed_at=station.observed_at,
            )
        )
        if can_raise_risk:
            reliable_water_levels += 1
            scored_levels.append(level)

    if not scored_levels:
        return _unavailable_response(
            generated_at=generated_at,
            source=source,
            settings=settings,
            water_levels=water_levels,
        )

    computed_score = max(level_to_score(level) for level in scored_levels)
    computed_level = score_to_level(computed_score)
    coverage = _coverage(forecasts, water_levels, settings)
    availability = _availability(
        freshness_status,
        coverage,
        rainfall_signals,
        settings.minimum_coverage_ratio,
    )
    display_score = _display_score(computed_score, availability)
    display_level = score_to_level(display_score)

    if any(forecast.is_mock for forecast in forecasts):
        uncertainty_reasons.append("Mock forecast data is suitable for prototype validation only.")
    if reliable_water_levels == 0 and water_levels:
        uncertainty_reasons.append("Water-level observations are shown as supporting context only.")
    if coverage.basin_coverage_ratio < settings.minimum_coverage_ratio:
        uncertainty_reasons.append("Forecast basin coverage is below the configured minimum.")
    if freshness_status == RiskFreshnessStatus.STALE:
        uncertainty_reasons.append(
            "Forecast model run is stale and must not be shown as current all-clear."
        )

    uncertainty = _uncertainty(freshness_status, coverage, uncertainty_reasons)
    signals = sorted(
        [*rainfall_signals, *water_signals],
        key=lambda signal: level_to_score(signal.level),
        reverse=True,
    )
    headline, summary, recommended_action = _public_text(
        display_level,
        availability,
        freshness_status,
        signals[0] if signals else None,
    )
    confidence = _confidence(freshness_status, coverage, uncertainty.level)

    return CurrentRiskResponse(
        freshness=_data_freshness(
            generated_at=generated_at,
            valid_at=latest_valid_at,
            source=source,
            is_mock=any(forecast.is_mock for forecast in forecasts),
        ),
        availability=availability,
        freshness_status=freshness_status,
        level=display_level,
        score=display_score,
        computed_level=computed_level,
        computed_score=computed_score,
        headline=headline,
        summary=summary,
        recommended_action=recommended_action,
        confidence=confidence,
        signals=signals,
        coverage=coverage,
        uncertainty=uncertainty,
        map_properties=RiskMapProperties(
            area_id="hatyai-basin",
            level=display_level,
            score=display_score,
            primary_driver=signals[0].name if signals else None,
            generated_at=generated_at,
            valid_at=latest_valid_at,
            availability=availability,
            freshness_status=freshness_status,
            uncertainty_level=uncertainty.level,
            source=source,
            model_run_time=latest_model_run,
            latest_source_retrieved_at=latest_retrieved_at,
            is_official_warning=False,
        ),
        is_official_warning=False,
    )


def _unavailable_response(
    *,
    generated_at: datetime,
    source: str,
    settings: RiskRuleSettings,
    water_levels: Sequence[WaterLevelRiskInput],
) -> CurrentRiskResponse:
    coverage = _coverage([], water_levels, settings)
    uncertainty = RiskUncertainty(
        level=RiskUncertaintyLevel.HIGH,
        reasons=[
            "Forecast rainfall input is unavailable, so current flood risk cannot be computed."
        ],
    )
    return CurrentRiskResponse(
        freshness=_data_freshness(
            generated_at=generated_at,
            valid_at=None,
            source=source,
            is_mock=any(station.is_mock for station in water_levels),
        ),
        availability=RiskAvailability.UNAVAILABLE,
        freshness_status=RiskFreshnessStatus.UNAVAILABLE,
        level=RiskLevel.YELLOW,
        score=1,
        computed_level=None,
        computed_score=None,
        headline="ข้อมูลความเสี่ยงน้ำท่วมยังไม่พร้อมใช้งาน",
        summary=(
            "Forecast rainfall data is unavailable, so the service cannot confirm "
            "current all-clear conditions. Please check official local sources."
        ),
        recommended_action=(
            "Use caution in flood-prone areas and monitor official weather and water updates."
        ),
        confidence=0,
        signals=[],
        coverage=coverage,
        uncertainty=uncertainty,
        map_properties=RiskMapProperties(
            area_id="hatyai-basin",
            level=RiskLevel.YELLOW,
            score=1,
            primary_driver=None,
            generated_at=generated_at,
            valid_at=None,
            availability=RiskAvailability.UNAVAILABLE,
            freshness_status=RiskFreshnessStatus.UNAVAILABLE,
            uncertainty_level=RiskUncertaintyLevel.HIGH,
            source=source,
            model_run_time=None,
            latest_source_retrieved_at=None,
            is_official_warning=False,
        ),
        is_official_warning=False,
    )


def _freshness_status(
    generated_at: datetime,
    model_run_time: datetime,
    settings: RiskRuleSettings,
) -> RiskFreshnessStatus:
    model_run_age_hours = (generated_at - model_run_time).total_seconds() / 3600
    if model_run_age_hours <= settings.fresh_run_max_age_hours:
        return RiskFreshnessStatus.FRESH
    if model_run_age_hours <= settings.aging_run_max_age_hours:
        return RiskFreshnessStatus.AGING
    return RiskFreshnessStatus.STALE


def _coverage(
    forecasts: Sequence[RainfallRiskInput],
    water_levels: Sequence[WaterLevelRiskInput],
    settings: RiskRuleSettings,
) -> RiskCoverage:
    forecast_cells_expected = max(0, settings.expected_forecast_cells)
    forecast_cells_available = len({forecast.area_id for forecast in forecasts})
    basin_coverage_ratio = (
        min(1, forecast_cells_available / forecast_cells_expected) if forecast_cells_expected else 1
    )
    elevated_signal_count = sum(
        1
        for forecast in forecasts
        if settings.rainfall_thresholds.get(forecast.accumulation_hours)
        and score_rainfall(
            forecast.rainfall_mm,
            settings.rainfall_thresholds[forecast.accumulation_hours],
        )
        != RiskLevel.GREEN
    )
    return RiskCoverage(
        forecast_cells_expected=forecast_cells_expected,
        forecast_cells_available=forecast_cells_available,
        basin_coverage_ratio=basin_coverage_ratio,
        water_stations_expected=max(0, settings.expected_water_stations),
        water_stations_available=len(water_levels),
        elevated_signal_count=elevated_signal_count,
    )


def _availability(
    freshness_status: RiskFreshnessStatus,
    coverage: RiskCoverage,
    rainfall_signals: Sequence[RiskSignal],
    minimum_coverage_ratio: float,
) -> RiskAvailability:
    if not rainfall_signals:
        return RiskAvailability.UNAVAILABLE
    if (
        freshness_status == RiskFreshnessStatus.STALE
        or coverage.basin_coverage_ratio < minimum_coverage_ratio
    ):
        return RiskAvailability.DEGRADED
    return RiskAvailability.AVAILABLE


def _display_score(computed_score: int, availability: RiskAvailability) -> int:
    if availability in {RiskAvailability.DEGRADED, RiskAvailability.UNAVAILABLE}:
        return max(computed_score, level_to_score(RiskLevel.YELLOW))
    return computed_score


def _uncertainty(
    freshness_status: RiskFreshnessStatus,
    coverage: RiskCoverage,
    reasons: Sequence[str],
) -> RiskUncertainty:
    if freshness_status in {RiskFreshnessStatus.STALE, RiskFreshnessStatus.UNAVAILABLE}:
        level = RiskUncertaintyLevel.HIGH
    elif coverage.basin_coverage_ratio < 1 or coverage.water_stations_available == 0:
        level = RiskUncertaintyLevel.MEDIUM
    else:
        level = RiskUncertaintyLevel.MEDIUM if reasons else RiskUncertaintyLevel.LOW
    return RiskUncertainty(level=level, reasons=list(dict.fromkeys(reasons)))


def _confidence(
    freshness_status: RiskFreshnessStatus,
    coverage: RiskCoverage,
    uncertainty_level: RiskUncertaintyLevel,
) -> float:
    if freshness_status == RiskFreshnessStatus.UNAVAILABLE:
        return 0
    base = 0.35 + (0.35 * coverage.basin_coverage_ratio)
    if freshness_status == RiskFreshnessStatus.FRESH:
        base += 0.15
    elif freshness_status == RiskFreshnessStatus.AGING:
        base += 0.05
    else:
        base -= 0.15
    if coverage.elevated_signal_count > 1:
        base += 0.1
    if uncertainty_level == RiskUncertaintyLevel.HIGH:
        base -= 0.2
    elif uncertainty_level == RiskUncertaintyLevel.LOW:
        base += 0.05
    return round(max(0, min(1, base)), 2)


def _public_text(
    level: RiskLevel,
    availability: RiskAvailability,
    freshness_status: RiskFreshnessStatus,
    primary_signal: RiskSignal | None,
) -> tuple[str, str, str]:
    if availability == RiskAvailability.UNAVAILABLE:
        return (
            "ข้อมูลความเสี่ยงน้ำท่วมยังไม่พร้อมใช้งาน",
            (
                "Forecast rainfall data is unavailable, so current all-clear conditions "
                "cannot be confirmed."
            ),
            "Use caution and monitor official local weather and flood updates.",
        )
    if availability == RiskAvailability.DEGRADED or freshness_status == RiskFreshnessStatus.STALE:
        return (
            "ข้อมูลเริ่มล่าช้า โปรดติดตามแหล่งข้อมูลทางการ",
            "Risk data is degraded or stale, so it must not be treated as a current all-clear.",
            "Check official sources and avoid relying on old green-status information.",
        )

    driver = primary_signal.name if primary_signal else "forecast rainfall"
    if level == RiskLevel.RED:
        return (
            "อันตราย มีสัญญาณความเสี่ยงน้ำท่วมสูง",
            f"{driver} is in the danger range for the configured Phase 1 rules.",
            "Avoid flood-prone areas and follow official instructions immediately.",
        )
    if level == RiskLevel.ORANGE:
        return (
            "เตือนภัย มีสัญญาณฝนสะสมหรือระดับน้ำสูง",
            f"{driver} suggests flooding is possible in vulnerable or low-lying areas.",
            "Prepare for flooding and monitor official local guidance.",
        )
    if level == RiskLevel.YELLOW:
        return (
            "เฝ้าระวังฝนและระดับน้ำ",
            f"{driver} has reached the watch range for the configured Phase 1 rules.",
            "Monitor updates, especially in low-lying or slow-drainage areas.",
        )
    return (
        "สถานการณ์ปกติจากข้อมูลล่าสุด",
        (
            "Latest available forecast data does not show rainfall or water-level signals "
            "above watch thresholds."
        ),
        "Continue normal monitoring and check back for updates.",
    )


def _data_freshness(
    *,
    generated_at: datetime,
    valid_at: datetime | None,
    source: str,
    is_mock: bool,
) -> DataFreshness:
    return DataFreshness(
        generated_at=generated_at,
        valid_at=valid_at,
        source=source,
        is_mock=is_mock,
    )
