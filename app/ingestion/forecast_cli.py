import argparse
import asyncio
import json
from datetime import UTC, datetime
from enum import StrEnum

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import JsonValue

from app.core.config import ForecastRepositoryBackend, get_settings
from app.ingestion.models import ForecastProvider, ForecastRunStatus
from app.ingestion.mongo_repository import build_mongo_repository
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import (
    DryRunForecastRepository,
    ForecastRepository,
)


async def run_dry_ingestion(
    providers: list[ForecastProvider],
    forecast_hours: list[int],
    include_mongo_preview: bool,
    repository: DryRunForecastRepository | None = None,
    *,
    use_fixtures: bool = False,
) -> dict[str, JsonValue]:
    """Run provider discovery, fetch, normalize, and dry-run storage.

    Args:
        providers: Providers to ingest in this run.
        forecast_hours: Forecast hours to request from each provider.
        include_mongo_preview: When True, include the native MongoDB document
            shapes in the returned payload.
        repository: Optional pre-built dry-run repository for tests.
        use_fixtures: Force fixture-backed clients for every provider so the
            CLI can run offline. The default uses real network clients where
            available (currently GFS).
    """
    repo = repository if repository is not None else DryRunForecastRepository()
    runs, frames = await _ingest_into_repository(
        repo,
        providers=providers,
        forecast_hours=forecast_hours,
        use_fixtures=use_fixtures,
    )

    payload: dict[str, JsonValue] = {
        "mode": "dry-run",
        "runs": runs,
        "frames": frames,
        "freshness": to_json_value(await repo.freshness_summary()),
    }
    if include_mongo_preview:
        payload["mongoPreview"] = to_json_value(repo.mongo_preview())
    return payload


async def run_mongo_ingestion(
    providers: list[ForecastProvider],
    forecast_hours: list[int],
    *,
    mongodb_uri: str,
    mongodb_database: str,
    use_fixtures: bool = False,
) -> dict[str, JsonValue]:
    """Run ingestion against a MongoDB-backed repository for cron pipelines.

    Args:
        providers: Providers to ingest in this run.
        forecast_hours: Forecast hours to request from each provider.
        mongodb_uri: MongoDB connection URI.
        mongodb_database: Database name to persist into.
        use_fixtures: Force fixture-backed clients for every provider so the
            CLI can run offline.
    """
    client = AsyncIOMotorClient(mongodb_uri)
    try:
        repository = build_mongo_repository(client, mongodb_database)
        await repository.ensure_indexes()
        runs, frames = await _ingest_into_repository(
            repository,
            providers=providers,
            forecast_hours=forecast_hours,
            use_fixtures=use_fixtures,
        )
        freshness = to_json_value(await repository.freshness_summary())
    finally:
        client.close()

    payload: dict[str, JsonValue] = {
        "mode": "mongo",
        "database": mongodb_database,
        "runs": runs,
        "frames": frames,
        "freshness": freshness,
    }
    return payload


async def _ingest_into_repository(
    repository: ForecastRepository,
    *,
    providers: list[ForecastProvider],
    forecast_hours: list[int],
    use_fixtures: bool,
) -> tuple[list[JsonValue], list[JsonValue]]:
    """Ingest each provider's latest run into ``repository`` and return JSON copies."""
    runs: list[JsonValue] = []
    frames: list[JsonValue] = []
    retrieved_at = datetime.now(UTC)

    for provider in providers:
        client = build_provider_client(provider, forecast_hours, use_fixtures=use_fixtures)
        run_ref = client.discover_latest_run(retrieved_at)
        artifacts = client.fetch_run(run_ref)
        run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(
            update={"status": ForecastRunStatus.STORED}
        )
        normalized_frames = normalize_frames(run_ref, artifacts, retrieved_at)

        await repository.upsert_run(run)
        await repository.upsert_frames(normalized_frames)

        runs.append(run.model_dump(mode="json", by_alias=True))
        frames.extend(frame.model_dump(mode="json", by_alias=True) for frame in normalized_frames)

    return runs, frames


def parse_forecast_hours(value: str) -> list[int]:
    """Parse a comma-separated forecast-hour list."""
    try:
        hours = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        msg = "forecast hours must be comma-separated integers"
        raise argparse.ArgumentTypeError(msg) from exc

    if not hours or any(hour <= 0 for hour in hours):
        msg = "forecast hours must be positive integers"
        raise argparse.ArgumentTypeError(msg)
    return hours


def parse_provider(value: str) -> list[ForecastProvider]:
    """Parse provider selection for the dry-run CLI."""
    if value == "all":
        return [ForecastProvider.GFS, ForecastProvider.ECMWF_OPEN_DATA]
    try:
        return [ForecastProvider(value)]
    except ValueError as exc:
        valid = ", ".join([provider.value for provider in ForecastProvider] + ["all"])
        msg = f"provider must be one of: {valid}"
        raise argparse.ArgumentTypeError(msg) from exc


def to_json_value(value: object) -> JsonValue:
    """Convert native-datetime Mongo preview objects to JSON-safe values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    """Build the forecast ingestion CLI parser."""
    parser = argparse.ArgumentParser(
        description="Run a small GFS/ECMWF forecast ingestion job.",
    )
    parser.add_argument(
        "--provider",
        default="gfs",
        type=parse_provider,
        help="Provider to run: gfs, ecmwf_open_data, or all.",
    )
    parser.add_argument(
        "--forecast-hours",
        default="6,12",
        type=parse_forecast_hours,
        help="Comma-separated positive forecast hours to normalize.",
    )
    parser.add_argument(
        "--mongo-preview",
        action="store_true",
        help="Include native MongoDB document shapes converted to JSON for display.",
    )
    parser.add_argument(
        "--use-fixtures",
        action="store_true",
        help=(
            "Force fixture-backed provider clients (no network). Use this for offline "
            "development and CI runs."
        ),
    )
    parser.add_argument(
        "--mongo",
        action="store_true",
        help=(
            "Persist runs and frames into the configured MongoDB repository instead "
            "of running in-memory. Equivalent to HFT_FORECAST_REPOSITORY_BACKEND=mongo."
        ),
    )
    return parser


def main() -> None:
    """Run the forecast ingestion CLI against the configured backend."""
    args = build_parser().parse_args()
    settings = get_settings()
    use_mongo = args.mongo or (
        settings.forecast_repository_backend is ForecastRepositoryBackend.MONGO
    )

    if use_mongo:
        payload = asyncio.run(
            run_mongo_ingestion(
                providers=args.provider,
                forecast_hours=args.forecast_hours,
                mongodb_uri=settings.mongodb_uri,
                mongodb_database=settings.mongodb_database,
                use_fixtures=args.use_fixtures,
            )
        )
    else:
        payload = asyncio.run(
            run_dry_ingestion(
                providers=args.provider,
                forecast_hours=args.forecast_hours,
                include_mongo_preview=args.mongo_preview,
                use_fixtures=args.use_fixtures,
            )
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
