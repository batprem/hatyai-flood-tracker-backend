"""MongoDB-backed implementation of :class:`ForecastRepository`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid

from app.ingestion.models import ForecastFrame, ForecastRun, FreshnessStatus
from app.ingestion.repository import MongoDocument, _to_mongo_document

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )


FORECAST_RUNS_COLLECTION = "forecast_runs"
FORECAST_FRAMES_COLLECTION = "forecast_frames"
FRAME_METADATA_FIELD = "metadata"


def _frame_to_mongo_document(frame: ForecastFrame) -> MongoDocument:
    """Render a frame document and attach the time-series metadata wrapper."""
    document = _to_mongo_document(frame, by_alias=True)
    document[FRAME_METADATA_FIELD] = {
        "provider": frame.provider.value,
        "model": frame.model,
        "area": {"name": frame.area.name},
    }
    return document


def _strip_frame_metadata(document: MongoDocument) -> MongoDocument:
    """Drop Mongo-only fields before re-validating into a Pydantic model."""
    document.pop("_id", None)
    document.pop(FRAME_METADATA_FIELD, None)
    return document


def _alias_to_field_map(model: type[BaseModel]) -> dict[str, str]:
    """Return a serialization-alias to field-name map for a Pydantic model."""
    mapping: dict[str, str] = {}
    for name, field in model.model_fields.items():
        alias = field.serialization_alias or field.alias or name
        mapping[alias] = name
    return mapping


def _decode_with_aliases(model: type[BaseModel], document: MongoDocument) -> MongoDocument:
    """Recursively rewrite ``document`` keys from serialization aliases to field names.

    Mongo drivers preserve native ``datetime`` values, but BSON dates are
    stored as UTC-naive on the wire. Some test backends (mongomock-motor)
    expose them without tzinfo, so naive datetimes are coerced to UTC here
    to satisfy :class:`ForecastFrame`'s timezone-aware validators.
    """
    aliases = _alias_to_field_map(model)
    decoded: MongoDocument = {}
    for key, value in document.items():
        field_name = aliases.get(key, key)
        if field_name not in model.model_fields:
            decoded[field_name] = value
            continue
        nested_model = _nested_model(model.model_fields[field_name].annotation)
        if nested_model is not None and isinstance(value, dict):
            decoded[field_name] = _decode_with_aliases(nested_model, cast(MongoDocument, value))
        elif isinstance(value, datetime) and value.tzinfo is None:
            decoded[field_name] = value.replace(tzinfo=UTC)
        else:
            decoded[field_name] = value
    return decoded


def _nested_model(annotation: object | None) -> type[BaseModel] | None:
    """Return the BaseModel subclass referenced by ``annotation`` when available."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


