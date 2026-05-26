"""Thin async wrapper around :func:`pywebpush.webpush`.

``pywebpush`` is synchronous (it uses ``requests``), so each send is run in a
worker thread via :func:`asyncio.to_thread` to avoid blocking the event loop.
This layer is intentionally thin: it performs no retries, reads no
configuration, and does not decide pruning policy. Callers own VAPID config
resolution, the iteration over subscriptions, and the 410-Gone pruning rule.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue
from pywebpush import WebPushException, webpush

if TYPE_CHECKING:
    from app.schemas.push_subscription import PushSubscription


@dataclass(frozen=True, slots=True)
class VapidConfig:
    """Carry the VAPID credentials needed to sign a Web Push request.

    Attributes:
        private_key: Base64url DER VAPID private key. Empty disables sending.
        subject: VAPID ``sub`` claim (a ``mailto:`` or ``https:`` URI).
    """

    private_key: str
    subject: str


@dataclass(frozen=True, slots=True)
class WebPushResult:
    """Describe the outcome of a single Web Push send.

    Attributes:
        status_code: HTTP status from the push service, or ``None`` when the
            transport failed before a response was received.
        gone: ``True`` when the push service reported the subscription expired
            (HTTP 404 or 410) and the caller should prune it.
    """

    status_code: int | None
    gone: bool


async def send_web_push(
    subscription: PushSubscription,
    payload: dict[str, JsonValue],
    *,
    vapid_config: VapidConfig,
    timeout_seconds: float = 10.0,
) -> WebPushResult:
    """Send one Web Push message and classify the push service response.

    The synchronous :func:`pywebpush.webpush` call is dispatched to a worker
    thread so the caller's event loop is never blocked. A 404 or 410 response
    is reported as ``gone`` so the caller can prune the dead subscription;
    other failures surface their status code (or ``None`` on a transport error)
    without raising.

    Args:
        subscription: The browser subscription to deliver to.
        payload: JSON-serializable push payload (rendered to the message body).
        vapid_config: VAPID credentials used to sign the request.
        timeout_seconds: Per-request timeout in seconds. Defaults to ``10.0``.

    Returns:
        A :class:`WebPushResult` describing the status code and whether the
        subscription is gone.
    """

    def _send() -> WebPushResult:
        try:
            response = webpush(
                subscription_info=subscription.to_webpush_info(),
                data=json.dumps(payload),
                vapid_private_key=vapid_config.private_key,
                vapid_claims={"sub": vapid_config.subject},
                timeout=timeout_seconds,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            return WebPushResult(status_code=status, gone=status in (404, 410))
        status_code = getattr(response, "status_code", None)
        return WebPushResult(status_code=status_code, gone=False)

    return await asyncio.to_thread(_send)
