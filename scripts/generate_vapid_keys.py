"""One-shot helper that prints a fresh VAPID key pair for Web Push.

Run with::

    uv run python scripts/generate_vapid_keys.py

The script emits the two environment variables consumed by
:class:`app.core.config.Settings` — ``VAPID_PUBLIC_KEY`` (the base64url raw
public key the browser uses as ``applicationServerKey``) and
``VAPID_PRIVATE_KEY`` (the base64url DER private key :func:`pywebpush.webpush`
signs requests with). Paste them into the Cloud Run / Railway environment.

Keys are never written to disk: they are printed once so an operator can copy
them into secret storage. Regenerating keys invalidates every existing browser
subscription, so do it deliberately.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02, b64urlencode


def generate_vapid_keypair() -> tuple[str, str]:
    """Generate a P-256 VAPID key pair encoded for Web Push and browsers.

    Returns:
        A ``(public_key, private_key)`` tuple where ``public_key`` is the
        base64url raw uncompressed point used as the browser
        ``applicationServerKey`` and ``private_key`` is the base64url DER
        encoding accepted by :func:`pywebpush.webpush`.
    """
    vapid = Vapid02()
    vapid.generate_keys()

    raw_public = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    der_private = vapid.private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return b64urlencode(raw_public), b64urlencode(der_private)


def main() -> None:
    """Print a fresh VAPID key pair as copy-pasteable environment variables."""
    public_key, private_key = generate_vapid_keypair()
    print("# Web Push VAPID keys — store as secrets, never commit.")
    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY={private_key}")


if __name__ == "__main__":
    main()