class MongoForecastRepository:
    """Persist normalized forecast runs and frames to MongoDB.

    Document shapes mirror :meth:`DryRunForecastRepository.mongo_preview` so the
    in-memory dry-run preview and Mongo persistence stay aligned. Run records
    use ``runId`` as the canonical primary key while frames use ``frameId``,
    and indexes guarantee idempotent upserts on ``(provider, model, runTime)``
    and ``frameId`` respectively.
    """

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._runs: AsyncIOMotorCollection = database[FORECAST_RUNS_COLLECTION]
        self._frames: AsyncIOMotorCollection = database[FORECAST_FRAMES_COLLECTION]

    @property
    def database(self) -> AsyncIOMotorDatabase:
        """Return the underlying Motor database handle."""
        return self._database

    async def ensure_indexes(self) -> None:
        """Create the time-series collection and indexes used by Phase 1.

        Notes:
            - ``forecast_frames`` is created as a time-series collection keyed
              on ``validTime`` with provider/model/area metadata fields.
            - ``forecast_runs`` gains a unique compound index on
              ``(provider, model, runTime)`` plus a ``runId`` lookup index.
            - Existing collections are tolerated: time-series creation is a
              no-op when the collection already exists.
        """
        await self._ensure_frames_timeseries()

        await self._runs.create_index(
            [("runId", ASCENDING)],
            name="runId_unique",
            unique=True,
        )
        await self._runs.create_index(
            [("provider", ASCENDING), ("model", ASCENDING), ("runTime", DESCENDING)],
            name="provider_model_runTime_unique",
            unique=True,
        )
        await self._frames.create_index(
            [("frameId", ASCENDING)],
            name="frameId_unique",
            unique=True,
        )
        await self._frames.create_index(
            [
                ("provider", ASCENDING),
                ("model", ASCENDING),
                ("area.name", ASCENDING),
                ("validTime", ASCENDING),
            ],
            name="provider_model_area_validTime",
        )

    async def upsert_run(self, run: ForecastRun) -> None:
        """Upsert a run document keyed by ``runId``."""
        document = _to_mongo_document(run, by_alias=True)
        await self._runs.replace_one(
            {"runId": run.run_id},
            document,
            upsert=True,
        )

    async def upsert_frames(self, frames: list[ForecastFrame]) -> None:
        """Replace frame documents idempotently keyed by ``frameId``.

        Time-series collections do not support ``replace_one`` upserts, so
        existing frames are deleted before the incoming batch is inserted.
        Each persisted document includes a top-level ``metadata`` field that
        mirrors ``provider``/``model``/``area.name`` so the time-series
        ``metaField`` can be honored without losing the dry-run preview shape.
        """
        if not frames:
            return

        frame_ids = [frame.frame_id for frame in frames]
        await self._frames.delete_many({"frameId": {"$in": frame_ids}})
        documents = [_frame_to_mongo_document(frame) for frame in frames]
        await self._frames.insert_many(documents)

    async def freshness_summary(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> MongoDocument:
        """Return the latest stored run and frame count for operator visibility."""
        query: MongoDocument = {}
        if provider is not None:
            query["provider"] = provider
        if model is not None:
            query["model"] = model

        latest = await self._runs.find_one(query, sort=[("runTime", DESCENDING)])
        if latest is None:
            return {
                "status": FreshnessStatus.FAILED,
                "reason": "no forecast runs stored",
            }

        frame_count = await self._frames.count_documents({"runId": latest["runId"]})
        summary: MongoDocument = {
            "provider": latest["provider"],
            "model": latest["model"],
            "runTime": latest["runTime"],
            "retrievedAt": latest["retrievedAt"],
            "status": latest["freshnessStatus"],
            "thresholdHours": latest["freshnessThresholdHours"],
            "frameCount": frame_count,
        }
        return summary

    async def list_frames(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        area_name: str | None = None,
        valid_time_from: datetime | None = None,
        valid_time_to: datetime | None = None,
    ) -> list[ForecastFrame]:
        """Return frames matching the supplied filters as Pydantic models."""
        query: MongoDocument = {}
        if provider is not None:
            query["provider"] = provider
        if model is not None:
            query["model"] = model
        if area_name is not None:
            query["area.name"] = area_name

        valid_time_clause: dict[str, datetime] = {}
        if valid_time_from is not None:
            valid_time_clause["$gte"] = valid_time_from
        if valid_time_to is not None:
            valid_time_clause["$lte"] = valid_time_to
        if valid_time_clause:
            query["validTime"] = cast(datetime, valid_time_clause)

        cursor = self._frames.find(query).sort(
            [("provider", ASCENDING), ("model", ASCENDING), ("validTime", ASCENDING)]
        )
        results: list[ForecastFrame] = []
        async for document in cursor:
            decoded = _decode_with_aliases(ForecastFrame, _strip_frame_metadata(document))
            results.append(ForecastFrame.model_validate(decoded))
        return results

    async def _ensure_frames_timeseries(self) -> None:
        """Create the ``forecast_frames`` time-series collection when missing.

        ``validTime`` is the time field. A top-level ``metadata`` sub-document
        carries ``provider``, ``model``, and ``area.name`` so MongoDB can use
        it as the time-series ``metaField`` while top-level fields remain
        queryable for filtering.
        """
        existing = await self._database.list_collection_names(
            filter={"name": FORECAST_FRAMES_COLLECTION}
        )
        if existing:
            return

        try:
            await self._database.create_collection(
                FORECAST_FRAMES_COLLECTION,
                timeseries={
                    "timeField": "validTime",
                    "metaField": FRAME_METADATA_FIELD,
                    "granularity": "hours",
                },
            )
        except (CollectionInvalid, NotImplementedError, TypeError):
            # Some test backends (e.g. mongomock-motor) do not implement the
            # time-series option; fall back to a regular collection so the
            # repository still works and indexes can be created normally.
            try:
                await self._database.create_collection(FORECAST_FRAMES_COLLECTION)
            except (CollectionInvalid, NotImplementedError):
                return


def build_mongo_repository(
    client: AsyncIOMotorClient, database_name: str
) -> MongoForecastRepository:
    """Create a :class:`MongoForecastRepository` bound to ``database_name``."""
    database = client[database_name]
    return MongoForecastRepository(database)
