from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configure runtime settings from environment variables."""

    app_name: str = "Hat Yai Flood Warning API"
    environment: str = "development"
    api_prefix: str = "/api"
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
        description="Comma-separated list of allowed frontend origins.",
    )
    cors_origin_regex: str | None = Field(
        default=r"https://.*\.vercel\.app",
        description="Optional regex for deployment preview origins.",
    )

    model_config = SettingsConfigDict(
        env_file="dev.env",
        env_file_encoding="utf-8",
        env_prefix="HFT_",
        extra="ignore",
    )

    def allowed_cors_origins(self) -> list[str]:
        """Return configured CORS origins as a clean list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
