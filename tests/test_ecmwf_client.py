"""Unit tests for the real ECMWF Open Data forecast provider client.

Tests load small pre-generated GRIB2 messages from ``tests/fixtures`` and
replay them through a stub HTTP client so no network access is needed. A
separate test class guarded by ``ECMWF_LIVE=1`` exercises the real CDN.

ECMWF tp accumulation semantics recap (tested explicitly below):
- Each ECMWF file contains tp accumulated from T+0 to the forecast step (metres).
- step 006: total_006 metres
- step 012: total_012 metres → window 6-12 h = (total_012 - total_006) * 1000 mm
- step 024: total_024 metres → window 12-24 h = (total_024 - total_012) * 1000 mm
This differs from GFS APCP which already contains per-interval values.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

from app.ingestion.ecmwf_client import (
    EcmwfBoundingBox,
    EcmwfIngestionError,
    EcmwfOpenDataProviderClient,
    build_ecmwf_client,
    decode_tp_message,
)
from app.ingestion.models import ForecastProvider, FreshnessStatus
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import ProviderRunRef

FIXTURE_DIR = Path(__file__).parent / "fixtures"
F006_BYTES = (FIXTURE_DIR / "ecmwf_tp_f006_sample.grib2").read_bytes()
F012_BYTES = (FIXTURE_DIR / "ecmwf_tp_f012_sample.grib2").read_bytes()
F024_BYTES = (FIXTURE_DIR / "ecmwf_tp_f024_sample.grib2").read_bytes()

# Phase 1 bounding box matches the Phase1Area default.
PHASE1_BBOX = EcmwfBoundingBox(west=100.15, south=6.55, east=100.95, north=7.35)

# Fixtures were generated with a 5x5 grid covering 100.0–101.0 / 6.5–7.5
# at 0.25-degree resolution. The Phase 1 bbox (100.15–100.95, 6.55–7.35)
# clips to inner points. We pin the expected count to the actual value
# produced by the decode_tp_message clipping logic.
EXPECTED_CLIP_FIXTURE_COUNT = 9  # cells inside [100.15,6.55,100.95,7.35] from 5×5 grid


# ---------------------------------------------------------------------------
# Stub HTTP client
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class _StubHttpClient:
    """Stub ``httpx.Client`` that returns fixture bytes keyed by forecast hour.

    The real client calls ``.get(url, headers=...)`` and reads ``status_code``
    and ``content`` from the response. The stub parses the forecast hour out of
    the ECMWF URL pattern ``.../{YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2``.
    """

    def __init__(self, payloads: dict[int, bytes], *, probe_status: int = 206) -> None:
        self._payloads = payloads
        self._probe_status = probe_status
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
        forecast_hour = _parse_forecast_hour_from_url(url)
        self.calls.append(
            {"url": url, "headers": dict(headers or {}), "forecast_hour": forecast_hour}
        )
        if forecast_hour not in self._payloads:
            return _StubResponse(404, b"not found")
        body = self._payloads[forecast_hour]
        if headers and headers.get("Range", "").startswith("bytes="):
            return _StubResponse(self._probe_status, body[:16])
        return _StubResponse(200, body)


def _parse_forecast_hour_from_url(url: str) -> int:
    """Extract the forecast step from the ECMWF filename pattern ``...-{step}h-...``."""
    import re

    match = re.search(r"-(\d+)h-oper-fc\.grib2", url)
    if match:
        return int(match.group(1))
    return -1


def _client_factory_from(
    payloads: dict[int, bytes],
    *,
    probe_status: int = 206,
) -> tuple[_StubHttpClient, Callable[[], _StubHttpClient]]:
    stub = _StubHttpClient(payloads, probe_status=probe_status)

    def factory() -> _StubHttpClient:
        return stub

    return stub, factory


def _make_run_ref(
    client: EcmwfOpenDataProviderClient,
    run_time: datetime,
) -> ProviderRunRef:
    return ProviderRunRef(
        provider=ForecastProvider.ECMWF_OPEN_DATA,
        model=client.model,
        product=client.product,
        run_time=run_time,
        cycle_hours=client.cycle_hours,
        freshness_threshold_hours=client.freshness_threshold_hours,
        license=client.license,
        attribution=client.attribution,
    )


# ---------------------------------------------------------------------------
# decode_tp_message unit tests
# ---------------------------------------------------------------------------


class DecodeTpMessageTests(unittest.TestCase):
    def test_decodes_f006_run_total_in_metres(self) -> None:
        decoded = decode_tp_message(F006_BYTES, forecast_hour=6)

        self.assertEqual(decoded.end_step, 6)
        self.assertEqual(decoded.start_step, 0)
        self.assertEqual(decoded.step_range, "0-6")
        self.assertEqual(decoded.units, "m")
        self.assertAlmostEqual(decoded.resolution_degrees, 0.25)
        # Fixture generates values_m[i] = 0.001 * (i+1); all positive.
        self.assertGreater(len(decoded.values_m), 0)
        self.assertGreater(min(decoded.values_m), 0.0)

    def test_decodes_f012_run_total_in_metres(self) -> None:
        decoded = decode_tp_message(F012_BYTES, forecast_hour=12)

        self.assertEqual(decoded.end_step, 12)
        self.assertEqual(decoded.step_range, "0-12")
        self.assertEqual(decoded.units, "m")

    def test_decodes_f024_run_total_in_metres(self) -> None:
        decoded = decode_tp_message(F024_BYTES, forecast_hour=24)

        self.assertEqual(decoded.end_step, 24)
        self.assertEqual(decoded.units, "m")

    def test_raises_when_forecast_hour_not_in_message(self) -> None:
        with self.assertRaises(EcmwfIngestionError):
            decode_tp_message(F006_BYTES, forecast_hour=99)

    def test_bbox_clipping_returns_only_cells_within_bounds(self) -> None:
        # Fixture grid: 100.0–101.0 lon, 6.5–7.5 lat at 0.25°.
        # Phase1 bbox: west=100.15, south=6.55, east=100.95, north=7.35.
        # Points exactly on the lower-left cell (100.0, 6.5) are excluded.
        decoded_full = decode_tp_message(F006_BYTES, forecast_hour=6)
        decoded_clipped = decode_tp_message(F006_BYTES, forecast_hour=6, bbox=PHASE1_BBOX)

        # Clipping should reduce the number of cells.
        self.assertLess(len(decoded_clipped.values_m), len(decoded_full.values_m))
        # All values in the full grid are positive; clipped values must also be.
        self.assertTrue(all(v >= 0 for v in decoded_clipped.values_m))
        self.assertEqual(len(decoded_clipped.values_m), EXPECTED_CLIP_FIXTURE_COUNT)

    def test_bbox_clipping_preserves_2d_grid_dimensions(self) -> None:
        """Clipped grid keeps proper width/height instead of collapsing to height=1."""
        # Phase1 bbox over a 5x5 0.25° fixture yields a 3×3 inner block:
        # lons {100.25, 100.50, 100.75} × lats {6.75, 7.00, 7.25} = 9 cells.
        decoded = decode_tp_message(F006_BYTES, forecast_hour=6, bbox=PHASE1_BBOX)
        self.assertEqual(decoded.width, 3)
        self.assertEqual(decoded.height, 3)
        self.assertEqual(decoded.width * decoded.height, len(decoded.values_m))

    def test_bbox_clipping_raises_when_no_cells_within_bounds(self) -> None:
        # Use a bbox that does not overlap the fixture grid at all.
        far_away_bbox = EcmwfBoundingBox(west=0.0, south=0.0, east=1.0, north=1.0)
        with self.assertRaises(EcmwfIngestionError):
            decode_tp_message(F006_BYTES, forecast_hour=6, bbox=far_away_bbox)


# ---------------------------------------------------------------------------
# Accumulation semantics tests
# ---------------------------------------------------------------------------


class AccumulationSemanticsTests(unittest.TestCase):
    """Verify that window mm values are correctly derived from run-total metres."""

    def test_first_step_window_equals_run_total_converted_to_mm(self) -> None:
        # For step 006 (first step), window = tp[006] * 1000.
        decoded_006 = decode_tp_message(F006_BYTES, forecast_hour=6)

        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5),
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
        run_ref = _make_run_ref(client, run_time)
        artifacts = client.fetch_run(run_ref)

        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        self.assertEqual(artifact.forecast_hour, 6)
        self.assertEqual(artifact.accumulation_hours, 6)

        # Window for the first step = run-total * 1000 mm.
        expected_mm = tuple(round(v * 1000.0, 4) for v in decoded_006.values_m)
        self.assertEqual(artifact.values_mm, expected_mm)

    def test_second_step_window_subtracts_prior_step(self) -> None:
        # For step 012 (second step after 006):
        # window_mm[i] = (tp012[i] - tp006[i]) * 1000
        decoded_006 = decode_tp_message(F006_BYTES, forecast_hour=6)
        decoded_012 = decode_tp_message(F012_BYTES, forecast_hour=12)

        # Use the full fixture grid (no bbox clip) so indices align.
        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6, 12),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))
        artifacts = client.fetch_run(run_ref)

        self.assertEqual(len(artifacts), 2)
        step_012_artifact = artifacts[1]
        self.assertEqual(step_012_artifact.forecast_hour, 12)
        self.assertEqual(step_012_artifact.accumulation_hours, 6)

        expected_mm = tuple(
            max(0.0, round((t12 - t06) * 1000.0, 4))
            for t12, t06 in zip(decoded_012.values_m, decoded_006.values_m, strict=True)
        )
        self.assertEqual(step_012_artifact.values_mm, expected_mm)

    def test_third_step_subtracts_second_step(self) -> None:
        # For step 024 (third step after 006, 012):
        # window_mm[i] = (tp024[i] - tp012[i]) * 1000
        decoded_012 = decode_tp_message(F012_BYTES, forecast_hour=12)
        decoded_024 = decode_tp_message(F024_BYTES, forecast_hour=24)

        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES, 24: F024_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6, 12, 24),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))
        artifacts = client.fetch_run(run_ref)

        self.assertEqual(len(artifacts), 3)
        step_024_artifact = artifacts[2]
        self.assertEqual(step_024_artifact.forecast_hour, 24)
        self.assertEqual(step_024_artifact.accumulation_hours, 12)

        expected_mm = tuple(
            max(0.0, round((t24 - t12) * 1000.0, 4))
            for t24, t12 in zip(decoded_024.values_m, decoded_012.values_m, strict=True)
        )
        self.assertEqual(step_024_artifact.values_mm, expected_mm)

    def test_accumulation_semantics_string_records_step_derivation(self) -> None:
        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6, 12),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))
        artifacts = client.fetch_run(run_ref)

        # Both artifacts should record the ECMWF run-accumulated derivation.
        for artifact in artifacts:
            self.assertIn("ECMWF", artifact.provider_accumulation_semantics)
            self.assertIn("run-accumulated", artifact.provider_accumulation_semantics)

        # Step 012 artifact must record it was derived from tp[12] - tp[6].
        step_012 = artifacts[1]
        self.assertIn("step=12h", step_012.provider_accumulation_semantics)
        self.assertIn("step=6h", step_012.provider_accumulation_semantics)

    def test_unit_conversion_metres_to_mm(self) -> None:
        # tp fixture values for step 006: values_m[i] = 0.001 * (i+1)
        # Expected mm after conversion = 0.001 * (i+1) * 1000 = (i+1)
        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))
        artifacts = client.fetch_run(run_ref)

        self.assertEqual(len(artifacts), 1)
        # All values should be positive (non-zero in the fixture) and in mm.
        self.assertTrue(all(v > 0 for v in artifacts[0].values_mm))
        # Verify approximate magnitude: fixture step 006 is ~1 mm per cell.
        first_mm = artifacts[0].values_mm[0]
        self.assertAlmostEqual(first_mm, 1.0, places=1)


# ---------------------------------------------------------------------------
# Cycle discovery tests
# ---------------------------------------------------------------------------


class DiscoverLatestRunTests(unittest.TestCase):
    def test_picks_most_recent_cycle_when_available(self) -> None:
        # At 03:00Z on 2026-05-01, only the 00Z cycle is old enough to be published.
        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        now = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
        run_ref = client.discover_latest_run(now)

        self.assertEqual(run_ref.run_time, datetime(2026, 5, 1, 0, tzinfo=UTC))
        self.assertEqual(run_ref.provider, ForecastProvider.ECMWF_OPEN_DATA)
        self.assertEqual(run_ref.model, "ifs")
        self.assertEqual(run_ref.freshness_threshold_hours, 13)
        self.assertEqual(run_ref.attribution, "ECMWF Open Data — IFS Forecast")
        self.assertEqual(run_ref.license, "CC-BY-4.0")

    def test_falls_back_to_previous_12z_when_00z_not_yet_published(self) -> None:
        # The probe for the most-recent 00Z cycle returns 404 (not published yet).
        # The client should fall back to the previous day's 12Z cycle.
        stub, factory = _client_factory_from({6: F006_BYTES})

        # Override: only respond 206 for the 12Z probe from the previous day.
        previous_day_12z = datetime(2026, 4, 30, 12, tzinfo=UTC)

        class _SelectiveStub(_StubHttpClient):
            def get(
                self,
                url: str,
                *,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> _StubResponse:
                # Accept only the previous 12Z probe; reject everything else.
                range_probe = (headers or {}).get("Range", "").startswith("bytes=")
                if "20260430" in url and "12z" in url and range_probe:
                    body = F006_BYTES
                    return _StubResponse(206, body[:16])
                if headers and headers.get("Range", "").startswith("bytes="):
                    return _StubResponse(404, b"not found")
                # Non-probe GET for fetch_run.
                return _StubResponse(200, F006_BYTES)

        selective = _SelectiveStub({6: F006_BYTES})

        def selective_factory() -> _SelectiveStub:
            return selective

        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=selective_factory,  # type: ignore[arg-type]
        )

        # At 01:00Z on 2026-05-01 the 00Z cycle is not published yet.
        now = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)
        run_ref = client.discover_latest_run(now)

        self.assertEqual(run_ref.run_time, previous_day_12z)

    def test_raises_when_no_cycle_within_freshness_window(self) -> None:
        # Empty payload map means all probes return 404.
        _, factory = _client_factory_from({})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        with self.assertRaises(EcmwfIngestionError):
            client.discover_latest_run(datetime(2026, 5, 1, 3, 0, tzinfo=UTC))

    def test_raises_when_only_stale_cycles_are_available(self) -> None:
        # Simulate all CDN probes failing (empty payload → 404) while still
        # within the candidate window. The client must raise rather than
        # silently return an unavailable run.
        _, factory = _client_factory_from({})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            freshness_threshold_hours=13,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        with self.assertRaises(EcmwfIngestionError):
            client.discover_latest_run(datetime(2026, 5, 1, 3, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# fetch_run error path tests
# ---------------------------------------------------------------------------


class FetchRunErrorTests(unittest.TestCase):
    def test_raises_when_provider_is_not_ecmwf(self) -> None:
        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        wrong_ref = ProviderRunRef(
            provider=ForecastProvider.GFS,
            model="gfs",
            product="pgrb2.0p25.apcp",
            run_time=datetime(2026, 5, 1, 0, tzinfo=UTC),
            cycle_hours=(0, 6, 12, 18),
            freshness_threshold_hours=7,
            license="NOAA",
            attribution="NOAA/NCEP GFS",
        )

        with self.assertRaises(EcmwfIngestionError):
            client.fetch_run(wrong_ref)

    def test_raises_on_non_200_http_response(self) -> None:
        # Stub returns 500 for the download request.
        class _ErrorStub:
            def __enter__(self) -> _ErrorStub:
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
                if (headers or {}).get("Range", "").startswith("bytes="):
                    return _StubResponse(206, F006_BYTES[:16])
                return _StubResponse(500, b"internal server error")

        def error_factory() -> _ErrorStub:
            return _ErrorStub()

        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            http_client_factory=error_factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))
        client2 = EcmwfOpenDataProviderClient(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            retries=0,
            http_client_factory=error_factory,  # type: ignore[arg-type]
        )

        with self.assertRaises(EcmwfIngestionError):
            client2.fetch_run(run_ref)

    def test_raises_on_network_error_after_retries(self) -> None:
        class _NetworkErrorStub:
            def __enter__(self) -> _NetworkErrorStub:
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
                raise httpx.ConnectError("simulated network failure")

        def net_error_factory() -> _NetworkErrorStub:
            return _NetworkErrorStub()

        client = EcmwfOpenDataProviderClient(
            forecast_hours=(6,),
            bbox=PHASE1_BBOX,
            retries=0,
            http_client_factory=net_error_factory,  # type: ignore[arg-type]
        )
        run_ref = _make_run_ref(client, datetime(2026, 5, 1, 0, tzinfo=UTC))

        with self.assertRaises(EcmwfIngestionError):
            client.fetch_run(run_ref)


# ---------------------------------------------------------------------------
# End-to-end normalizer integration test
# ---------------------------------------------------------------------------


class EcmwfNormalizerIntegrationTests(unittest.TestCase):
    def test_artifacts_normalize_into_fresh_forecast_frames(self) -> None:
        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES, 12: F012_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6, 12),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        run_ref = client.discover_latest_run(retrieved_at)
        artifacts = client.fetch_run(run_ref)
        run_record = build_run_record(run_ref, artifacts, retrieved_at)
        frames = normalize_frames(run_ref, artifacts, retrieved_at)

        self.assertEqual(len(frames), 2)
        self.assertEqual(run_record.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(run_record.provider, ForecastProvider.ECMWF_OPEN_DATA)
        self.assertEqual(run_record.attribution, "ECMWF Open Data — IFS Forecast")
        self.assertEqual(run_record.license, "CC-BY-4.0")

        frame_006 = frames[0]
        self.assertEqual(frame_006.forecast_hour, 6)
        self.assertEqual(frame_006.accumulation_hours, 6)
        self.assertEqual(frame_006.unit, "mm")
        self.assertEqual(frame_006.window_end, frame_006.valid_time)
        self.assertGreaterEqual(frame_006.quality.minimum_mm, 0.0)
        self.assertEqual(frame_006.source.license, "CC-BY-4.0")
        self.assertEqual(frame_006.source.attribution, "ECMWF Open Data — IFS Forecast")
        self.assertIn("ECMWF", frame_006.provider_accumulation_semantics)

        frame_012 = frames[1]
        self.assertEqual(frame_012.forecast_hour, 12)
        self.assertEqual(frame_012.accumulation_hours, 6)
        # Window for 12 h step = tp[12] - tp[6]; both are positive so result ≥ 0.
        self.assertGreaterEqual(min(frame_012.values_mm), 0.0)

    def test_production_gate_license_fields_are_populated(self) -> None:
        """ECMWF source records must carry SPDX id, license URL, and redistribution note.

        Per docs/data-sources.md, the License Notes production gate requires
        each provider record to carry license/terms URL and a redistribution
        and caching decision. The ECMWF real client must populate both.
        """
        full_bbox = EcmwfBoundingBox(west=100.0, south=6.5, east=101.0, north=7.5)
        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6,),
            bbox=full_bbox,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        run_ref = client.discover_latest_run(retrieved_at)
        artifacts = client.fetch_run(run_ref)
        run_record = build_run_record(run_ref, artifacts, retrieved_at)
        frames = normalize_frames(run_ref, artifacts, retrieved_at)

        # ProviderRunRef must carry the production-gate fields.
        self.assertEqual(run_ref.license, "CC-BY-4.0")
        self.assertTrue(run_ref.license_url)
        self.assertIn("creativecommons", run_ref.license_url or "")
        self.assertTrue(run_ref.redistribution_note)
        self.assertIn("CC-BY-4.0", run_ref.redistribution_note or "")
        self.assertIn("attribution", (run_ref.redistribution_note or "").lower())

        # ForecastRun and every ForecastSource must mirror the production-gate fields.
        self.assertEqual(run_record.license, "CC-BY-4.0")
        self.assertTrue(run_record.license_url)
        self.assertTrue(run_record.redistribution_note)
        for frame in frames:
            self.assertEqual(frame.source.license, "CC-BY-4.0")
            self.assertTrue(frame.source.license_url)
            self.assertTrue(frame.source.redistribution_note)


# ---------------------------------------------------------------------------
# build_ecmwf_client factory validation tests
# ---------------------------------------------------------------------------


class BuildEcmwfClientTests(unittest.TestCase):
    def test_raises_on_empty_forecast_hours(self) -> None:
        with self.assertRaises(ValueError):
            build_ecmwf_client(forecast_hours=[], bbox=PHASE1_BBOX)

    def test_raises_on_non_positive_forecast_hour(self) -> None:
        with self.assertRaises(ValueError):
            build_ecmwf_client(forecast_hours=(0, 6), bbox=PHASE1_BBOX)

    def test_deduplicates_and_sorts_forecast_hours(self) -> None:
        _, factory = _client_factory_from({6: F006_BYTES})
        client = build_ecmwf_client(
            forecast_hours=(6, 6, 6),
            bbox=PHASE1_BBOX,
            http_client_factory=factory,  # type: ignore[arg-type]
        )
        self.assertEqual(client.forecast_hours, (6,))


# ---------------------------------------------------------------------------
# Live CDN test (opt-in only)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    os.environ.get("ECMWF_LIVE") == "1",
    "Set ECMWF_LIVE=1 to exercise the live ECMWF Open Data CDN",
)
class EcmwfOpenDataClientLiveTests(unittest.TestCase):
    """Live CDN check; opt-in only via the ECMWF_LIVE environment variable."""

    def test_one_cycle_round_trips_with_fresh_status(self) -> None:
        client = build_ecmwf_client(
            forecast_hours=(6, 12),
            bbox=PHASE1_BBOX,
        )
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
            self.assertGreaterEqual(min(frame.values_mm), 0.0)
            self.assertIn("ECMWF", frame.provider_accumulation_semantics)


# Suppress unused-import linter warnings for re-exported symbols used only by hints.
_ = (httpx, cast, dataclass)


if __name__ == "__main__":
    unittest.main()
