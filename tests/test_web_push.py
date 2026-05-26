"""Tests for the Web Push subscription API and alert dispatch path (HFT-32).

``pywebpush.webpush`` is mocked throughout so tests run offline. Coverage
mirrors HFT-52: subscription create/delete storage, the public VAPID-key
endpoint, broadcast to every subscription, and 410-Gone pruning of dead
endpoints.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.ingestion.subscription_repository import DryRunSubscriptionRepository
from app.main import create_app
from app.schemas.push_subscription import PushSubscription
from app.services.alert_dispatch import build_web_push_payload, send_web_push_alerts
from app.services.web_push import VapidConfig

DASHBOARD_URL = "https://hatyai-flood-warning.vercel.app"
VAPID_PRIVATE = "test-private-key"
VAPID_PUBLIC = "test-public-key"


def _subscription(endpoint: str) -> PushSubscription:
    return PushSubscription(
        endpoint=endpoint,
        p256dh="p256dh-key",
        auth="auth-secret",
        created_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
    )


def _vapid() -> VapidConfig:
    return VapidConfig(private_key=VAPID_PRIVATE, subject="mailto:admin@example.com")


class SubscriptionEndpointTests(unittest.TestCase):
    def _client(self, repository: DryRunSubscriptionRepository) -> TestClient:
        settings = Settings(VAPID_PUBLIC_KEY=VAPID_PUBLIC, VAPID_PRIVATE_KEY=VAPID_PRIVATE)
        app = create_app(settings=settings, subscription_repository=repository)
        return TestClient(app)

    def test_post_subscription_stores_record(self) -> None:
        repository = DryRunSubscriptionRepository()
        with self._client(repository) as http:
            response = http.post(
                "/api/alerts/subscriptions",
                json={
                    "endpoint": "https://push.example.com/v1/abc",
                    "keys": {"p256dh": "p256dh-key", "auth": "auth-secret"},
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "subscribed")
        self.assertEqual(payload["endpoint"], "https://push.example.com/v1/abc")

        import asyncio

        stored = asyncio.run(repository.list_subscriptions())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].endpoint, "https://push.example.com/v1/abc")
        self.assertEqual(stored[0].p256dh, "p256dh-key")
        self.assertEqual(stored[0].auth, "auth-secret")

    def test_post_subscription_is_idempotent(self) -> None:
        repository = DryRunSubscriptionRepository()
        body = {
            "endpoint": "https://push.example.com/v1/abc",
            "keys": {"p256dh": "p256dh-key", "auth": "auth-secret"},
        }
        with self._client(repository) as http:
            http.post("/api/alerts/subscriptions", json=body)
            http.post("/api/alerts/subscriptions", json=body)

        import asyncio

        stored = asyncio.run(repository.list_subscriptions())
        self.assertEqual(len(stored), 1)

    def test_delete_subscription_removes_record(self) -> None:
        repository = DryRunSubscriptionRepository()
        with self._client(repository) as http:
            http.post(
                "/api/alerts/subscriptions",
                json={
                    "endpoint": "https://push.example.com/v1/abc",
                    "keys": {"p256dh": "p256dh-key", "auth": "auth-secret"},
                },
            )
            response = http.request(
                "DELETE",
                "/api/alerts/subscriptions",
                json={"endpoint": "https://push.example.com/v1/abc"},
            )

        self.assertEqual(response.status_code, 204)

        import asyncio

        stored = asyncio.run(repository.list_subscriptions())
        self.assertEqual(stored, [])

    def test_delete_unknown_endpoint_is_idempotent(self) -> None:
        repository = DryRunSubscriptionRepository()
        with self._client(repository) as http:
            response = http.request(
                "DELETE",
                "/api/alerts/subscriptions",
                json={"endpoint": "https://push.example.com/v1/missing"},
            )
        self.assertEqual(response.status_code, 204)

    def test_get_vapid_public_key(self) -> None:
        repository = DryRunSubscriptionRepository()
        with self._client(repository) as http:
            response = http.get("/api/alerts/vapid-public-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"vapid_public_key": VAPID_PUBLIC})


class SendWebPushAlertsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_sends_to_all_subscriptions(self) -> None:
        repository = DryRunSubscriptionRepository()
        subscriptions = [
            _subscription("https://push.example.com/v1/a"),
            _subscription("https://push.example.com/v1/b"),
        ]
        for sub in subscriptions:
            await repository.upsert_subscription(sub)

        from app.schemas.common import RiskLevel

        payload = build_web_push_payload(
            level=RiskLevel.ORANGE,
            valid_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
            dashboard_url=DASHBOARD_URL,
        )

        ok_response = MagicMock(status_code=201)
        with patch(
            "app.services.web_push.webpush", return_value=ok_response
        ) as mock_webpush:
            sent = await send_web_push_alerts(
                subscriptions,
                payload,
                _vapid(),
                repository=repository,
            )

        self.assertEqual(sent, 2)
        self.assertEqual(mock_webpush.call_count, 2)
        # Both subscriptions survive a successful send.
        self.assertEqual(len(await repository.list_subscriptions()), 2)

    async def test_410_gone_prunes_subscription(self) -> None:
        from pywebpush import WebPushException

        repository = DryRunSubscriptionRepository()
        live = _subscription("https://push.example.com/v1/live")
        dead = _subscription("https://push.example.com/v1/dead")
        await repository.upsert_subscription(live)
        await repository.upsert_subscription(dead)

        gone_response = MagicMock(status_code=410)
        ok_response = MagicMock(status_code=201)

        def fake_webpush(*, subscription_info: dict[str, object], **_: object) -> MagicMock:
            if subscription_info["endpoint"] == dead.endpoint:
                raise WebPushException("gone", response=gone_response)
            return ok_response

        with patch("app.services.web_push.webpush", side_effect=fake_webpush):
            sent = await send_web_push_alerts(
                [live, dead],
                {"title_en": "x"},
                _vapid(),
                repository=repository,
            )

        self.assertEqual(sent, 1)
        remaining = await repository.list_subscriptions()
        self.assertEqual([sub.endpoint for sub in remaining], [live.endpoint])

    async def test_empty_vapid_key_skips_send(self) -> None:
        repository = DryRunSubscriptionRepository()
        sub = _subscription("https://push.example.com/v1/a")
        await repository.upsert_subscription(sub)
        with patch("app.services.web_push.webpush") as mock_webpush:
            sent = await send_web_push_alerts(
                [sub],
                {"title_en": "x"},
                VapidConfig(private_key="", subject="mailto:admin@example.com"),
                repository=repository,
            )
        self.assertEqual(sent, 0)
        mock_webpush.assert_not_called()
        self.assertEqual(len(await repository.list_subscriptions()), 1)

    async def test_non_success_status_logged_and_kept(self) -> None:
        repository = DryRunSubscriptionRepository()
        sub = _subscription("https://push.example.com/v1/a")
        await repository.upsert_subscription(sub)
        err_response = MagicMock(status_code=500)
        with patch("app.services.web_push.webpush", return_value=err_response):
            sent = await send_web_push_alerts(
                [sub],
                {"title_en": "x"},
                _vapid(),
                repository=repository,
            )
        self.assertEqual(sent, 0)
        # A 500 is a transient server error, not a gone subscription: keep it.
        self.assertEqual(len(await repository.list_subscriptions()), 1)


class WebPushPayloadTests(unittest.TestCase):
    def test_bilingual_orange_payload_matches_spec(self) -> None:
        from app.schemas.common import RiskLevel

        payload = build_web_push_payload(
            level=RiskLevel.ORANGE,
            valid_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
            dashboard_url=DASHBOARD_URL,
        )
        self.assertEqual(payload["title_en"], "Flood Alert – ORANGE")
        self.assertEqual(payload["title_th"], "แจ้งเตือนน้ำท่วม – เฝ้าระวัง")
        self.assertIn("2026-05-27 18:00 UTC", payload["body_en"])
        self.assertIn("2026-05-27 18:00 UTC", payload["body_th"])
        self.assertEqual(payload["url"], DASHBOARD_URL)
        self.assertEqual(payload["risk_level"], "orange")


class DispatchWebPushAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_triggered_dispatch_sends_and_persists_state(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        from app.schemas.common import RiskLevel
        from app.services.alert_dispatch import (
            WEB_PUSH_SOURCE,
            dispatch_web_push_alert,
            read_alert_state,
        )

        client = AsyncMongoMockClient()
        database = client["hatyai_flood_warning_test"]
        repository = DryRunSubscriptionRepository()
        await repository.upsert_subscription(_subscription("https://push.example.com/v1/a"))

        ok_response = MagicMock(status_code=201)
        with patch("app.services.web_push.webpush", return_value=ok_response) as mock_webpush:
            decision = await dispatch_web_push_alert(
                database=database,
                repository=repository,
                current_level=RiskLevel.ORANGE,
                valid_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
                vapid_config=_vapid(),
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
            )

        self.assertTrue(decision.should_send)
        mock_webpush.assert_called_once()
        stored = await read_alert_state(database, source=WEB_PUSH_SOURCE)
        assert stored is not None
        self.assertEqual(stored.last_risk_level, RiskLevel.ORANGE)


if __name__ == "__main__":
    unittest.main()
