from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from app.services.risk_rules import RiskRuleSettings


class ForecastRepositoryBackend(StrEnum):
    """Select the active forecast repository implementation."""

    DRY_RUN = "dry_run"
    MONGO = "mongo"


class Settings(BaseSettings):
    """Configure runtime settings from environment variables."""

    app_name: str = "Hat Yai Flood Warning API"
    environment: str = "development"
    api_prefix: str = "/api"
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
        description="Comma-separated list of allowed frontend origins.",
    )
    frontend_origin: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HFT_FRONTEND_ORIGIN", "FRONTEND_ORIGIN"),
        description=(
            "Comma-separated list of public frontend origins to merge into CORS allow-list."
        ),
    )
    cors_origin_regex: str | None = Field(
        default=r"https://.*\.vercel\.app",
        description="Optional regex for deployment preview origins.",
    )
    risk_rainfall_1h_yellow_mm: float = 25
    risk_rainfall_1h_orange_mm: float = 40
    risk_rainfall_1h_red_mm: float = 60
    risk_rainfall_3h_yellow_mm: float = 40
    risk_rainfall_3h_orange_mm: float = 70
    risk_rainfall_3h_red_mm: float = 100
    risk_rainfall_6h_yellow_mm: float = 60
    risk_rainfall_6h_orange_mm: float = 100
    risk_rainfall_6h_red_mm: float = 150
    risk_rainfall_24h_yellow_mm: float = 80
    risk_rainfall_24h_orange_mm: float = 130
    risk_rainfall_24h_red_mm: float = 200
    risk_rainfall_48h_yellow_mm: float = 120
    risk_rainfall_48h_orange_mm: float = 200
    risk_rainfall_48h_red_mm: float = 300
    risk_rainfall_72h_yellow_mm: float = 160
    risk_rainfall_72h_orange_mm: float = 250
    risk_rainfall_72h_red_mm: float = 350
    risk_fresh_run_max_age_hours: float = 12
    risk_aging_run_max_age_hours: float = 18
    risk_expected_forecast_cells: int = 2
    risk_expected_water_stations: int = 2
    risk_minimum_coverage_ratio: float = Field(default=0.6, ge=0, le=1)
    risk_water_watch_ratio: float = Field(default=0.8, gt=0, le=1)
    risk_water_station_max_age_hours: float = 3
    risk_allow_mock_water_level_raise: bool = False
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        description="Connection URI used by the Mongo-backed forecast repository.",
    )
    mongodb_database: str = Field(
        default="hatyai_flood_warning",
        description="Database name used by the Mongo-backed forecast repository.",
    )
    forecast_repository_backend: ForecastRepositoryBackend = Field(
        default=ForecastRepositoryBackend.DRY_RUN,
        description=("Select the forecast repository backend: 'dry_run' (in-memory) or 'mongo'."),
    )

    model_config = SettingsConfigDict(
        env_file="dev.env",
        env_file_encoding="utf-8",
        env_prefix="HFT_",
        extra="ignore",
    )

    def allowed_cors_origins(self) -> list[str]:
        """Return configured CORS origins as a clean list, merging FRONTEND_ORIGIN entries."""
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        if self.frontend_origin:
            for origin in self.frontend_origin.split(","):
                stripped = origin.strip()
                if stripped and stripped not in origins:
                    origins.append(stripped)
        return origins

    def risk_rule_settings(self) -> RiskRuleSettings:
        """Return typed risk rule settings from environment-backed config."""
        from app.services.risk_rules import RainfallThreshold, RiskRuleSettings

        return RiskRuleSettings(
            rainfall_thresholds={
                1: RainfallThreshold(
                    window_hours=1,
                    yellow_mm=self.risk_rainfall_1h_yellow_mm,
                    orange_mm=self.risk_rainfall_1h_orange_mm,
                    red_mm=self.risk_rainfall_1h_red_mm,
                ),
                3: RainfallThreshold(
                    window_hours=3,
                    yellow_mm=self.risk_rainfall_3h_yellow_mm,
                    orange_mm=self.risk_rainfall_3h_orange_mm,
                    red_mm=self.risk_rainfall_3h_red_mm,
                ),
                6: RainfallThreshold(
                    window_hours=6,
                    yellow_mm=self.risk_rainfall_6h_yellow_mm,
                    orange_mm=self.risk_rainfall_6h_orange_mm,
                    red_mm=self.risk_rainfall_6h_red_mm,
                ),
                24: RainfallThreshold(
                    window_hours=24,
                    yellow_mm=self.risk_rainfall_24h_yellow_mm,
                    orange_mm=self.risk_rainfall_24h_orange_mm,
                    red_mm=self.risk_rainfall_24h_red_mm,
                ),
                48: RainfallThreshold(
                    window_hours=48,
                    yellow_mm=self.risk_rainfall_48h_yellow_mm,
                    orange_mm=self.risk_rainfall_48h_orange_mm,
                    red_mm=self.risk_rainfall_48h_red_mm,
                ),
                72: RainfallThreshold(
                    window_hours=72,
                    yellow_mm=self.risk_rainfall_72h_yellow_mm,
                    orange_mm=self.risk_rainfall_72h_orange_mm,
                    red_mm=self.risk_rainfall_72h_red_mm,
                ),
            },
            fresh_run_max_age_hours=self.risk_fresh_run_max_age_hours,
            aging_run_max_age_hours=self.risk_aging_run_max_age_hours,
            expected_forecast_cells=self.risk_expected_forecast_cells,
            expected_water_stations=self.risk_expected_water_stations,
            minimum_coverage_ratio=self.risk_minimum_coverage_ratio,
            water_watch_ratio=self.risk_water_watch_ratio,
            water_station_max_age_hours=self.risk_water_station_max_age_hours,
            allow_mock_water_level_raise=self.risk_allow_mock_water_level_raise,
        )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
