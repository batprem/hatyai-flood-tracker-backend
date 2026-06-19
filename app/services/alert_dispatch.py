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

from pydantic import BaseModel, Field, JsonValue

from app.ingestion.delivery_repository import DeliveryOutcome
from app.schemas.alert_delivery import AlertDelivery
from app.schemas.common import RiskLevel
from app.services.line_messaging import send_line_notify
from app.services.web_push import VapidConfig, send_web_push

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

    from app.ingestion.delivery_repository import DeliveryRepository
    from app.ingestion.subscription_repository import SubscriptionRepository
    from app.schemas.push_subscription import PushSubscription

logger = logging.getLogger(__name__)

ALERT_STATE_COLLECTION = "alert_state"
LINE_NOTIFY_SOURCE = "line_notify"
WEB_PUSH_SOURCE = "web_push"

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


async def _log_delivery(
    *,
    delivery_repository: DeliveryRepository | None,
    channel: str,
    risk_level: RiskLevel,
    alerted_at: datetime,
    outcome: DeliveryOutcome,
    state: AlertState | None,
    decision_reason: str,
    error_detail: str | None = None,
) -> None:
    """Write a single delivery audit record when a repository is available.

    Args:
        delivery_repository: Audit repository, or ``None`` to skip logging.
        channel: Alert channel identifier.
        risk_level: Risk level being evaluated.
        alerted_at: UTC timestamp of the dispatch evaluation.
        outcome: Outcome of the send attempt.
        state: Previously persisted alert state used for cooldown context.
            Pass ``None`` when no prior state exists.
        decision_reason: Human-readable rationale from the cooldown logic.
        error_detail: Exception message on a failed outcome. Defaults to
            ``None``.
    """
    if delivery_repository is None:
        return
    delivery = AlertDelivery(
        channel=channel,
        risk_level=risk_level,
        alerted_at=alerted_at,
        outcome=outcome.value,
        previous_level=state.last_risk_level if state is not None else None,
        previous_alerted_at=state.alerted_at if state is not None else None,
        decision_reason=decision_reason,
        error_detail=error_detail,
    )
    try:
        await delivery_repository.append(delivery)
    except Exception:
        logger.warning("Failed to write delivery audit record; continuing", exc_info=True)


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
    delivery_repository: DeliveryRepository | None = None,
) -> AlertDecision:
    """Evaluate the alert rule and push a LINE alert when warranted.

    Reads the persisted alert state, applies :func:`should_send_alert`, and on a
    positive decision sends a LINE Notify push and records the new state. An
    empty ``token`` short-circuits to a logged warning so non-production
    environments never error. The persisted state is only updated after a
    successful send so a transient transport failure is retried on the next run.
    Every evaluation outcome is written to ``delivery_repository`` when provided.

    Args:
        database: Motor database holding the ``alert_state`` collection.
        current_level: Freshly computed basin risk level.
        valid_at: Forecast valid time for the alerting risk, or ``None``.
        token: LINE Notify channel access token. Empty disables sending.
        cooldown_hours: Minimum hours between alerts for an unchanged level.
        dashboard_url: Public dashboard URL appended to the message.
        now: Current UTC timestamp. Defaults to ``None`` (uses ``datetime.now``).
        source: Alert channel identifier. Defaults to ``line_notify``.
        delivery_repository: Audit repository for delivery records. Defaults to
            ``None`` (no logging).

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
        await _log_delivery(
            delivery_repository=delivery_repository,
            channel="line",
            risk_level=current_level,
            alerted_at=evaluated_at,
            outcome=DeliveryOutcome.SKIPPED_COOLDOWN,
            state=state,
            decision_reason=decision.reason,
        )
        return decision

    if not token:
        logger.warning(
            "LINE alert would fire (%s) but LINE_NOTIFY_TOKEN is empty; skipping send",
            decision.reason,
        )
        final_decision = AlertDecision(
            should_send=False,
            reason=f"token unset; would have sent ({decision.reason})",
        )
        await _log_delivery(
            delivery_repository=delivery_repository,
            channel="line",
            risk_level=current_level,
            alerted_at=evaluated_at,
            outcome=DeliveryOutcome.SKIPPED_NO_TOKEN,
            state=state,
            decision_reason=final_decision.reason,
        )
        return final_decision

    message = format_alert_message(
        level=current_level,
        valid_at=valid_at,
        dashboard_url=dashboard_url,
    )
    error_detail: str | None = None
    send_outcome = DeliveryOutcome.SENT
    try:
        http_status = await send_line_notify(token, message)
        logger.info("LINE alert sent: %s (http %s)", decision.reason, http_status)
    except Exception as exc:
        error_detail = str(exc)
        send_outcome = DeliveryOutcome.FAILED
        logger.warning("LINE alert send failed: %s", exc, exc_info=True)
        await _log_delivery(
            delivery_repository=delivery_repository,
            channel="line",
            risk_level=current_level,
            alerted_at=evaluated_at,
            outcome=send_outcome,
            state=state,
            decision_reason=decision.reason,
            error_detail=error_detail,
        )
        return decision

    await write_alert_state(
        database,
        AlertState(source=source, last_risk_level=current_level, alerted_at=evaluated_at),
    )
    await _log_delivery(
        delivery_repository=delivery_repository,
        channel="line",
        risk_level=current_level,
        alerted_at=evaluated_at,
        outcome=send_outcome,
        state=state,
        decision_reason=decision.reason,
    )
    return decision


def build_web_push_payload(
    *,
    level: RiskLevel,
    valid_at: datetime | None,
    dashboard_url: str,
) -> dict[str, JsonValue]:
    """Build the bilingual Web Push payload consumed by the service worker.

    The shape matches ``docs/service-worker-spec.md``: bilingual title/body
    fields, the dashboard ``url`` to open on click, and the ``risk_level`` for
    icon/color selection.

    Args:
        level: Risk level to announce (expected to be orange or red).
        valid_at: Forecast valid time for the alerting risk, or ``None`` when
            unknown.
        dashboard_url: Public dashboard URL opened when the notification is
            tapped.

    Returns:
        A JSON-serializable mapping with bilingual title/body, ``url``, and
        ``risk_level`` keys.
    """
    th_label = _LEVEL_LABEL_TH.get(level, level.value)
    valid_text = (
        valid_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if valid_at is not None else "N/A"
    )
    return {
        "title_en": f"Flood Alert – {level.value.upper()}",
        "title_th": f"แจ้งเตือนน้ำท่วม – {th_label}",
        "body_en": f"Basin risk level raised to {level.value.upper()}. Valid: {valid_text}.",
        "body_th": f"ความเสี่ยงน้ำท่วมระดับ{th_label} ณ {valid_text}",
        "url": dashboard_url,
        "risk_level": level.value,
    }


async def send_web_push_alerts(
    subscriptions: list[PushSubscription],
    payload: dict[str, JsonValue],
    vapid_config: VapidConfig,
    *,
    repository: SubscriptionRepository,
) -> int:
    """Push ``payload`` to every subscription and prune expired endpoints.

    Each subscription is delivered independently: a transport error or a
    non-success status is logged and the loop continues so one dead endpoint
    cannot block the rest. A 404/410 response means the browser dropped the
    subscription, so it is deleted from the repository (HFT-52 pruning). An
    empty ``vapid_config.private_key`` short-circuits with a warning so
    non-production environments never error.

    Args:
        subscriptions: Subscriptions to deliver to.
        payload: JSON-serializable push payload shared across recipients.
        vapid_config: VAPID credentials used to sign each request.
        repository: Subscription store used to prune expired endpoints.

    Returns:
        The number of subscriptions that accepted the push (HTTP 2xx).
    """
    if not vapid_config.private_key:
        logger.warning(
            "Web Push would fire for %d subscriptions but VAPID_PRIVATE_KEY is empty; skipping",
            len(subscriptions),
        )
        return 0

    sent = 0
    for subscription in subscriptions:
        try:
            result = await send_web_push(subscription, payload, vapid_config=vapid_config)
        except Exception:
            logger.warning(
                "Web Push send raised for endpoint %s; continuing",
                subscription.endpoint,
                exc_info=True,
            )
            continue

        if result.gone:
            await repository.delete_subscription(subscription.endpoint)
            logger.info(
                "Web Push pruned expired subscription %s (http %s)",
                subscription.endpoint,
                result.status_code,
            )
            continue

        if result.status_code is not None and 200 <= result.status_code < 300:
            sent += 1
        else:
            logger.warning(
                "Web Push send failed for endpoint %s (http %s); continuing",
                subscription.endpoint,
                result.status_code,
            )

    logger.info("Web Push dispatched: %d of %d subscriptions accepted", sent, len(subscriptions))
    return sent


async def dispatch_web_push_alert(
    *,
    database: AsyncIOMotorDatabase,
    repository: SubscriptionRepository,
    current_level: RiskLevel,
    valid_at: datetime | None,
    vapid_config: VapidConfig,
    cooldown_hours: int,
    dashboard_url: str,
    now: datetime | None = None,
    delivery_repository: DeliveryRepository | None = None,
) -> AlertDecision:
    """Evaluate the alert rule and broadcast a Web Push to all subscribers.

    Uses the same edge-triggered transition and cooldown rules as the LINE
    channel (:func:`should_send_alert`) but against an independent ``web_push``
    alert-state document so the two channels never interfere. An empty
    ``vapid_config.private_key`` short-circuits to a logged warning so
    non-production environments never error. State is only persisted after at
    least one successful send so a transient delivery failure is retried on the
    next run. Every evaluation outcome is written to ``delivery_repository`` when
    provided.

    Args:
        database: Motor database holding the ``alert_state`` collection.
        repository: Subscription store used to read recipients and prune dead
            endpoints.
        current_level: Freshly computed basin risk level.
        valid_at: Forecast valid time for the alerting risk, or ``None``.
        vapid_config: VAPID credentials used to sign each push.
        cooldown_hours: Minimum hours between alerts for an unchanged level.
        dashboard_url: Public dashboard URL opened from the notification.
        now: Current UTC timestamp. Defaults to ``None`` (uses ``datetime.now``).
        delivery_repository: Audit repository for delivery records. Defaults to
            ``None`` (no logging).

    Returns:
        The :class:`AlertDecision` describing whether a broadcast was sent.
    """
    evaluated_at = now or datetime.now(UTC)
    state = await read_alert_state(database, source=WEB_PUSH_SOURCE)
    decision = should_send_alert(
        current_level=current_level,
        state=state,
        now=evaluated_at,
        cooldown_hours=cooldown_hours,
    )

    if not decision.should_send:
        logger.info("Web Push alert suppressed: %s", decision.reason)
        await _log_delivery(
            delivery_repository=delivery_repository,
            channel="web_push",
            risk_level=current_level,
            alerted_at=evaluated_at,
            outcome=DeliveryOutcome.SKIPPED_COOLDOWN,
            state=state,
            decision_reason=decision.reason,
        )
        return decision

    if not vapid_config.private_key:
        logger.warning(
            "Web Push alert would fire (%s) but VAPID_PRIVATE_KEY is empty; skipping send",
            decision.reason,
        )
        final_decision = AlertDecision(
            should_send=False,
            reason=f"vapid key unset; would have sent ({decision.reason})",
        )
        await _log_delivery(
            delivery_repository=delivery_repository,
            channel="web_push",
            risk_level=current_level,
            alerted_at=evaluated_at,
            outcome=DeliveryOutcome.SKIPPED_NO_TOKEN,
            state=state,
            decision_reason=final_decision.reason,
        )
        return final_decision

    subscriptions = await repository.list_subscriptions()
    payload = build_web_push_payload(
        level=current_level,
        valid_at=valid_at,
        dashboard_url=dashboard_url,
    )
    sent = await send_web_push_alerts(
        subscriptions,
        payload,
        vapid_config,
        repository=repository,
    )
    logger.info("Web Push alert sent: %s (%d delivered)", decision.reason, sent)

    await write_alert_state(
        database,
        AlertState(source=WEB_PUSH_SOURCE, last_risk_level=current_level, alerted_at=evaluated_at),
    )
    await _log_delivery(
        delivery_repository=delivery_repository,
        channel="web_push",
        risk_level=current_level,
        alerted_at=evaluated_at,
        outcome=DeliveryOutcome.SENT,
        state=state,
        decision_reason=decision.reason,
    )
    return decision
