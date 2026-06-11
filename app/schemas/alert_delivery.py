"""Pydantic models for alert delivery audit records."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import RiskLevel


class AlertDelivery(BaseModel):
    """Model a single alert send attempt stored in ``alert_deliveries``.

    One document is written per channel per dispatch evaluation, regardless of
    outcome. The ``error_detail`` field is populated only on ``failed`` outcomes
    so operators can diagnose silent delivery failures without inspecting logs.
    """

    channel: str = Field(description="Alert channel identifier, e.g. 'line' or 'web_push'.")
    risk_level: RiskLevel = Field(description="Risk level that triggered the dispatch evaluation.")
    alerted_at: datetime = Field(description="UTC timestamp of the dispatch evaluation.")
    outcome: str = Field(
        description=(
            "Dispatch outcome: 'sent', 'failed', 'skipped_cooldown', or 'skipped_no_token'."
        )
    )
    previous_level: RiskLevel | None = Field(
        default=None,
        description="Last-alerted risk level at the time of evaluation, or null when none.",
    )
    previous_alerted_at: datetime | None = Field(
        default=None,
        description="Timestamp of the last alert for this channel, or null when none.",
    )
    decision_reason: str = Field(
        default="",
        description="Human-readable rationale from the cooldown decision logic.",
    )
    error_detail: str | None = Field(
        default=None,
        description="Exception message on a 'failed' outcome; null otherwise.",
    )


class AlertDeliveryListResponse(BaseModel):
    """Model the ``GET /api/alerts/recent`` response body."""

    deliveries: list[AlertDelivery] = Field(
        description="Delivery records, newest first."
    )
    count: int = Field(description="Number of records returned.")
