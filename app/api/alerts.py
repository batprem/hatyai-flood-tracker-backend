"""Alert-channel endpoints: LINE smoke-test, Web Push subscriptions, audit log."""

import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import get_app_settings, get_delivery_repository, get_subscription_repository
from app.core.config import Settings
from app.ingestion.delivery_repository import DeliveryRepository
from app.ingestion.subscription_repository import SubscriptionRepository
from app.schemas.alert_delivery import AlertDeliveryListResponse
from app.schemas.common import RiskLevel
from app.schemas.push_subscription import (
    PushSubscription,
    PushSubscriptionRequest,
    PushSubscriptionResponse,
    PushUnsubscribeRequest,
    VapidPublicKeyResponse,
)
from app.services.alert_dispatch import format_alert_message
from app.services.line_messaging import send_line_notify

router = APIRouter(prefix="/alerts", tags=["alerts"])

_DEFAULT_RECENT_LIMIT = 50
_MAX_RECENT_LIMIT = 200


class AlertTestResponse(BaseModel):
    """Model the result of triggering a test LINE Notify push."""

    status: str = Field(description="Always 'sent' when the test message was dispatched.")
    line_status: int = Field(description="HTTP status code returned by the LINE Notify API.")


def _require_test_token(authorization: str | None, settings: Settings) -> None:
    """Authorize a test-alert request using the configured bearer token.

    A request is authorized only when ``ALERTS_TEST_TOKEN`` is configured and
    the ``Authorization: Bearer <token>`` header matches it via a
    constant-time comparison. An unset token rejects every request.

    Args:
        authorization: Raw ``Authorization`` header value, or ``None``.
        settings: Application settings carrying ``alerts_test_token``.

    Raises:
        HTTPException: 403 when the token is unset, missing, or mismatched.
    """
    configured = settings.alerts_test_token
    presented = ""
    if authorization is not None and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :].strip()

    if not configured or not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing alerts test token.",
        )


@router.post("/test", response_model=AlertTestResponse)
async def trigger_test_alert(
    settings: Annotated[Settings, Depends(get_app_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> AlertTestResponse:
    """Send a fake orange-risk LINE Notify message to verify the channel.

    Protected by a static bearer token so it can be safely exposed for Railway
    environment smoke-testing without enabling public alert injection.

    Args:
        settings: Application settings injected via dependency.
        authorization: ``Authorization`` header carrying the bearer token.
            Defaults to ``None``.

    Returns:
        The dispatch result including the LINE Notify HTTP status code.
    """
    _require_test_token(authorization, settings)

    message = format_alert_message(
        level=RiskLevel.ORANGE,
        valid_at=datetime.now(UTC),
        dashboard_url=settings.line_notify_dashboard_url,
    )
    line_status = await send_line_notify(settings.line_notify_token, message)
    return AlertTestResponse(status="sent", line_status=line_status)


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> VapidPublicKeyResponse:
    """Return the public VAPID key the frontend uses to subscribe to push.

    Public and unauthenticated: the key is non-secret by design (it is the
    browser ``applicationServerKey``). An empty value signals that Web Push is
    not configured for this deployment.

    Args:
        settings: Application settings injected via dependency.

    Returns:
        The base64url VAPID public key, empty when Web Push is unconfigured.
    """
    return VapidPublicKeyResponse(vapid_public_key=settings.vapid_public_key)


@router.post(
    "/subscriptions",
    response_model=PushSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    body: PushSubscriptionRequest,
    repository: Annotated[SubscriptionRepository, Depends(get_subscription_repository)],
) -> PushSubscriptionResponse:
    """Register or refresh a browser Web Push subscription.

    Idempotent on ``endpoint``: re-posting the same browser subscription
    refreshes its keys without creating a duplicate. The server assigns
    ``created_at`` so clients cannot spoof it.

    Args:
        body: Native W3C ``PushSubscription.toJSON()`` payload from the browser.
        repository: Subscription store injected via dependency.

    Returns:
        Confirmation carrying the stored endpoint.
    """
    subscription = PushSubscription(
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        created_at=datetime.now(UTC),
    )
    await repository.upsert_subscription(subscription)
    return PushSubscriptionResponse(status="subscribed", endpoint=subscription.endpoint)


@router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    body: PushUnsubscribeRequest,
    repository: Annotated[SubscriptionRepository, Depends(get_subscription_repository)],
) -> Response:
    """Remove a browser Web Push subscription by endpoint.

    Idempotent: deleting an unknown endpoint still returns 204 so a client that
    retries an unsubscribe never sees an error.

    Args:
        body: Request body carrying the endpoint to remove.
        repository: Subscription store injected via dependency.

    Returns:
        An empty ``204 No Content`` response.
    """
    await repository.delete_subscription(body.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/recent", response_model=AlertDeliveryListResponse)
async def list_recent_alerts(
    repository: Annotated[DeliveryRepository, Depends(get_delivery_repository)],
    limit: Annotated[
        int,
        Query(description="Maximum number of delivery records to return. Clamped to 200."),
    ] = _DEFAULT_RECENT_LIMIT,
) -> AlertDeliveryListResponse:
    """Return the most recent alert delivery records, newest first.

    Each record captures channel, risk level, timestamp, outcome, cooldown
    context, and error detail for failed sends. Token values are never
    included. Records are returned regardless of outcome so operators can
    observe both sent and suppressed dispatches.

    Args:
        repository: Delivery audit repository injected via dependency.
        limit: Maximum records to return. Clamped to a safe ceiling. Defaults
            to 50.

    Returns:
        The delivery records with a count.
    """
    safe_limit = max(1, min(limit, _MAX_RECENT_LIMIT))
    deliveries = await repository.recent(limit=safe_limit)
    return AlertDeliveryListResponse(deliveries=deliveries, count=len(deliveries))
