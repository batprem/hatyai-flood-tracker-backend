"""Pluggable photo storage behind a small async interface (HFT-73).

Phase 4 stores citizen-report photos in MongoDB GridFS. The choice is
deliberate and recorded on HFT-73: Railway disk is ephemeral, a GCS bucket
needs credentials and billing the project has not provisioned, and GridFS
reuses the existing Mongo deployment with zero new secrets at prototype scale.

The :class:`PhotoStorage` protocol isolates the report logic from the backend
so a GCS signed-URL implementation can swap in later without touching the
routers or repository. A backend only has to satisfy ``save`` / ``open`` /
``delete``; callers never reference GridFS directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


class PhotoNotFoundError(Exception):
    """Raise when a requested photo is absent from the storage backend."""


class StoredPhoto:
    """Hold a photo's bytes and content type read back from storage."""

    def __init__(self, data: bytes, content_type: str) -> None:
        self.data = data
        self.content_type = content_type


class PhotoStorage(Protocol):
    """Persist, read, and delete report photos behind a backend-agnostic API."""

    async def save(self, *, data: bytes, content_type: str) -> str:
        """Persist photo bytes and return an opaque storage key.

        Args:
            data: Sanitized image bytes (EXIF already stripped by the caller).
            content_type: MIME type to record alongside the bytes.

        Returns:
            An opaque storage key the report document references.
        """
        ...

    async def open(self, key: str) -> StoredPhoto:
        """Read a stored photo by key, raising when the key is absent.

        Implementations raise :class:`PhotoNotFoundError` when no photo exists
        for the key.

        Args:
            key: Opaque storage key returned by :meth:`save`.

        Returns:
            The stored photo bytes and content type.
        """
        ...

    async def delete(self, key: str) -> None:
        """Delete a stored photo by key, ignoring an already-absent key.

        Args:
            key: Opaque storage key returned by :meth:`save`.
        """
        ...


class GridFSPhotoStorage:
    """Store report photos in a MongoDB GridFS bucket.

    Keys are the GridFS file ``_id`` rendered as a hex string so they round-trip
    cleanly through JSON and URLs. The content type is stored in the GridFS file
    metadata and returned on read so the photo endpoint can set the right
    ``Content-Type`` header.
    """

    def __init__(
        self, database: AsyncIOMotorDatabase, *, bucket_name: str = "report_photos"
    ) -> None:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket

        self._bucket = AsyncIOMotorGridFSBucket(database, bucket_name=bucket_name)

    async def save(self, *, data: bytes, content_type: str) -> str:
        """Upload bytes to GridFS and return the file id hex string.

        Args:
            data: Sanitized image bytes (EXIF already stripped by the caller).
            content_type: MIME type recorded in GridFS metadata.

        Returns:
            The GridFS file ``_id`` as a hex string.
        """
        file_id = await self._bucket.upload_from_stream(
            "report_photo",
            data,
            metadata={"content_type": content_type},
        )
        return str(file_id)

    async def open(self, key: str) -> StoredPhoto:
        """Download a GridFS file by id and return its bytes and content type.

        Args:
            key: GridFS file id hex string returned by :meth:`save`.

        Returns:
            The stored photo bytes and content type.

        Raises:
            PhotoNotFoundError: When the key is malformed or no file exists.
        """
        from bson import ObjectId
        from bson.errors import InvalidId
        from gridfs.errors import NoFile

        try:
            object_id = ObjectId(key)
        except (InvalidId, TypeError) as exc:
            raise PhotoNotFoundError(key) from exc

        try:
            stream = await self._bucket.open_download_stream(object_id)
        except NoFile as exc:
            raise PhotoNotFoundError(key) from exc

        data = await stream.read()
        metadata = stream.metadata or {}
        content_type = metadata.get("content_type", "application/octet-stream")
        return StoredPhoto(data=data, content_type=content_type)

    async def delete(self, key: str) -> None:
        """Delete a GridFS file by id, ignoring a malformed or absent key.

        Args:
            key: GridFS file id hex string returned by :meth:`save`.
        """
        from bson import ObjectId
        from bson.errors import InvalidId
        from gridfs.errors import NoFile

        try:
            object_id = ObjectId(key)
        except (InvalidId, TypeError):
            return
        try:
            await self._bucket.delete(object_id)
        except NoFile:
            return


class InMemoryPhotoStorage:
    """Keep report photos in a process-local dict for dry-run and tests.

    Mirrors the GridFS semantics without a Mongo dependency so the submission
    and photo-serving paths can be exercised in unit tests.
    """

    def __init__(self) -> None:
        self._store: dict[str, StoredPhoto] = {}
        self._counter = 0

    async def save(self, *, data: bytes, content_type: str) -> str:
        """Store bytes under a monotonically increasing key.

        Args:
            data: Sanitized image bytes (EXIF already stripped by the caller).
            content_type: MIME type to record alongside the bytes.

        Returns:
            The generated storage key.
        """
        self._counter += 1
        key = f"mem-{self._counter:024d}"
        self._store[key] = StoredPhoto(data=data, content_type=content_type)
        return key

    async def open(self, key: str) -> StoredPhoto:
        """Return the stored photo for a key.

        Args:
            key: Storage key returned by :meth:`save`.

        Returns:
            The stored photo bytes and content type.

        Raises:
            PhotoNotFoundError: When no photo exists for the key.
        """
        photo = self._store.get(key)
        if photo is None:
            raise PhotoNotFoundError(key)
        return photo

    async def delete(self, key: str) -> None:
        """Remove a stored photo, ignoring an absent key.

        Args:
            key: Storage key returned by :meth:`save`.
        """
        self._store.pop(key, None)


def build_gridfs_photo_storage(
    client: AsyncIOMotorClient, database_name: str
) -> GridFSPhotoStorage:
    """Create a :class:`GridFSPhotoStorage` bound to ``database_name``.

    Args:
        client: Motor async MongoDB client.
        database_name: Name of the MongoDB database hosting the bucket.

    Returns:
        A configured GridFS-backed photo storage instance.
    """
    return GridFSPhotoStorage(client[database_name])


__all__ = [
    "GridFSPhotoStorage",
    "InMemoryPhotoStorage",
    "PhotoNotFoundError",
    "PhotoStorage",
    "StoredPhoto",
    "build_gridfs_photo_storage",
]
