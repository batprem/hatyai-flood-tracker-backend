"""Pydantic models for Web Push subscriptions stored for flood alerts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PushSubscriptionKeys(BaseModel):
    """Model the encryption key pair a browser returns when subscribing."""

    p256dh: str = Field(
        description="Base64url ECDH public key from the browser PushSubscription.",
    )
    auth: str = Field(
        description="Base64url auth secret from the browser PushSubscription.",
    )


class PushSubscription(BaseModel):
    """Model a stored browser Web Push subscription endpoint.

    The shape mirrors the W3C ``PushSubscription.toJSON()`` payload (endpoint
    plus a ``keys`` object) flattened to top-level ``p256dh``/``auth`` for
    compact storage, plus a server-assigned ``created_at`` timestamp. The
    ``endpoint`` is the natural key: it is unique per browser/device and is the
    URL the push service is contacted on.
    """

    endpoint: str = Field(
        description="Push service endpoint URL; unique per browser subscription.",
    )
    p256dh: str = Field(
        description="Base64url ECDH public key from the browser PushSubscription.",
    )
    auth: str = Field(
        description="Base64url auth secret from the browser PushSubscription.",
    )
    created_at: datetime = Field(
        description="UTC timestamp when the subscription was first stored.",
    )

    def to_webpush_info(self) -> dict[str, str | bytes | dict[str, str | bytes]]:
        """Return the subscription in the shape :func:`pywebpush.webpush` expects.

        The return type widens the value union to ``str | bytes`` to match the
        ``pywebpush.webpush`` ``subscription_info`` parameter; only ``str``
        values are produced in practice.

        Returns:
            A mapping with the ``endpoint`` and a nested ``keys`` object
            carrying ``p256dh`` and ``auth``.
        """
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


class PushSubscriptionRequest(BaseModel):
    """Model the request body when a browser registers a push subscription.

    Accepts the native W3C ``PushSubscription.toJSON()`` shape so the frontend
    can post the browser object directly without reshaping. The server assigns
    ``created_at`` so clients cannot backdate or spoof it.
    """

    endpoint: str = Field(
        description="Push service endpoint URL; unique per browser subscription.",
    )
    keys: PushSubscriptionKeys = Field(
        description="Browser-provided p256dh and auth encryption keys.",
    )


class PushUnsubscribeRequest(BaseModel):
    """Model the request body when a browser removes its push subscription."""

    endpoint: str = Field(
        description="Push service endpoint URL of the subscription to remove.",
    )


class PushSubscriptionResponse(BaseModel):
    """Model the response confirming a stored push subscription."""

    status: str = Field(description="Always 'subscribed' when storage succeeds.")
    endpoint: str = Field(description="The endpoint that was stored.")


class VapidPublicKeyResponse(BaseModel):
    """Model the public VAPID key served to browsers for subscribing."""

    vapid_public_key: str = Field(
        description="Base64url VAPID public key used as the browser applicationServerKey.",
    )
