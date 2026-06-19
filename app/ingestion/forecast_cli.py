import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import JsonValue

from app.core.config import ForecastRepositoryBackend, Settings, get_settings
from app.ingestion.models import (
    ForecastFrame,
    ForecastProvider,
    ForecastRun,
    ForecastRunStatus,
    FreshnessStatus,
)
from app.ingestion.mongo_repository import MongoForecastRepository, build_mongo_repository
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import (
    DryRunForecastRepository,
    ForecastRepository,
)
from app.ingestion.station_repository import build_mongo_station_repository
from app.ingestion.subscription_repository import build_mongo_subscription_repository
from app.services.alert_dispatch import dispatch_risk_alert, dispatch_web_push_alert
from app.services.data_quality import evaluate_and_alert
from app.services.forecast_frames import DEFAULT_AREA_NAME
from app.services.ops_notifier import LineOpsNotifier
from app.services.risk_rules import (
    build_rainfall_inputs_from_frames,
    calculate_current_risk,
)
from app.services.web_push import VapidConfig

logger = logging.getLogger(__name__)


def _cycle_run_id(provider: ForecastProvider, retrieved_at: datetime) -> str:
    """Build a stable run identifier when no provider run was discovered.

    Used to persist a failure record when discovery itself errors out, so the
    failure is visible to operators in ``forecast_runs`` instead of vanishing.
    """
    cycle = retrieved_at.astimezone(UTC).strftime("%Y%m%d%H")
    return f"{provider.value}:discovery-failed:{cycle}"


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
        include_mongo_preview: When True, include the native MongoDB document shapes in
            the returned payload.
        repository: Optional pre-built dry-run repository for tests.
        use_fixtures: Force fixture-backed clients for every provider so the CLI can run offline.

    Returns:
        Dictionary with ingestion results including runs, frames, failures, and freshness metadata.
    """
    repo = repository if repository is not None else DryRunForecastRepository()
    runs, frames, failures = await _ingest_into_repository(
        repo,
        providers=providers,
        forecast_hours=forecast_hours,
        use_fixtures=use_fixtures,
    )

    payload: dict[str, JsonValue] = {
        "mode": "dry-run",
        "runs": runs,
        "frames": frames,
        "failures": failures,
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
        use_fixtures: Force fixture-backed clients for every provider so the CLI can run offline.

    Returns:
        Dictionary with ingestion results including runs, frames, failures, and freshness metadata.
    """
    settings = get_settings()
    client = AsyncIOMotorClient(mongodb_uri)
    alert_reason: str | None = None
    try:
        repository = build_mongo_repository(client, mongodb_database)
        await repository.ensure_indexes()
        station_repository = build_mongo_station_repository(client, mongodb_database)
        await station_repository.ensure_indexes()
        runs, frames, failures = await _ingest_into_repository(
            repository,
            providers=providers,
            forecast_hours=forecast_hours,
            use_fixtures=use_fixtures,
        )
        freshness = to_json_value(await repository.freshness_summary())
        # Pipeline observability (HFT-75): detect ingestion failures and
        # staleness breaches and dispatch each one through the OpsNotifier
        # interface. LineOpsNotifier (HFT-81) logs structured JSON and also
        # broadcasts to the LINE ops channel when a token is configured.
        await evaluate_and_alert(
            repository,
            station_repository,
            settings,
            notifier=LineOpsNotifier(settings.line_ops_token),
        )
        alert_reason = await _evaluate_and_dispatch_alert(repository, settings=get_settings())
    finally:
        client.close()

    payload: dict[str, JsonValue] = {
        "mode": "mongo",
        "database": mongodb_database,
        "runs": runs,
        "frames": frames,
        "failures": failures,
        "freshness": freshness,
        "alert": alert_reason,
    }
    return payload


