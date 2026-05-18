from datetime import datetime
from typing import Protocol, cast

from app.ingestion.models import ForecastFrame, ForecastRun, FreshnessStatus

MongoDocument = dict[str, object]


class ForecastRepository(Protocol):
    """Store normalized forecast runs and frames."""

    async def upsert_run(self, run: ForecastRun) -> None:
        """Upsert a provider run record.

        Args:
            run: The forecast run to upsert.
        """
        ...

    async def upsert_frames(self, frames: list[ForecastFrame]) -> None:
        """Upsert normalized forecast frame records.

        Args:
            frames: List of forecast frames to upsert.
        """
        ...

    async def freshness_summary(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> MongoDocument:
        """Return a backend-facing freshness summary, optionally scoped to a provider/model.

        Args:
            provider: Filter by provider name. Defaults to ``None``.
            model: Filter by model name. Defaults to ``None``.

        Returns:
            Freshness document with status and metadata.
        """
        ...

    async def list_frames(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        area_name: str | None = None,
        valid_time_from: datetime | None = None,
        valid_time_to: datetime | None = None,
    ) -> list[ForecastFrame]:
        """List normalized forecast frames matching the supplied filters.

        Args:
            provider: Filter by provider name. Defaults to ``None``.
            model: Filter by model name. Defaults to ``None``.
            area_name: Filter by area name. Defaults to ``None``.
            valid_time_from: Lower bound on validTime. Defaults to ``None``.
            valid_time_to: Upper bound on validTime. Defaults to ``None``.

        Returns:
            List of ForecastFrame records matching the filters.
        """
        ...


class DryRunForecastRepository:
    """Keep forecast records in memory while preserving MongoDB document shape."""

    def __init__(self) -> None:
        self.runs: list[ForecastRun] = []
        self.frames: list[ForecastFrame] = []

    async def upsert_run(self, run: ForecastRun) -> None:
        """Store a run in memory with idempotent run id semantics.

        Args:
            run: The forecast run to upsert.
        """
        self.runs = [existing for existing in self.runs if existing.run_id != run.run_id]
        self.runs.append(run)

    async def upsert_frames(self, frames: list[ForecastFrame]) -> None:
        """Store frames in memory with idempotent frame id semantics.

        Args:
            frames: List of forecast frames to upsert.
        """
        incoming_ids = {frame.frame_id for frame in frames}
        self.frames = [
            existing for existing in self.frames if existing.frame_id not in incoming_ids
        ]
        self.frames.extend(frames)

    async def freshness_summary(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> MongoDocument:
        """Return a simple freshness document for API/operator visibility.

        Args:
            provider: Filter by provider name. Defaults to ``None``.
            model: Filter by model name. Defaults to ``None``.

        Returns:
            Freshness document with status and metadata.
        """
        candidates = [
            run
            for run in self.runs
            if (provider is None or run.provider.value == provider)
            and (model is None or run.model == model)
        ]
        if not candidates:
            return {
                "status": FreshnessStatus.FAILED,
                "reason": "no forecast runs stored",
            }

        latest_run = max(candidates, key=lambda run: run.run_time)
        frame_count = len([frame for frame in self.frames if frame.run_id == latest_run.run_id])
        return {
            "provider": latest_run.provider,
            "model": latest_run.model,
            "runTime": latest_run.run_time,
            "retrievedAt": latest_run.retrieved_at,
            "status": latest_run.freshness_status,
            "thresholdHours": latest_run.freshness_threshold_hours,
            "frameCount": frame_count,
        }

    async def list_frames(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        area_name: str | None = None,
        valid_time_from: datetime | None = None,
        valid_time_to: datetime | None = None,
    ) -> list[ForecastFrame]:
        """Return stored frames filtered by provider, model, area, and valid-time window.

        Args:
            provider: Filter by provider name. Defaults to ``None``.
            model: Filter by model name. Defaults to ``None``.
            area_name: Filter by area name. Defaults to ``None``.
            valid_time_from: Lower bound on validTime. Defaults to ``None``.
            valid_time_to: Upper bound on validTime. Defaults to ``None``.

        Returns:
            List of ForecastFrame records matching the filters.
        """
        result: list[ForecastFrame] = []
        for frame in self.frames:
            if provider is not None and frame.provider.value != provider:
                continue
            if model is not None and frame.model != model:
                continue
            if area_name is not None and frame.area.name != area_name:
                continue
            if valid_time_from is not None and frame.valid_time < valid_time_from:
                continue
            if valid_time_to is not None and frame.valid_time > valid_time_to:
                continue
            result.append(frame)
        result.sort(key=lambda frame: (frame.provider.value, frame.model, frame.valid_time))
        return result

    def mongo_preview(self) -> MongoDocument:
        """Render native-datetime documents as they would be passed to Motor/PyMongo."""
        return {
            "forecast_runs": [
                _to_mongo_document(run, by_alias=True)
                for run in sorted(self.runs, key=lambda item: item.run_id)
            ],
            "forecast_frames": [
                _to_mongo_document(frame, by_alias=True)
                for frame in sorted(self.frames, key=lambda item: item.frame_id)
            ],
        }


def _to_mongo_document(model: ForecastRun | ForecastFrame, by_alias: bool) -> MongoDocument:
    return cast(MongoDocument, model.model_dump(mode="python", by_alias=by_alias))
