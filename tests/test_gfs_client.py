"""Unit and integration tests for the real GFS forecast provider client.

The unit tests load tiny pre-recorded GRIB2 messages from
``tests/fixtures`` and replay them through a stubbed ``httpx.Client`` so the
provider client can be exercised without network access. A separate test marked
with the ``GFS_LIVE`` environment variable hits NOAA NOMADS directly; CI must
leave this off so test runs do not depend on live infrastructure.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.ingestion.gfs_client import (
    GfsBoundingBox,
    GfsForecastProviderClient,
    GfsIngestionError,
    decode_apcp_message,
)
from app.ingestion.models import ForecastProvider, FreshnessStatus
from app.ingestion.normalizer import build_run_record, normalize_frames

FIXTURE_DIR = Path(__file__).parent / "fixtures"
F006_BYTES = (FIXTURE_DIR / "gfs_apcp_f006_sample.grib2").read_bytes()
F012_BYTES = (FIXTURE_DIR / "gfs_apcp_f012_sample.grib2").read_bytes()
PHASE1_BBOX = GfsBoundingBox(west=100.15, south=6.55, east=100.95, north=7.35)


class _StubResponse:
    """Tiny stand-in for ``httpx.Response`` used by the stub transport."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class _StubHttpClient:
    """Stub ``httpx.Client`` that returns canned bytes per forecast hour.

    The real client only calls ``.get(url, params=..., headers=...)`` and uses
    the ``status_code`` and ``content`` attributes of the response, plus context
    manager support. We mirror that minimal surface here so unit tests do not
    need a real network or a transport mock.
    """

    def __init__(self, payloads: dict[int, bytes]) -> None:
        self._payloads = payloads
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> _StubHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StubResponse:
        params = params or {}
        forecast_hour = _parse_forecast_hour_from_filename(params.get("file", ""))
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "headers": dict(headers or {}),
                "forecast_hour": forecast_hour,
            }
        )
        if forecast_hour not in self._payloads:
            return _StubResponse(404, b"not found")
        body = self._payloads[forecast_hour]
        if headers and headers.get("Range", "").startswith("bytes="):
            # Mimic NOMADS partial-content response for the availability probe.
            return _StubResponse(206, body[:16])
        return _StubResponse(200, body)


def _parse_forecast_hour_from_filename(filename: str) -> int:
    marker = ".f"
    idx = filename.rfind(marker)
    if idx == -1:
        return -1
    return int(filename[idx + len(marker) : idx + len(marker) + 3])


def _client_factory_from(
    payloads: dict[int, bytes],
) -> tuple[_StubHttpClient, Callable[[], _StubHttpClient]]:
    stub = _StubHttpClient(payloads)

    def factory() -> _StubHttpClient:
        return stub

    return stub, factory


class DecodeApcpMessageTests(unittest.TestCase):
    def test_picks_interval_record_for_f006(self) -> None:
        decoded = decode_apcp_message(F006_BYTES, forecast_hour=6)

        self.assertEqual(decoded.accumulation_hours, 6)
        self.assertEqual(decoded.units, "kg m**-2")
        self.assertEqual(decoded.width, 5)
        self.assertEqual(decoded.height, 5)
        self.assertAlmostEqual(decoded.resolution_degrees, 0.25)
        self.assertIn("stepRange=0-6", decoded.semantics)
        self.assertEqual(min(decoded.values_mm), 0.0)
        self.assertGreater(max(decoded.values_mm), 0.0)

    def test_picks_short_window_for_f012_over_run_total(self) -> None:
        decoded = decode_apcp_message(F012_BYTES, forecast_hour=12)

        # The fixture contains both 6-12 (interval) and 0-12 (run total). The
        # decoder must always choose the interval so totals do not silently
        # mix incompatible windows.
        self.assertEqual(decoded.accumulation_hours, 6)
        self.assertIn("stepRange=6-12", decoded.semantics)

    def test_raises_when_no_message_matches_forecast_hour(self) -> None:
        with self.assertRaises(GfsIngestionError):
            decode_apcp_message(F006_BYTES, forecast_hour=24)


