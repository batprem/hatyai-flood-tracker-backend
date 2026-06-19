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
    risk_rainfall_24h_yellow_mm: float = 70
    risk_rainfall_24h_orange_mm: float = 120
    risk_rainfall_24h_red_mm: float = 180
    risk_rainfall_48h_yellow_mm: float = 110
    risk_rainfall_48h_orange_mm: float = 175
    risk_rainfall_48h_red_mm: float = 275
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
    ecmwf_base_url: str = Field(
        default="https://data.ecmwf.int/forecasts",
        validation_alias=AliasChoices("HFT_ECMWF_BASE_URL", "ECMWF_BASE_URL"),
        description="Base CDN URL for ECMWF Open Data IFS real-time forecasts.",
    )
    ecmwf_freshness_threshold_hours: int = Field(
        default=13,
        validation_alias=AliasChoices(
            "HFT_ECMWF_FRESHNESS_THRESHOLD_HOURS", "ECMWF_FRESHNESS_THRESHOLD_HOURS"
        ),
        description=(
            "Hours after a nominal IFS run time before the run is considered stale. "
            "ECMWF publishes results ~9-12 h after the nominal run time; 13 h allows "
            "for typical dissemination delays."
        ),
    )
    ecmwf_http_timeout_seconds: float = Field(
        default=120.0,
        validation_alias=AliasChoices(
            "HFT_ECMWF_HTTP_TIMEOUT_SECONDS", "ECMWF_HTTP_TIMEOUT_SECONDS"
        ),
        description=(
            "HTTP timeout in seconds for ECMWF Open Data downloads. "
            "Files are global 0.25-degree GRIB2 (1-3 MB); 120 s covers slow CDN responses."
        ),
    )
    thaiwater_base_url: str = Field(
        default="https://api-v3.thaiwater.net/api/v1/thaiwater30/public",
        validation_alias=AliasChoices("HFT_THAIWATER_BASE_URL", "THAIWATER_BASE_URL"),
        description=(
            "Base URL for the ThaiWater / HAII national hydroinformatics public API. "
            "The Phase 1 station endpoints are appended (e.g. /waterlevel_load)."
        ),
    )
    thaiwater_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HFT_THAIWATER_API_KEY", "THAIWATER_API_KEY"),
        description=(
            "Optional bearer token / API key for ThaiWater. The public read-only "
            "endpoints used in Phase 1 do not require credentials, but setting this "
            "enables authenticated tiers when access is granted by HAII."
        ),
    )
    thaiwater_max_age_hours: float = Field(
        default=3.0,
        validation_alias=AliasChoices("HFT_THAIWATER_MAX_AGE_HOURS", "THAIWATER_MAX_AGE_HOURS"),
        description=(
            "Maximum age (hours) of a ThaiWater station observation before it is "
            "treated as stale and excluded from the public response. Aligns with the "
            "risk engine's water_station_max_age_hours threshold."
        ),
    )
    thaiwater_timeout_seconds: float = Field(
        default=10.0,
        validation_alias=AliasChoices("HFT_THAIWATER_TIMEOUT_SECONDS", "THAIWATER_TIMEOUT_SECONDS"),
        description=(
            "HTTP timeout in seconds for ThaiWater requests. Public JSON responses "
            "are small (<1 MB) so the default is short to fail fast on outages."
        ),
    )
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
    data_quality_gfs_max_age_hours: float = Field(
        default=6.0,
        gt=0,
        validation_alias=AliasChoices(
            "HFT_DATA_QUALITY_GFS_MAX_AGE_HOURS", "DATA_QUALITY_GFS_MAX_AGE_HOURS"
        ),
        description=(
            "Maximum age (hours) of the latest successful GFS run before the data-quality "
            "monitor flags it as stale. Used by the /health data_quality block and the "
            "ingestion scheduler's stale-data alert."
        ),
    )
    data_quality_ecmwf_max_age_hours: float = Field(
        default=12.0,
        gt=0,
        validation_alias=AliasChoices(
            "HFT_DATA_QUALITY_ECMWF_MAX_AGE_HOURS", "DATA_QUALITY_ECMWF_MAX_AGE_HOURS"
        ),
        description=(
            "Maximum age (hours) of the latest successful ECMWF run before the data-quality "
            "monitor flags it as stale. ECMWF disseminates ~9-12 h after the nominal run time."
        ),
    )
    data_quality_station_max_age_hours: float = Field(
        default=3.0,
        gt=0,
        validation_alias=AliasChoices(
            "HFT_DATA_QUALITY_STATION_MAX_AGE_HOURS", "DATA_QUALITY_STATION_MAX_AGE_HOURS"
        ),
        description=(
            "Maximum age (hours) of the latest station observation before the data-quality "
            "monitor flags it as stale. Aligns with thaiwater_max_age_hours by default."
        ),
    )
    line_notify_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "HFT_LINE_MESSAGING_API_TOKEN",
            "LINE_MESSAGING_API_TOKEN",
            "HFT_LINE_NOTIFY_TOKEN",
            "LINE_NOTIFY_TOKEN",
        ),
        description=(
            "LINE Messaging API channel access token used to broadcast flood alerts. "
            "Accepts the legacy LINE_NOTIFY_TOKEN alias for backward compatibility. "
            "When empty the alert dispatcher logs a warning and skips sending so "
            "non-production environments do not error. Never commit a real token to source."
        ),
    )
    line_ops_token: str = Field(
        default="",
        validation_alias=AliasChoices("HFT_LINE_OPS_TOKEN", "LINE_OPS_TOKEN"),
        description=(
            "LINE Messaging API channel access token for the operator alert channel. "
            "Separate from the public channel token so ops noise never reaches the public. "
            "When empty LINE ops delivery is skipped and a warning is logged."
        ),
    )
    line_notify_cooldown_hours: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices(
            "HFT_LINE_NOTIFY_COOLDOWN_HOURS", "LINE_NOTIFY_COOLDOWN_HOURS"
        ),
        description=(
            "Minimum hours between LINE alerts for the same risk level. A further "
            "upward transition (e.g. orange to red) bypasses the cooldown."
        ),
    )
    line_notify_dashboard_url: str = Field(
        default="https://hatyai-flood-warning.vercel.app",
        validation_alias=AliasChoices("HFT_LINE_NOTIFY_DASHBOARD_URL", "LINE_NOTIFY_DASHBOARD_URL"),
        description="Public dashboard URL appended to outgoing LINE alert messages.",
    )
    alerts_test_token: str = Field(
        default="",
        validation_alias=AliasChoices("HFT_ALERTS_TEST_TOKEN", "ALERTS_TEST_TOKEN"),
        description=(
            "Bearer token protecting POST /api/alerts/test. When empty the endpoint "
            "rejects every request so a misconfigured deploy cannot be triggered."
        ),
    )
    reports_moderation_token: str = Field(
        default="",
        validation_alias=AliasChoices("HFT_REPORTS_MODERATION_TOKEN", "REPORTS_MODERATION_TOKEN"),
        description=(
            "Bearer token protecting the citizen-report moderation endpoints "
            "(list pending, approve, reject). When empty every moderation request "
            "is rejected, mirroring ALERTS_TEST_TOKEN, so a misconfigured deploy "
            "cannot expose moderation."
        ),
    )
    reports_rate_limit_per_hour: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices(
            "HFT_REPORTS_RATE_LIMIT_PER_HOUR", "REPORTS_RATE_LIMIT_PER_HOUR"
        ),
        description=(
            "Maximum citizen reports a single submitter IP may file per rolling "
            "hour. Enforced with a Mongo-backed counter keyed on a salted hash of "
            "the IP; the raw IP is never persisted."
        ),
    )
    reports_ip_hash_salt: str = Field(
        default="hatyai-flood-reports",
        validation_alias=AliasChoices("HFT_REPORTS_IP_HASH_SALT", "REPORTS_IP_HASH_SALT"),
        description=(
            "Salt mixed into the SHA-256 hash of the submitter IP used as the "
            "rate-limit key. Set a deployment-specific value so rate-limit keys "
            "cannot be precomputed. The raw IP is never stored."
        ),
    )
    vapid_private_key: str = Field(
        default="",
        validation_alias=AliasChoices("HFT_VAPID_PRIVATE_KEY", "VAPID_PRIVATE_KEY"),
        description=(
            "Base64url-encoded VAPID private key used to sign Web Push requests. When "
            "empty the Web Push dispatcher logs a warning and skips sending so "
            "non-production environments do not error. Never commit a real key. "
            "Generate with `uv run python scripts/generate_vapid_keys.py`."
        ),
    )
    vapid_public_key: str = Field(
        default="",
        validation_alias=AliasChoices("HFT_VAPID_PUBLIC_KEY", "VAPID_PUBLIC_KEY"),
        description=(
            "Base64url-encoded VAPID public key (the browser applicationServerKey) "
            "served by GET /api/alerts/vapid-public-key so the frontend can subscribe."
        ),
    )
    vapid_subject: str = Field(
        default="mailto:admin@hatyai-flood.example.com",
        validation_alias=AliasChoices("HFT_VAPID_SUBJECT", "VAPID_SUBJECT"),
        description=(
            "VAPID 'sub' claim identifying the application server contact. Must be a "
            "mailto: or https: URI per the Web Push VAPID spec."
        ),
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