async def _evaluate_and_dispatch_alert(
    repository: MongoForecastRepository,
    *,
    settings: Settings,
) -> str | None:
    """Compute basin risk from stored frames and dispatch a LINE alert when warranted.

    Risk is derived from the freshly persisted forecast frames only; the
    scheduler has no live water-station client, which is sufficient for the
    rainfall-driven basin risk used to gate alerts. Both the LINE Notify and
    Web Push channels fire on the same risk transition, each tracking its own
    edge-triggered state. Any failure here is logged and swallowed so an
    alerting problem never fails an otherwise-successful ingestion run.

    Args:
        repository: Mongo-backed forecast repository holding the latest frames.
        settings: Application settings carrying the alert tokens, VAPID keys,
            and cooldown.

    Returns:
        The LINE dispatch decision reason, or ``None`` when no alert was
        evaluated (for example when no frames are available).
    """
    try:
        frames = await repository.list_frames(area_name=DEFAULT_AREA_NAME)
        rainfall_inputs = build_rainfall_inputs_from_frames(frames)
        if not rainfall_inputs:
            logger.info("alert evaluation skipped: no rainfall inputs from stored frames")
            return None
        risk = calculate_current_risk(
            forecasts=rainfall_inputs,
            water_levels=[],
            settings=settings.risk_rule_settings(),
            generated_at=datetime.now(UTC),
        )
        decision = await dispatch_risk_alert(
            database=repository.database,
            current_level=risk.level,
            valid_at=risk.freshness.valid_at,
            token=settings.line_notify_token,
            cooldown_hours=settings.line_notify_cooldown_hours,
            dashboard_url=settings.line_notify_dashboard_url,
        )
        subscription_repository = build_mongo_subscription_repository(
            repository.database.client, repository.database.name
        )
        await dispatch_web_push_alert(
            database=repository.database,
            repository=subscription_repository,
            current_level=risk.level,
            valid_at=risk.freshness.valid_at,
            vapid_config=VapidConfig(
                private_key=settings.vapid_private_key,
                subject=settings.vapid_subject,
            ),
            cooldown_hours=settings.line_notify_cooldown_hours,
            dashboard_url=settings.line_notify_dashboard_url,
        )
    except Exception:  # pragma: no cover - defensive: alerting must not fail ingestion
        logger.exception("alert evaluation failed; ingestion run is unaffected")
        return None
    return decision.reason


async def _ingest_into_repository(
    repository: ForecastRepository,
    *,
    providers: list[ForecastProvider],
    forecast_hours: list[int],
    use_fixtures: bool,
) -> tuple[list[JsonValue], list[JsonValue], list[JsonValue]]:
    """Ingest each provider's latest run into ``repository`` and return JSON copies.

    Per-provider failure is contained: a provider that raises during discovery
    or fetch records a ``FAILED`` run with ``error_reason`` and the loop
    continues. The persisted run carries ``status=STORED`` only when every
    requested forecast hour produced a frame, ``status=PARTIAL`` when the
    provider returned fewer frames than requested but at least one frame, and
    ``status=FAILED`` when the provider produced nothing.

    Args:
        repository: Repository to persist ingested records into.
        providers: Forecast providers to ingest.
        forecast_hours: Forecast hours to request from each provider.
        use_fixtures: Force fixture-backed clients for testing.

    Returns:
        Tuple of ``(runs, frames, failures)`` where ``runs`` includes both
        successful and failed run records and ``failures`` is a list of
        per-provider failure descriptors for easy detection. The list is
        empty on full success.
    """
    runs: list[JsonValue] = []
    frames: list[JsonValue] = []
    failures: list[JsonValue] = []
    retrieved_at = datetime.now(UTC)
    requested_hours = sorted(set(forecast_hours))

    for provider in providers:
        try:
            run, normalized_frames = await _ingest_one_provider(
                provider=provider,
                forecast_hours=requested_hours,
                retrieved_at=retrieved_at,
                use_fixtures=use_fixtures,
            )
        except Exception as exc:  # pragma: no cover - exercised via tests below
            logger.exception("provider %s ingestion failed", provider.value)
            failed_run = _build_failed_run_record(
                provider=provider,
                forecast_hours=requested_hours,
                retrieved_at=retrieved_at,
                error_reason=f"{type(exc).__name__}: {exc}",
            )
            await repository.upsert_run(failed_run)
            run_doc = failed_run.model_dump(mode="json", by_alias=True)
            runs.append(run_doc)
            failures.append(
                {
                    "provider": provider.value,
                    "errorReason": failed_run.error_reason,
                    "runId": failed_run.run_id,
                }
            )
            continue

        await repository.upsert_run(run)
        await repository.upsert_frames(normalized_frames)
        runs.append(run.model_dump(mode="json", by_alias=True))
        frames.extend(frame.model_dump(mode="json", by_alias=True) for frame in normalized_frames)

    return runs, frames, failures


