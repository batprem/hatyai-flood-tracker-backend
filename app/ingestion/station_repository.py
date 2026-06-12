"""MongoDB persistence for normalized station observations.

A small repository indirection mirrors the pattern used by
:mod:`app.ingestion.mongo_repository` for forecast frames. The
``station_observations`` collection is a MongoDB time-series collection
keyed on ``observed_at`` with ``station_id`` as the metadata field.

Design notes:

- ``observed_at`` is the time field. Time-series storage is the right
  default for telemetry that grows continuously and is queried by time
  range or "latest per station".
- ``station_id`` is the metadata field so MongoDB can colocate documents
  per station for efficient per-station range queries.
- Compound index on ``(station_id, observed_at desc)`` accelerates the
  common "latest observation per station" lookup.
- ``2dsphere`` index on ``location`` (a GeoJSON Point) supports future
  basin-polygon / radius queries for map overlays.
- Writes are idempotent: an exact ``(station_id, observed_at)`` match is
  replaced rather than duplicated so the route can write-through on every
  successful provider fetch without growing the collection unboundedly.

For Phase 1, the repository is optional: the API endpoint still works in
dry-run mode without a real Mongo connection. Persistence is enabled only
when ``HFT_FORECAST_REPOSITORY_BACKEND=mongo`` so the same envelope flag
controls forecast and station storage.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pymongo import ASCENDING, DESCENDING, GEOSPHERE
from pymongo.errors import CollectionInvalid

from app.ingestion.thaiwater_client import StationObservation

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

logger = logging.getLogger(__name__)


STATION_OBSERVATIONS_COLLECTION = "station_observations"
STATION_METADATA_FIELD = "station_id"
STATION_TIME_FIELD = "observed_at"


class StationObservationRepository(Protocol):
    """Persist normalized station observations."""

    async def ensure_indexes(self) -> None:
        """Create the time-series collection and supporting indexes."""
        ...

    async def upsert_many(self, observations: list[StationObservation]) -> None:
        """Persist a batch of observations idempotently.

        Args:
            observations: List of station observations to persist.
        """
        ...

    async def latest_per_station(
        self,
        *,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return the most recent observation per station, optionally filtered.

        Args:
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            List of the latest StationObservation records per station.
        """
        ...

    async def list_between(
        self,
        *,
        observed_from: datetime,
        observed_to: datetime,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return observations in a half-open ``observed_at`` time range.

        Args:
            observed_from: Inclusive lower bound on ``observed_at`` (UTC).
            observed_to: Exclusive upper bound on ``observed_at`` (UTC).
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            Matching observations sorted by ``(station_id, observed_at)``.
        """
        ...


class DryRunStationRepository:
    """Keep station observations in memory.

    Used by the dry-run backend and unit tests so the API can be exercised
    without a Mongo instance. Behavior mirrors the persistence semantics of
    the Mongo-backed repository.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, datetime], StationObservation] = {}

    async def ensure_indexes(self) -> None:
        """No-op for the in-memory backend."""

    async def upsert_many(self, observations: list[StationObservation]) -> None:
        """Replace existing records keyed on (station_id, observed_at).

        Args:
            observations: List of station observations to upsert.
        """
        for record in observations:
            self._records[(record.station_id, record.observed_at)] = record

    async def latest_per_station(
        self,
        *,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return the latest observation per station from the in-memory store.

        Args:
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            List of the latest StationObservation records per station.
        """
        latest: dict[str, StationObservation] = {}
        for record in self._records.values():
            if station_ids is not None and record.station_id not in station_ids:
                continue
            existing = latest.get(record.station_id)
            if existing is None or record.observed_at > existing.observed_at:
                latest[record.station_id] = record
        return sorted(latest.values(), key=lambda obs: obs.station_id)

    async def list_between(
        self,
        *,
        observed_from: datetime,
        observed_to: datetime,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return in-memory observations in a half-open ``observed_at`` range.

        Args:
            observed_from: Inclusive lower bound on ``observed_at`` (UTC).
            observed_to: Exclusive upper bound on ``observed_at`` (UTC).
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            Matching observations sorted by ``(station_id, observed_at)``.
        """
        selected = [
            record
            for record in self._records.values()
            if observed_from <= record.observed_at < observed_to
            and (station_ids is None or record.station_id in station_ids)
        ]
        selected.sort(key=lambda obs: (obs.station_id, obs.observed_at))
        return selected


class MongoStationRepository:
    """Persist station observations to a MongoDB time-series collection."""

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._collection: AsyncIOMotorCollection = database[STATION_OBSERVATIONS_COLLECTION]

    @property
    def collection(self) -> AsyncIOMotorCollection:
        """Return the underlying Motor collection handle."""
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the time-series collection and indexes used by Phase 1.

        Falls back to a regular collection on backends (e.g. ``mongomock``)
        that do not implement the ``timeseries`` option, so unit tests can
        exercise the persistence path without a real Mongo deployment.
        """
        existing = await self._database.list_collection_names(
            filter={"name": STATION_OBSERVATIONS_COLLECTION}
        )
        if not existing:
            try:
                await self._database.create_collection(
                    STATION_OBSERVATIONS_COLLECTION,
                    timeseries={
                        "timeField": STATION_TIME_FIELD,
                        "metaField": STATION_METADATA_FIELD,
                        "granularity": "minutes",
                    },
                )
            except (CollectionInvalid, NotImplementedError, TypeError):
                try:
                    await self._database.create_collection(STATION_OBSERVATIONS_COLLECTION)
                except (CollectionInvalid, NotImplementedError):
                    return

        # Latest-per-station lookup index.
        await self._collection.create_index(
            [(STATION_METADATA_FIELD, ASCENDING), (STATION_TIME_FIELD, DESCENDING)],
            name="station_id_observed_at",
        )
        # Geospatial index for map-driven queries.
        try:
            await self._collection.create_index(
                [("location", GEOSPHERE)],
                name="location_2dsphere",
            )
        except NotImplementedError:
            # Some mock backends do not support 2dsphere; the route does not
            # depend on it, so we degrade silently.
            pass

    async def upsert_many(self, observations: list[StationObservation]) -> None:
        """Idempotently persist a batch keyed on (station_id, observed_at).

        Time-series collections in MongoDB do not support ``update`` or
        ``replace_one`` with the standard query path. We instead delete the
        matching keys and insert the batch, keeping each ``(station, time)``
        pair unique.

        Args:
            observations: List of station observations to upsert.
        """
        if not observations:
            return
        keys = [
            {STATION_METADATA_FIELD: obs.station_id, STATION_TIME_FIELD: obs.observed_at}
            for obs in observations
        ]
        await self._collection.delete_many({"$or": keys})
        documents = [_observation_to_document(obs) for obs in observations]
        await self._collection.insert_many(documents)

    async def latest_per_station(
        self,
        *,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return the most recent record per station via an aggregation pipeline.

        Args:
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            List of the latest StationObservation records per station.
        """
        match: dict[str, object] = {}
        if station_ids is not None:
            match[STATION_METADATA_FIELD] = {"$in": station_ids}
        pipeline: list[dict[str, object]] = []
        if match:
            pipeline.append({"$match": match})
        pipeline.extend(
            [
                {"$sort": {STATION_TIME_FIELD: -1}},
                {"$group": {"_id": f"${STATION_METADATA_FIELD}", "doc": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$doc"}},
                {"$sort": {STATION_METADATA_FIELD: 1}},
            ]
        )
        cursor = self._collection.aggregate(pipeline)
        results: list[StationObservation] = []
        async for document in cursor:
            results.append(_document_to_observation(document))
        return results

    async def list_between(
        self,
        *,
        observed_from: datetime,
        observed_to: datetime,
        station_ids: list[str] | None = None,
    ) -> list[StationObservation]:
        """Return stored observations in a half-open ``observed_at`` range.

        Served by the time-series collection's time field plus the
        ``(station_id, observed_at)`` compound index.

        Args:
            observed_from: Inclusive lower bound on ``observed_at`` (UTC).
            observed_to: Exclusive upper bound on ``observed_at`` (UTC).
            station_ids: Filter to specific stations. Defaults to ``None``.

        Returns:
            Matching observations sorted by ``(station_id, observed_at)``.
        """
        query: dict[str, object] = {
            STATION_TIME_FIELD: {"$gte": observed_from, "$lt": observed_to},
        }
        if station_ids is not None:
            query[STATION_METADATA_FIELD] = {"$in": station_ids}
        cursor = self._collection.find(query).sort(
            [(STATION_METADATA_FIELD, ASCENDING), (STATION_TIME_FIELD, ASCENDING)]
        )
        results: list[StationObservation] = []
        async for document in cursor:
            results.append(_document_to_observation(document))
        return results


def build_mongo_station_repository(
    client: AsyncIOMotorClient, database_name: str
) -> MongoStationRepository:
    """Create a :class:`MongoStationRepository` bound to ``database_name``.

    Args:
        client: Motor async MongoDB client.
        database_name: Name of the MongoDB database.

    Returns:
        A configured MongoStationRepository instance.
    """
    database = client[database_name]
    return MongoStationRepository(database)


def _observation_to_document(observation: StationObservation) -> dict[str, object]:
    """Render a Pydantic record as a Mongo-friendly document."""
    return observation.model_dump(mode="python")


def _document_to_observation(document: dict[str, object]) -> StationObservation:
    """Re-hydrate a Mongo document into a :class:`StationObservation`."""
    document.pop("_id", None)
    return StationObservation.model_validate(document)


__all__ = [
    "DryRunStationRepository",
    "MongoStationRepository",
    "STATION_OBSERVATIONS_COLLECTION",
    "STATION_METADATA_FIELD",
    "STATION_TIME_FIELD",
    "StationObservationRepository",
    "build_mongo_station_repository",
]
