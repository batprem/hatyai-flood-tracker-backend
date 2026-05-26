"""MongoDB persistence for Web Push subscriptions.

A small repository indirection mirrors the pattern used by
:mod:`app.ingestion.station_repository`. Subscriptions are a bounded,
naturally-keyed set rather than telemetry, so ``push_subscriptions`` is a
*regular* collection (not a time-series one) with a unique index on
``endpoint``. Writes are idempotent upserts keyed on ``endpoint`` so a browser
re-registering the same subscription never creates duplicates.

For Phase 1 the repository is optional: a :class:`DryRunSubscriptionRepository`
keeps subscriptions in memory so the API and dispatch path can be exercised
without a Mongo instance, matching the forecast and station repositories.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from pymongo import ASCENDING

from app.schemas.push_subscription import PushSubscription

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

logger = logging.getLogger(__name__)


PUSH_SUBSCRIPTIONS_COLLECTION = "push_subscriptions"


class SubscriptionRepository(Protocol):
    """Persist and prune Web Push subscriptions."""

    async def ensure_indexes(self) -> None:
        """Create the unique ``endpoint`` index used by Phase 1."""
        ...

    async def upsert_subscription(self, subscription: PushSubscription) -> None:
        """Persist a subscription idempotently keyed on endpoint.

        Args:
            subscription: The browser push subscription to store.
        """
        ...

    async def delete_subscription(self, endpoint: str) -> bool:
        """Delete a subscription by endpoint.

        Args:
            endpoint: Push service endpoint URL to remove.

        Returns:
            ``True`` when a subscription was removed, ``False`` otherwise.
        """
        ...

    async def list_subscriptions(self) -> list[PushSubscription]:
        """Return all stored subscriptions.

        Returns:
            List of every stored push subscription.
        """
        ...


class DryRunSubscriptionRepository:
    """Keep push subscriptions in memory.

    Used by the dry-run backend and unit tests so the subscription API and the
    Web Push dispatch path can be exercised without a Mongo instance. Behavior
    mirrors the persistence semantics of the Mongo-backed repository.
    """

    def __init__(self) -> None:
        self._records: dict[str, PushSubscription] = {}

    async def ensure_indexes(self) -> None:
        """No-op for the in-memory backend."""

    async def upsert_subscription(self, subscription: PushSubscription) -> None:
        """Store the subscription keyed on its endpoint.

        Args:
            subscription: The browser push subscription to store.
        """
        self._records[subscription.endpoint] = subscription

    async def delete_subscription(self, endpoint: str) -> bool:
        """Remove the subscription with the given endpoint from memory.

        Args:
            endpoint: Push service endpoint URL to remove.

        Returns:
            ``True`` when a subscription was removed, ``False`` otherwise.
        """
        return self._records.pop(endpoint, None) is not None

    async def list_subscriptions(self) -> list[PushSubscription]:
        """Return all in-memory subscriptions sorted by endpoint.

        Returns:
            List of every stored push subscription.
        """
        return sorted(self._records.values(), key=lambda sub: sub.endpoint)


class MongoSubscriptionRepository:
    """Persist push subscriptions to a regular MongoDB collection."""

    def __init__(self, database: AsyncIOMotorDatabase) -> None:
        self._database = database
        self._collection: AsyncIOMotorCollection = database[PUSH_SUBSCRIPTIONS_COLLECTION]

    @property
    def collection(self) -> AsyncIOMotorCollection:
        """Return the underlying Motor collection handle."""
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the unique ``endpoint`` index that keys idempotent upserts."""
        await self._collection.create_index(
            [("endpoint", ASCENDING)],
            name="endpoint_unique",
            unique=True,
        )

    async def upsert_subscription(self, subscription: PushSubscription) -> None:
        """Replace the document keyed on endpoint, inserting when absent.

        ``created_at`` is preserved on update via ``$setOnInsert`` so a browser
        re-registering an existing endpoint keeps its original first-seen time.

        Args:
            subscription: The browser push subscription to store.
        """
        document = subscription.model_dump(mode="python")
        created_at = document.pop("created_at")
        await self._collection.update_one(
            {"endpoint": subscription.endpoint},
            {"$set": document, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )

    async def delete_subscription(self, endpoint: str) -> bool:
        """Delete the document with the given endpoint.

        Args:
            endpoint: Push service endpoint URL to remove.

        Returns:
            ``True`` when a document was deleted, ``False`` otherwise.
        """
        result = await self._collection.delete_one({"endpoint": endpoint})
        return result.deleted_count > 0

    async def list_subscriptions(self) -> list[PushSubscription]:
        """Return all stored subscriptions sorted by endpoint.

        Returns:
            List of every stored push subscription.
        """
        cursor = self._collection.find({}).sort([("endpoint", ASCENDING)])
        results: list[PushSubscription] = []
        async for document in cursor:
            document.pop("_id", None)
            results.append(PushSubscription.model_validate(document))
        return results


def build_mongo_subscription_repository(
    client: AsyncIOMotorClient, database_name: str
) -> MongoSubscriptionRepository:
    """Create a :class:`MongoSubscriptionRepository` bound to ``database_name``.

    Args:
        client: Motor async MongoDB client.
        database_name: Name of the MongoDB database.

    Returns:
        A configured MongoSubscriptionRepository instance.
    """
    database = client[database_name]
    return MongoSubscriptionRepository(database)


__all__ = [
    "DryRunSubscriptionRepository",
    "MongoSubscriptionRepository",
    "PUSH_SUBSCRIPTIONS_COLLECTION",
    "SubscriptionRepository",
    "build_mongo_subscription_repository",
]
