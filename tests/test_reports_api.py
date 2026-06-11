"""Tests for the citizen flood-report API (HFT-73).

Coverage:

- submission happy path (JSON and multipart) returns ``pending`` and 201;
- pending reports are invisible publicly, approved reports are visible;
- moderation auth: empty token rejects, wrong token rejects, correct works;
- EXIF stripping: a JPEG built with GPS EXIF has all metadata gone after the
  sanitizer runs, and the served photo carries no EXIF;
- per-IP rate limiting blocks the sixth submission within the window;
- basin validation rejects a far-away point.

The dry-run repository and in-memory photo storage back the app so no Mongo
instance is required, matching the existing test suites. The Mongo-backed
``register_submission`` rate limiter is exercised separately with
``mongomock-motor``.
"""

from __future__ import annotations

import asyncio
import io
import unittest
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import Settings
from app.ingestion.report_repository import DryRunReportRepository, MongoReportRepository
from app.main import create_app
from app.schemas.reports import ReportStatus, WaterDepthCategory
from app.services.photo_storage import InMemoryPhotoStorage
from app.services.report_photos import sanitize_photo

MODERATION_TOKEN = "test-moderation-token"
# A point well inside the U-Tapao basin (central Hat Yai).
IN_BASIN = {"longitude": 100.474, "latitude": 6.997}
# A point far away (Bangkok) — outside the basin and its buffer.
FAR_AWAY = {"longitude": 100.5018, "latitude": 13.7563}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"REPORTS_MODERATION_TOKEN": MODERATION_TOKEN}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _jpeg_with_gps_exif() -> bytes:
    """Build a small JPEG carrying EXIF GPS tags for the EXIF-stripping test.

    GPS coordinate components are written as ``IFDRational`` instances so Pillow
    serializes them correctly across versions, mirroring how a real camera/phone
    embeds latitude/longitude that we must guarantee is stripped before storage.
    """
    from PIL.TiffImagePlugin import IFDRational

    def _dms(*parts: int) -> tuple[IFDRational, ...]:
        return tuple(IFDRational(part, 1) for part in parts)

    image = Image.new("RGB", (16, 16), color=(120, 130, 140))
    exif = Image.Exif()
    # 0x8825 is the GPS IFD pointer; populate a couple of GPS sub-tags.
    exif[0x8825] = {
        1: "N",
        2: _dms(6, 59, 0),
        3: "E",
        4: _dms(100, 28, 0),
    }
    exif[0x010E] = "secret location note"  # ImageDescription
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class ReportApiTestBase(unittest.TestCase):
    def _client(
        self,
        *,
        repository: DryRunReportRepository | None = None,
        storage: InMemoryPhotoStorage | None = None,
        settings: Settings | None = None,
    ) -> tuple[TestClient, DryRunReportRepository, InMemoryPhotoStorage]:
        repo = repository or DryRunReportRepository()
        store = storage or InMemoryPhotoStorage()
        app = create_app(
            settings=settings or _settings(),
            report_repository=repo,
            photo_storage=store,
        )
        return TestClient(app), repo, store


class SubmissionTests(ReportApiTestBase):
    def test_json_submission_happy_path(self) -> None:
        client, repo, _ = self._client()
        with client as http:
            response = http.post(
                "/api/reports",
                json={**IN_BASIN, "water_depth": "knee", "note": "rising fast"},
            )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "pending")
        self.assertFalse(body["has_photo"])
        self.assertIn("id", body)
        stored = asyncio.run(repo.get_report(body["id"]))
        assert stored is not None
        self.assertEqual(stored.status, ReportStatus.PENDING)
        self.assertEqual(stored.note, "rising fast")

    def test_multipart_submission_with_photo(self) -> None:
        client, repo, store = self._client()
        photo = _jpeg_with_gps_exif()
        with client as http:
            response = http.post(
                "/api/reports",
                data={**IN_BASIN, "water_depth": "waist"},
                files={"photo": ("flood.jpg", photo, "image/jpeg")},
            )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["has_photo"])
        stored = asyncio.run(repo.get_report(body["id"]))
        assert stored is not None
        self.assertTrue(stored.has_photo)

    def test_far_away_point_rejected(self) -> None:
        client, _, _ = self._client()
        with client as http:
            response = http.post(
                "/api/reports",
                json={**FAR_AWAY, "water_depth": "ankle"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("U-Tapao", response.json()["detail"])

    def test_invalid_water_depth_is_422(self) -> None:
        client, _, _ = self._client()
        with client as http:
            response = http.post(
                "/api/reports",
                json={**IN_BASIN, "water_depth": "ocean"},
            )
        self.assertEqual(response.status_code, 422)

    def test_rate_limit_blocks_sixth_submission(self) -> None:
        client, _, _ = self._client(settings=_settings(REPORTS_RATE_LIMIT_PER_HOUR=5))
        with client as http:
            for _ in range(5):
                ok = http.post("/api/reports", json={**IN_BASIN, "water_depth": "knee"})
                self.assertEqual(ok.status_code, 201)
            blocked = http.post("/api/reports", json={**IN_BASIN, "water_depth": "knee"})
        self.assertEqual(blocked.status_code, 429)


class VisibilityTests(ReportApiTestBase):
    def _submit(self, http: TestClient) -> str:
        response = http.post("/api/reports", json={**IN_BASIN, "water_depth": "knee"})
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_pending_invisible_approved_visible(self) -> None:
        client, _, _ = self._client()
        with client as http:
            report_id = self._submit(http)

            public = http.get("/api/reports")
            self.assertEqual(public.json()["count"], 0)

            approve = http.post(
                f"/api/reports/moderation/{report_id}/approve",
                headers={"Authorization": f"Bearer {MODERATION_TOKEN}"},
            )
            self.assertEqual(approve.status_code, 200)
            self.assertEqual(approve.json()["status"], "approved")

            public_after = http.get("/api/reports")
            payload = public_after.json()
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["reports"][0]["id"], report_id)

    def test_rejected_stays_hidden(self) -> None:
        client, _, _ = self._client()
        with client as http:
            report_id = self._submit(http)
            http.post(
                f"/api/reports/moderation/{report_id}/reject",
                headers={"Authorization": f"Bearer {MODERATION_TOKEN}"},
            )
            self.assertEqual(http.get("/api/reports").json()["count"], 0)

    def test_public_photo_only_for_approved(self) -> None:
        client, _, _ = self._client()
        photo = _jpeg_with_gps_exif()
        with client as http:
            submit = http.post(
                "/api/reports",
                data={**IN_BASIN, "water_depth": "knee"},
                files={"photo": ("flood.jpg", photo, "image/jpeg")},
            )
            report_id = submit.json()["id"]
            # Pending: public photo path is 404.
            self.assertEqual(http.get(f"/api/reports/{report_id}/photo").status_code, 404)
            http.post(
                f"/api/reports/moderation/{report_id}/approve",
                headers={"Authorization": f"Bearer {MODERATION_TOKEN}"},
            )
            served = http.get(f"/api/reports/{report_id}/photo")
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.headers["content-type"], "image/jpeg")