class GfsForecastProviderClientUnitTests(unittest.TestCase):
    def test_fetch_run_returns_frames_with_correct_provenance(self) -> None:
        stub, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})

        client = GfsForecastProviderClient(
            forecast_hours=(6, 12),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
        from app.ingestion.providers import ProviderRunRef

        run_ref = ProviderRunRef(
            provider=ForecastProvider.GFS,
            model=client.model,
            product=client.product,
            run_time=run_time,
            cycle_hours=client.cycle_hours,
            freshness_threshold_hours=client.freshness_threshold_hours,
            license=client.license,
            attribution=client.attribution,
        )

        artifacts = client.fetch_run(run_ref)
        self.assertEqual(len(artifacts), 2)
        first = artifacts[0]
        self.assertEqual(first.forecast_hour, 6)
        self.assertEqual(first.accumulation_hours, 6)
        self.assertIn("stepRange=0-6", first.provider_accumulation_semantics)
        self.assertGreater(len(first.values_mm), 0)
        self.assertGreaterEqual(min(first.values_mm), 0.0)
        self.assertEqual(first.grid_width, 5)
        self.assertEqual(first.grid_height, 5)
        self.assertAlmostEqual(first.grid_resolution_degrees, 0.25)
        self.assertIn(
            "gfs.20260501/00/atmos/gfs.t00z.pgrb2.0p25.f006",
            first.source_url,
        )
        self.assertEqual(
            first.raw_artifact_ref,
            "gfs/20260501/00/pgrb2.0p25.apcp/f006.grib2",
        )

        second = artifacts[1]
        self.assertEqual(second.forecast_hour, 12)
        self.assertIn("stepRange=6-12", second.provider_accumulation_semantics)

        # Stub recorded the bbox subregion params verbatim.
        f006_call = next(call for call in stub.calls if call["forecast_hour"] == 6)
        params = f006_call["params"]
        self.assertEqual(params.get("var_APCP"), "on")
        self.assertEqual(params.get("subregion"), "")
        self.assertEqual(params.get("leftlon"), "100.15")
        self.assertEqual(params.get("rightlon"), "100.95")
        self.assertEqual(params.get("toplat"), "7.35")
        self.assertEqual(params.get("bottomlat"), "6.55")

    def test_discover_latest_run_picks_most_recent_published_cycle(self) -> None:
        stub, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})
        client = GfsForecastProviderClient(
            forecast_hours=(6, 12),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        # 03:00Z means the 00Z cycle is the only one published; the probe will
        # see a 206 response with the GRIB header bytes and accept it.
        now = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
        run_ref = client.discover_latest_run(now)

        self.assertEqual(run_ref.run_time, datetime(2026, 5, 1, 0, tzinfo=UTC))
        self.assertEqual(run_ref.provider, ForecastProvider.GFS)
        self.assertEqual(run_ref.model, "gfs")
        self.assertEqual(run_ref.product, "pgrb2.0p25.apcp")
        self.assertEqual(run_ref.freshness_threshold_hours, 7)
        self.assertEqual(run_ref.attribution, "NOAA/NCEP Global Forecast System (GFS)")

    def test_discover_latest_run_raises_when_no_cycle_within_freshness(self) -> None:
        # Empty payload map means every probe responds with 404, so no cycle
        # is available within the freshness window.
        _, factory = _client_factory_from({})
        client = GfsForecastProviderClient(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        with self.assertRaises(GfsIngestionError):
            client.discover_latest_run(datetime(2026, 5, 1, 3, 0, tzinfo=UTC))

    def test_fetch_run_normalizes_into_forecast_frames_with_fresh_status(self) -> None:
        _, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})
        client = GfsForecastProviderClient(
            forecast_hours=(6, 12),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        run_ref = client.discover_latest_run(retrieved_at)
        artifacts = client.fetch_run(run_ref)
        run_record = build_run_record(run_ref, artifacts, retrieved_at)
        frames = normalize_frames(run_ref, artifacts, retrieved_at)

        self.assertEqual(len(frames), 2)
        self.assertEqual(run_record.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(run_record.expected_forecast_hours, [6, 12])
        f006 = frames[0]
        self.assertEqual(f006.forecast_hour, 6)
        self.assertEqual(f006.accumulation_hours, 6)
        self.assertEqual(f006.unit, "mm")
        self.assertEqual(f006.window_end, f006.valid_time)
        self.assertEqual(f006.valid_time, run_ref.run_time.replace(hour=6))
        self.assertGreaterEqual(f006.quality.minimum_mm, 0.0)
        self.assertGreaterEqual(
            f006.quality.maximum_mm, f006.quality.minimum_mm
        )
        self.assertIn(
            "stepRange=0-6", f006.provider_accumulation_semantics
        )
        self.assertEqual(f006.source.attribution, "NOAA/NCEP Global Forecast System (GFS)")
        self.assertIn("NOAA", f006.source.license)


@unittest.skipUnless(
    os.environ.get("GFS_LIVE") == "1",
    "Set GFS_LIVE=1 to exercise the live NOMADS endpoint",
)
class GfsForecastProviderClientLiveTests(unittest.TestCase):
    """Live NOMADS check; opt-in only via the GFS_LIVE environment variable."""

    def test_one_cycle_round_trips_with_fresh_status(self) -> None:
        from app.ingestion.gfs_client import build_gfs_client

        client = build_gfs_client(forecast_hours=(6, 12), bbox=PHASE1_BBOX)
        now = datetime.now(UTC)
        run_ref = client.discover_latest_run(now)
        artifacts = client.fetch_run(run_ref)
        retrieved_at = datetime.now(UTC)
        run_record = build_run_record(run_ref, artifacts, retrieved_at)
        frames = normalize_frames(run_ref, artifacts, retrieved_at)

        self.assertEqual(run_record.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(len(frames), 2)
        for frame in frames:
            self.assertEqual(frame.unit, "mm")
            self.assertEqual(frame.window_end, frame.valid_time)
            self.assertGreater(frame.grid.width, 0)
            self.assertGreater(frame.grid.height, 0)
            self.assertGreaterEqual(min(frame.values_mm), 0.0)
            self.assertIn("stepRange=", frame.provider_accumulation_semantics)


# Suppress unused-import warnings for re-exported helpers used only by hints.
_ = (httpx, contextmanager, Iterator)


if __name__ == "__main__":
    unittest.main()
