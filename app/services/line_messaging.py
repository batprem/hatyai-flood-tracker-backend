"""Thin async client for the LINE Messaging API broadcast endpoint."""

from __future__ import annotations

import httpx

LINE_MESSAGING_API_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


async def send_line_notify(
    token: str,
    message: str,
    *,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Broadcast a plain-text message to all bot followers via the LINE Messaging API.

    The function name is kept as ``send_line_notify`` for call-site compatibility
    while the transport has migrated from the deprecated LINE Notify v1 API to
    the LINE Messaging API broadcast endpoint (HFT-82).

    This layer is intentionally thin: it performs no retries, reads no
    configuration, and does not interpret the response body. Callers own
    token resolution, retry policy, and error handling.

    Args:
        token: LINE Messaging API channel access token used as the bearer credential.
        message: Plain-text message body to broadcast to all bot followers.
        timeout_seconds: Request timeout in seconds. Defaults to ``10.0``.
        client: Optional pre-built async client to reuse. When ``None`` a
            short-lived client is created for this call. Defaults to ``None``.

    Returns:
        The HTTP status code returned by the LINE Messaging API.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {"messages": [{"type": "text", "text": message}]}

    if client is not None:
        response = await client.post(LINE_MESSAGING_API_BROADCAST_URL, headers=headers, json=body)
        return response.status_code

    async with httpx.AsyncClient(timeout=timeout_seconds) as owned_client:
        response = await owned_client.post(
            LINE_MESSAGING_API_BROADCAST_URL, headers=headers, json=body
        )
        return response.status_code
