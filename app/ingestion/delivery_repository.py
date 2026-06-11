"""Persistence for alert delivery audit records.

Every public alert send attempt — whether it succeeds, fails, or is suppressed
by a cooldown — is written to the ``alert_deliveries`` collection so operators
can observe delivery outcomes and debug silent failures.

The repository mirrors the pattern used by
:mod:`app.ingestion.subscription_repository`: a :class:`Protocol` defines the
interface, a :class:`DryRunDeliveryRepository` keeps records in memory for
tests, and :class:`MongoDeliveryRepository` writes to MongoDB. Reads are
newest-first and capped by a caller-supplied limit.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pymongo import DESCENDING

from app.schemas.alert_delivery import AlertDelivery

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

logger = logging.getLogger(__name__)

ALERT_DELIVERIES_COLLECTION = "alert_deliveries"

#: Hard cap applied server-side so callers cannot request arbitrarily large scans.
MAX_RECENT_LIMIT = 500


class DeliveryOutcome(StrEnum):
    """Enumerate the possible outcomes of a single alert send attempt."""

    SENT = "sent"
    FAILED = "failed"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    SKIPPED_NO_TOKEN = "skipped_no_token"


class DeliveryRepository(Protocol):
    """Persist and query alert delivery audit records."""

    async def append(self, delivery: AlertDelivery) -> None:
        """Write a single delivery record.

        Args:
            delivery: The audit record to persist.
        """
        ...

    async def recent(self, *, limit: int) -> list[AlertDelivery]:
        """Return the most recent delivery records, newest first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            Delivery records ordered from newest to oldest.
        """
        ...


class DryRunDeliveryRepository:
    """Keep delivery records in memory for tests and the dry-run backend.

    Behavior mirrors the persistence semantics of the Mongo-backed repository:
    ``append`` always inserts (records are append-only), and ``recent`` returns
    records newest-first by ``alerted_at``.
    """

    def __init__(self) -> None:
        self._records: list[AlertDelivery] = []

    async def append(self, delivery: AlertDelivery) -> None:
        """Append a delivery record to the in-memory list.

        Args:
            delivery: The audit record to store.
        """
        self._records.append(delivery)

    async def recent(self, *, limit: int) -> list[AlertDelivery]:
        """Return up to ``limit`` records sorted newest first by ``alerted_at``.

        Args:
            limit: Maximum number of records to return.

        Returns:
            Delivery records ordered from newest to oldest.
        """
        sorted_records = sorted(self._records, key=lambda r: r.alerted_at, reverse=True)
        return sorted_records[:limit]


class MongoDeliveryRepository:
    """Persist alert delivery audit records to a regular MongoDB collection."""

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._collection: AsyncIOMotorCollection = database[ALERT_DELIVERIES_COLLECTION]

    @property
    def collection(self) -> AsyncIOMotorCollection:
        """Return the underlying Motor collection handle."""
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the ``alerted_at`` descending index used for recent-first queries."""
        await self._collection.create_index(
            [("alerted_at", DESCENDING)],
            name="alerted_at_desc",
        )

    async def append(self, delivery: AlertDelivery) -> None:
        """Insert a delivery record document.

        Args:
            delivery: The audit record to persist.
        """
        await self._collection.insert_one(delivery.model_dump(mode="python"))

    async def recent(self, *, limit: int) -> list[AlertDelivery]:
        """Return up to ``limit`` delivery records, newest first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            Delivery records ordered from newest to oldest.
        """
        safe_limit = max(1, min(limit, MAX_RECENT_LIMIT))
        cursor = self._collection.find({}).sort([("alerted_at", DESCENDING)]).limit(safe_limit)
        results: list[AlertDelivery] = []
        async for document in cursor:
            document.pop("_id", None)
            results.append(AlertDelivery.model_validate(document))
        return results


def build_mongo_delivery_repository(
    client: AsyncIOMotorClient, database_name: str
) -> MongoDeliveryRepository:
    """Create a :class:`MongoDeliveryRepository` bound to ``database_name``.

    Args:
        client: Motor async MongoDB client.
        database_name: Name of the MongoDB database.

    Returns:
        A configured MongoDeliveryRepository instance.
    """
    database = client[database_name]
    return MongoDeliveryRepository(database)


__all__ = [
    "ALERT_DELIVERIES_COLLECTION",
    "DeliveryOutcome",
    "DeliveryRepository",
    "DryRunDeliveryRepository",
    "MongoDeliveryRepository",
    "build_mongo_delivery_repository",
]