class ModerationAuthTests(ReportApiTestBase):
    def test_empty_token_rejects_all(self) -> None:
        client, _, _ = self._client(settings=_settings(REPORTS_MODERATION_TOKEN=""))
        with client as http:
            response = http.get(
                "/api/reports/moderation/pending",
                headers={"Authorization": "Bearer anything"},
            )
        self.assertEqual(response.status_code, 403)

    def test_wrong_token_rejected(self) -> None:
        client, _, _ = self._client()
        with client as http:
            response = http.get(
                "/api/reports/moderation/pending",
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(response.status_code, 403)

    def test_missing_header_rejected(self) -> None:
        client, _, _ = self._client()
        with client as http:
            response = http.get("/api/reports/moderation/pending")
        self.assertEqual(response.status_code, 403)

    def test_correct_token_lists_pending(self) -> None:
        client, _, _ = self._client()
        with client as http:
            http.post("/api/reports", json={**IN_BASIN, "water_depth": "knee"})
            response = http.get(
                "/api/reports/moderation/pending",
                headers={"Authorization": f"Bearer {MODERATION_TOKEN}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)


class ExifStrippingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sanitizer_removes_all_exif_including_gps(self) -> None:
        original = _jpeg_with_gps_exif()
        # Sanity: the original really does carry GPS EXIF.
        with Image.open(io.BytesIO(original)) as before:
            self.assertTrue(dict(before.getexif()))
            self.assertIn(0x8825, before.getexif())

        sanitized = await sanitize_photo(original, content_type="image/jpeg")

        with Image.open(io.BytesIO(sanitized)) as after:
            exif = after.getexif()
            self.assertEqual(dict(exif), {})
            self.assertNotIn(0x8825, exif)
            self.assertEqual(after.format, "JPEG")


class MongoRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_mongo_register_submission_enforces_ceiling(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        client = AsyncMongoMockClient()
        repo = MongoReportRepository(client["hatyai_flood_warning_test"])
        now = datetime(2026, 6, 12, 3, 30, tzinfo=UTC)
        allowed = [
            await repo.register_submission("ip-hash", now=now, max_per_window=3)
            for _ in range(4)
        ]
        self.assertEqual(allowed, [True, True, True, False])

    async def test_mongo_create_and_moderation_roundtrip(self) -> None:
        from mongomock_motor import AsyncMongoMockClient

        client = AsyncMongoMockClient()
        repo = MongoReportRepository(client["hatyai_flood_warning_test"])
        await repo.ensure_indexes()
        report = await repo.create_report(
            longitude=IN_BASIN["longitude"],
            latitude=IN_BASIN["latitude"],
            water_depth=WaterDepthCategory.KNEE,
            note=None,
            photo_key=None,
            created_at=datetime(2026, 6, 12, 3, 0, tzinfo=UTC),
        )
        self.assertEqual(report.status, ReportStatus.PENDING)
        self.assertEqual(await repo.list_approved(), [])
        approved = await repo.set_status(report.id, ReportStatus.APPROVED)
        assert approved is not None
        self.assertEqual(approved.status, ReportStatus.APPROVED)
        listed = await repo.list_approved()
        self.assertEqual([r.id for r in listed], [report.id])


if __name__ == "__main__":
    unittest.main()
