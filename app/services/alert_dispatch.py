"""Edge-triggered LINE flood-alert dispatch with cooldown deduplication.

The dispatcher fires a LINE Notify push only when basin risk transitions
*upward* into ``orange`` or ``red``. The last alerted level and timestamp are
persisted in a small ``alert_state`` collection so transitions survive across
scheduler runs and process restarts. A configurable cooldown suppresses
repeat alerts for the same level, while a further upward transition (for
example ``orange`` to ``red``) bypasses the cooldown.

This module keeps the *decision* (:func:`should_send_alert`) pure and free of
IO so the transition and cooldown rules can be unit-tested independently of
MongoDB and the LINE transport, per the project's risk-logic separation rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.schemas.common import RiskLevel
from app.services.line_notify import send_line_notify

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

ALERT_STATE_COLLECTION = "alert_state"
LINE_NOTIFY_SOURCE = "line_notify"

#: Levels that warrant a public push. Green and yellow never alert.
ALERTING_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.ORANGE, RiskLevel.RED})

_LEVEL_RANK: dict[RiskLevel, int] = {
    RiskLevel.GREEN: 0,
    RiskLevel.YELLOW: 1,
    RiskLevel.ORANGE: 2,
    RiskLevel.RED: 3,
}

_LEVEL_LABEL_TH: dict[RiskLevel, str] = {
    RiskLevel.ORANGE: "เฝ้าระวัง",
    RiskLevel.RED: "อันตราย",
}


class AlertState(BaseModel):
    """Model the last-alerted state for one alert channel.

    One document exists per ``source`` (currently ``line_notify``). It records
    the most recently alerted risk level and the time the alert was sent so the
    dispatcher can apply edge-triggered transition and cooldown rules.
    """

    source: str = Field(description="Alert channel identifier, e.g. 'line_notify'.")
    last_risk_level: RiskLevel = Field(description="Risk level of the most recent alert sent.")
    alerted_at: datetime = Field(description="UTC timestamp when the last alert was sent.")


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """Describe whether an alert should fire and why.

    Attributes are inspectable so the scheduler can log the rationale for both
    fired and suppressed alerts without re-deriving the rule.
    """

    should_send: bool
    reason: str


def level_rank(level: RiskLevel) -> int:
    """Return the ordinal severity rank of a risk level.

    Args:
        level: Public risk level.

    Returns:
        Integer rank where green is 0 and red is 3.
    """
    return _LEVEL_RANK[level]


def should_send_alert(
    *,
    current_level: RiskLevel,
    state: AlertState | None,
    now: datetime,
    cooldown_hours: int,
) -> AlertDecision:
    """Decide whether to push a LINE alert for the current basin risk.

    An alert fires only when ``current_level`` is ``orange`` or ``red`` and the
    move is an *upward* transition relative to the last alerted level. When the
    level is unchanged, the cooldown window must have elapsed; a further upward
    transition always fires regardless of cooldown.

    Args:
        current_level: Freshly computed basin risk level.
        state: Previously persisted alert state, or ``None`` when nothing has
            been alerted yet.
        now: Current UTC timestamp used for cooldown evaluation.
        cooldown_hours: Minimum hours between alerts for an unchanged level.

    Returns:
        An :class:`AlertDecision` capturing the send flag and the rationale.
    """
    if current_level not in ALERTING_LEVELS:
        return AlertDecision(
            should_send=False,
            reason=f"level {current_level.value} is below the orange alert threshold",
        )

    if state is None:
        return AlertDecision(
            should_send=True,
            reason=f"first alert at level {current_level.value}",
        )

    current_rank = level_rank(current_level)
    previous_rank = level_rank(state.last_risk_level)

    if current_rank < previous_rank:
        return AlertDecision(
            should_send=False,
            reason=(
                f"downward transition {state.last_risk_level.value} to "
                f"{current_level.value} does not alert"
            ),
        )

    if current_rank > previous_rank:
        return AlertDecision(
            should_send=True,
            reason=(
                f"upward transition {state.last_risk_level.value} to "
                f"{current_level.value} bypasses cooldown"
            ),
        )

    elapsed = now - state.alerted_at
    if elapsed < timedelta(hours=cooldown_hours):
        return AlertDecision(
            should_send=False,
            reason=(
                f"level {current_level.value} unchanged within "
                f"{cooldown_hours}h cooldown ({elapsed} elapsed)"
            ),
        )

    return AlertDecision(
        should_send=True,
        reason=(f"level {current_level.value} unchanged but cooldown of {cooldown_hours}h elapsed"),
    )


def format_alert_message(
    *,
    level: RiskLevel,
    valid_at: datetime | None,
    dashboard_url: str,
) -> str:
    """Render the bilingual Thai/English LINE alert message body.

    Args:
        level: Risk level to announce (expected to be orange or red).
        valid_at: Forecast valid time for the alerting risk, or ``None`` when
            unknown.
        dashboard_url: Public dashboard URL appended as the call to action.

    Returns:
        A multi-line plain-text message suitable for LINE Notify.
    """
    th_label = _LEVEL_LABEL_TH.get(level, level.value)
    valid_text = (
        valid_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if valid_at is not None else "N/A"
    )
    return (
        "⚠️ Hat Yai Flood Alert / แจ้งเตือนน้ำท่วมหาดใหญ่\n"
        f"Risk level: {level.value.upper()} / ระดับ: {th_label}\n"
        f"Valid: {valid_text}\n"
        f"{dashboard_url}"
    )


async def read_alert_state(
    database: AsyncIOMotorDatabase,
    *,
    source: str = LINE_NOTIFY_SOURCE,
) -> AlertState | None:
    """Return the persisted alert state for a channel, or ``None`` when unset.

    Args:
        database: Motor database holding the ``alert_state`` collection.
        source: Alert channel identifier. Defaults to ``line_notify``.

    Returns:
        The stored :class:`AlertState`, or ``None`` when no document exists.
    """
    document = await database[ALERT_STATE_COLLECTION].find_one({"source": source})
    if document is None:
        return None
    document.pop("_id", None)
    alerted_at = document.get("alerted_at")
    if isinstance(alerted_at, datetime) and alerted_at.tzinfo is None:
        # BSON dates round-trip as UTC-naive (and mongomock-motor keeps them
        # naive); coerce back to UTC so cooldown comparisons stay tz-aware.
        document["alerted_at"] = alerted_at.replace(tzinfo=UTC)
    return AlertState.model_validate(document)


async def write_alert_state(
    database: AsyncIOMotorDatabase,
    state: AlertState,
) -> None:
    """Upsert the alert state for a channel keyed by ``source``.

    Args:
        database: Motor database holding the ``alert_state`` collection.
        state: Alert state to persist.
    """
    await database[ALERT_STATE_COLLECTION].update_one(
        {"source": state.source},
        {"$set": state.model_dump(mode="python")},
        upsert=True,
    )


async def dispatch_risk_alert(
    *,
    database: AsyncIOMotorDatabase,
    current_level: RiskLevel,
    valid_at: datetime | None,
    token: str,
    cooldown_hours: int,
    dashboard_url: str,
    now: datetime | None = None,
    source: str = LINE_NOTIFY_SOURCE,
) -> AlertDecision:
    """Evaluate the alert rule and push a LINE alert when warranted.

    Reads the persisted alert state, applies :func:`should_send_alert`, and on a
    positive decision sends a LINE Notify push and records the new state. An
    empty ``token`` short-circuits to a logged warning so non-production
    environments never error. The persisted state is only updated after a
    successful send so a transient transport failure is retried on the next run.

    Args:
        database: Motor database holding the ``alert_state`` collection.
        current_level: Freshly computed basin risk level.
        valid_at: Forecast valid time for the alerting risk, or ``None``.
        token: LINE Notify channel access token. Empty disables sending.
        cooldown_hours: Minimum hours between alerts for an unchanged level.
        dashboard_url: Public dashboard URL appended to the message.
        now: Current UTC timestamp. Defaults to ``None`` (uses ``datetime.now``).
        source: Alert channel identifier. Defaults to ``line_notify``.

    Returns:
        The :class:`AlertDecision` describing whether an alert was sent.
    """
    evaluated_at = now or datetime.now(UTC)
    state = await read_alert_state(database, source=source)
    decision = should_send_alert(
        current_level=current_level,
        state=state,
        now=evaluated_at,
        cooldown_hours=cooldown_hours,
    )

    if not decision.should_send:
        logger.info("LINE alert suppressed: %s", decision.reason)
        return decision

    if not token:
        logger.warning(
            "LINE alert would fire (%s) but LINE_NOTIFY_TOKEN is empty; skipping send",
            decision.reason,
        )
        return AlertDecision(
            should_send=False,
            reason=f"token unset; would have sent ({decision.reason})",
        )

    message = format_alert_message(
        level=current_level,
        valid_at=valid_at,
        dashboard_url=dashboard_url,
    )
    status = await send_line_notify(token, message)
    logger.info("LINE alert sent: %s (http %s)", decision.reason, status)

    await write_alert_state(
        database,
        AlertState(source=source, last_risk_level=current_level, alerted_at=evaluated_at),
    )
    return decision
