"""Validate and sanitize citizen-report photos (HFT-73).

Privacy is a hard requirement: every uploaded photo is fully re-encoded with
Pillow before storage so all EXIF metadata — crucially any embedded GPS tags —
is discarded. We never copy the original bytes through to storage. The
sanitizer also enforces the accepted formats (JPEG / PNG) and a size cap.

Pillow decoding/encoding is CPU-bound and blocking, so the public entry point
runs it on a worker thread via ``anyio.to_thread`` to keep the event loop
responsive.
"""

from __future__ import annotations

import asyncio
import io

from PIL import Image, UnidentifiedImageError

# Output is always re-encoded JPEG: a single sanitized format keeps the
# storage/serving path simple and guarantees no metadata survives.
SANITIZED_CONTENT_TYPE = "image/jpeg"
ACCEPTED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # ~5 MB cap on the *uploaded* bytes.
_ACCEPTED_PILLOW_FORMATS = frozenset({"JPEG", "PNG"})
_JPEG_QUALITY = 85


class PhotoValidationError(Exception):
    """Raise when an uploaded photo is too large, empty, or not a valid JPEG/PNG."""


def _strip_and_reencode(data: bytes) -> bytes:
    """Decode, drop all metadata, and re-encode the image as clean JPEG.

    Re-encoding through a fresh ``Image`` object is the standard way to discard
    EXIF/GPS metadata: only pixel data is carried over, never the original
    metadata blocks. Palette and transparency modes are flattened to RGB so the
    JPEG encoder always succeeds.

    Args:
        data: Raw uploaded image bytes.

    Returns:
        Sanitized JPEG bytes with no EXIF/GPS metadata.

    Raises:
        PhotoValidationError: When the bytes are not a decodable JPEG/PNG image.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            is_accepted = image_format in _ACCEPTED_PILLOW_FORMATS
            # Copy raw pixels into a brand-new image so no metadata, ICC
            # profile, or EXIF block is carried into the output. Going through
            # ``tobytes``/``frombytes`` guarantees only pixel data survives.
            converted = image.convert("RGB")
            pixels = converted.tobytes()
            size = converted.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        msg = "Uploaded file is not a valid JPEG or PNG image."
        raise PhotoValidationError(msg) from exc

    if not is_accepted:
        msg = "Unsupported image format; only JPEG and PNG are accepted."
        raise PhotoValidationError(msg)

    clean = Image.frombytes("RGB", size, pixels)
    buffer = io.BytesIO()
    clean.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()


async def sanitize_photo(data: bytes, *, content_type: str | None) -> bytes:
    """Validate and strip metadata from an uploaded report photo.

    Enforces the size cap and declared content type, then re-encodes the image
    on a worker thread (via :func:`asyncio.to_thread`) so all EXIF (including
    GPS) is removed before storage without blocking the event loop.

    Args:
        data: Raw uploaded image bytes.
        content_type: Declared MIME type from the upload, or ``None``.

    Returns:
        Sanitized JPEG bytes safe to persist.

    Raises:
        PhotoValidationError: When the photo is empty, oversized, of an
            unsupported declared type, or not a decodable JPEG/PNG.
    """
    if not data:
        msg = "Uploaded photo is empty."
        raise PhotoValidationError(msg)
    if len(data) > MAX_PHOTO_BYTES:
        msg = "Uploaded photo exceeds the 5 MB size limit."
        raise PhotoValidationError(msg)
    if content_type is not None and content_type not in ACCEPTED_CONTENT_TYPES:
        msg = "Unsupported photo content type; only JPEG and PNG are accepted."
        raise PhotoValidationError(msg)
    return await asyncio.to_thread(_strip_and_reencode, data)


__all__ = [
    "ACCEPTED_CONTENT_TYPES",
    "MAX_PHOTO_BYTES",
    "SANITIZED_CONTENT_TYPE",
    "PhotoValidationError",
    "sanitize_photo",
]
