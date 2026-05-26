"""Dev-only endpoint to smoke-test the LINE Notify alert channel."""

import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.schemas.common import RiskLevel
from app.services.alert_dispatch import format_alert_message
from app.services.line_notify import send_line_notify

router = APIRouter(prefix="/alerts", tags=["alerts"])


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