async def _ingest_one_provider(
    *,
    provider: ForecastProvider,
    forecast_hours: list[int],
    retrieved_at: datetime,
    use_fixtures: bool,
) -> tuple[ForecastRun, list[ForecastFrame]]:
    """Discover, fetch, and normalize one provider; classify run status by coverage.

    The current provider clients are atomic per run: ``fetch_run`` either
    returns every requested forecast hour or raises. Classification still
    distinguishes ``STORED`` (full coverage), ``PARTIAL`` (some artifacts
    but fewer than requested), and ``FAILED`` (no artifacts) so downstream
    operators can see partial coverage if a future client returns fewer
    frames without raising.
    """
    client = build_provider_client(provider, forecast_hours, use_fixtures=use_fixtures)
    run_ref = client.discover_latest_run(retrieved_at)
    artifacts = client.fetch_run(run_ref)
    normalized_frames = normalize_frames(run_ref, artifacts, retrieved_at)

    base_run = build_run_record(run_ref, artifacts, retrieved_at)
    requested_count = len(forecast_hours)
    produced_count = len(artifacts)
    if produced_count == requested_count and produced_count > 0:
        status = ForecastRunStatus.STORED
        error_reason: str | None = None
    elif produced_count > 0:
        status = ForecastRunStatus.PARTIAL
        error_reason = (
            f"provider returned {produced_count} of {requested_count} requested forecast hours"
        )
    else:
        status = ForecastRunStatus.FAILED
        error_reason = "provider returned no forecast artifacts"

    run = base_run.model_copy(update={"status": status, "error_reason": error_reason})
    return run, normalized_frames


def _build_failed_run_record(
    *,
    provider: ForecastProvider,
    forecast_hours: list[int],
    retrieved_at: datetime,
    error_reason: str,
) -> ForecastRun:
    """Build a placeholder ``FAILED`` run when a provider raises before normalization.

    The record is intentionally minimal: there is no run_time to record from
    the provider so we fall back to ``retrieved_at`` and emit a stable
    discovery-failed run id so the run can be re-attempted idempotently on
    the next cron tick.
    """
    return ForecastRun(
        run_id=_cycle_run_id(provider, retrieved_at),
        provider=provider,
        model=provider.value,
        product="unknown",
        run_time=retrieved_at,
        retrieved_at=retrieved_at,
        processed_at=retrieved_at,
        expected_forecast_hours=forecast_hours,
        source_urls=[],
        status=ForecastRunStatus.FAILED,
        freshness_status=FreshnessStatus.FAILED,
        freshness_threshold_hours=1,
        license="review-required",
        attribution=f"{provider.value} provider — ingestion failed before discovery",
        error_reason=error_reason,
    )


def parse_forecast_hours(value: str) -> list[int]:
    """Parse a comma-separated forecast-hour list.

    Args:
        value: Comma-separated string of positive integers.

    Returns:
        List of validated positive forecast hours.

    Raises:
        argparse.ArgumentTypeError: When values are not integers or not positive.
    """
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
    """Parse provider selection for the dry-run CLI.

    Args:
        value: Provider name or 'all' to select all providers.

    Returns:
        List of selected ForecastProvider enums.

    Raises:
        argparse.ArgumentTypeError: When provider name is not recognized.
    """
    if value == "all":
        return [ForecastProvider.GFS, ForecastProvider.ECMWF_OPEN_DATA]
    try:
        return [ForecastProvider(value)]
    except ValueError as exc:
        valid = ", ".join([provider.value for provider in ForecastProvider] + ["all"])
        msg = f"provider must be one of: {valid}"
        raise argparse.ArgumentTypeError(msg) from exc


def to_json_value(value: object) -> JsonValue:
    """Convert native-datetime Mongo preview objects to JSON-safe values.

    Args:
        value: A value that may contain datetime, enum, or nested structures.

    Returns:
        JSON-serializable value with datetimes as ISO 8601 strings and enums as their string values.
    """
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
    """Run the forecast ingestion CLI against the configured backend.

    Exits non-zero whenever any provider invocation fails so the Cloud Run
    Job surfaces failed runs as failed executions. A ``failed`` ``forecast_runs``
    row is still written for each failed provider before exit.
    """
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

    failures = payload.get("failures")
    if isinstance(failures, list) and failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
