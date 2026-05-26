"""Thin async client for the LINE Notify v1 push API."""

from __future__ import annotations

import httpx

LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


async def send_line_notify(
    token: str,
    message: str,
    *,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Post a message to the LINE Notify v1 API and return the HTTP status.

    This layer is intentionally thin: it performs no retries, reads no
    configuration, and does not interpret the response body. Callers own
    token resolution, retry policy, and error handling.

    Args:
        token: LINE Notify channel access token used as the bearer credential.
        message: Plain-text message body to push to subscribers.
        timeout_seconds: Request timeout in seconds. Defaults to ``10.0``.
        client: Optional pre-built async client to reuse. When ``None`` a
            short-lived client is created for this call. Defaults to ``None``.

    Returns:
        The HTTP status code returned by the LINE Notify API.
    """
    headers = {"Authorization": f"Bearer {token}"}
    data = {"message": message}

    if client is not None:
        response = await client.post(LINE_NOTIFY_URL, headers=headers, data=data)
        return response.status_code

    async with httpx.AsyncClient(timeout=timeout_seconds) as owned_client:
        response = await owned_client.post(LINE_NOTIFY_URL, headers=headers, data=data)
        return response.status_code
