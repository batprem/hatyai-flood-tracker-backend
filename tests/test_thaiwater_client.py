"""Unit and integration tests for the ThaiWater station observation client."""

from __future__ import annotations

import json
import unittest
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.ingestion.station_repository import DryRunStationRepository
from app.ingestion.thaiwater_client import (
    PHASE1_STATION_SEEDS,
    THAIWATER_PROVIDER_NAME,
    StationObservation,
    StationObservationClient,
    StationQualityFlag,
    StationVariable,
    ThaiwaterIngestionError,
    ThaiwaterStationClient,
)
from app.main import create_app
from app.services.water_levels import get_water_levels

# ---------------------------------------------------------------------------
# Fixed reference timestamps used across tests.
# ---------------------------------------------------------------------------

# A representative "now" in UTC and the matching Asia/Bangkok wall clock time
# the provider would publish for an observation taken five minutes earlier.
NOW = datetime(2026, 5, 17, 7, 0, tzinfo=UTC)
# 13:55 in Asia/Bangkok = 06:55 UTC, five minutes before NOW.
OBSERVED_BANGKOK_STR = "2026-05-17 13:55:00"


# ---------------------------------------------------------------------------
# Helpers to build fake provider payloads.
# ---------------------------------------------------------------------------


def _payload_for(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """Wrap a list of records in the ThaiWater {data: [...]} envelope."""
    return {"data": list(records)}


def _record(
    *,
    code: str,
    value: float,
    observed_str: str = OBSERVED_BANGKOK_STR,
) -> dict[str, object]:
    return {
        "station": {"tele_station_oldcode": code},
        "waterlevel_msl": value,
        "waterlevel_datetime": observed_str,
    }


def _stub_transport(payload: object, *, status_code: int = 200) -> httpx.MockTransport:
    """Build an ``httpx.MockTransport`` that replies with ``payload``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _build_client(
    payload: object,
    *,
    status_code: int = 200,
    max_age: timedelta = timedelta(hours=3),
    seeds: Sequence[object] | None = None,
) -> tuple[ThaiwaterStationClient, httpx.AsyncClient]:
    transport = _stub_transport(payload, status_code=status_code)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://thaiwater.test")
    client = ThaiwaterStationClient(
        http_client=http_client,
        base_url="https://thaiwater.test/api",
        seeds=tuple(seeds) if seeds is not None else PHASE1_STATION_SEEDS,
        max_age=max_age,
    )
    return client, http_client


# ---------------------------------------------------------------------------
# Client unit tests
# ---------------------------------------------------------------------------


class ThaiwaterClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_seed_stations_from_envelope_payload(self) -> None:
        payload = _payload_for(
            [
                _record(code="X.173A", value=4.10),
                _record(code="X.44", value=3.55),
                _record(code="X.174", value=2.10),
            ]
        )
        client, http = _build_client(payload)
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()

        self.assertEqual(len(observations), 3)
        ids = sorted(obs.station_id for obs in observations)
        self.assertEqual(ids, ["X.173A", "X.174", "X.44"])

        sample = next(obs for obs in observations if obs.station_id == "X.173A")
        self.assertEqual(sample.provider, THAIWATER_PROVIDER_NAME)
        self.assertEqual(sample.variable, StationVariable.WATER_LEVEL)
        self.assertEqual(sample.unit, "m")
        self.assertEqual(sample.value, 4.10)
        self.assertEqual(sample.quality_flag, StationQualityFlag.OK)
        self.assertEqual(sample.observed_at.tzinfo, UTC)
        # 13:55 Bangkok = 06:55 UTC.
        self.assertEqual(sample.observed_at.hour, 6)
        self.assertEqual(sample.observed_at.minute, 55)
        self.assertEqual(sample.station_name_en, "Ban Muang Kong, U-Tapao Canal")
        self.assertEqual(sample.canal_or_lake_en, "U-Tapao Canal")
        self.assertEqual(sample.location.coordinates, (100.5006, 6.8242))
        self.assertEqual(sample.attribution, "ThaiWater / HAII (Hydro Informatics Institute)")
        self.assertEqual(sample.license_note, "review-required")
        self.assertTrue(sample.provenance_url.startswith("https://thaiwater.test/api"))

    async def test_accepts_bare_list_response(self) -> None:
        payload = [
            _record(code="X.44", value=2.0),
        ]
        client, http = _build_client(payload)
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].station_id, "X.44")

    async def test_filters_out_stations_not_in_seed_list(self) -> None:
        payload = _payload_for(
            [
                _record(code="X.44", value=1.5),
                _record(code="X.999", value=99.0),  # Unknown station.
            ]
        )
        client, http = _build_client(payload)
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()
        ids = [obs.station_id for obs in observations]
        self.assertEqual(ids, ["X.44"])

    async def test_drops_stale_records(self) -> None:
        stale_record = _record(
            code="X.44",
            value=1.0,
            observed_str="2026-05-16 13:55:00",  # >24 h before NOW.
        )
        payload = _payload_for([stale_record])
        client, http = _build_client(payload, max_age=timedelta(hours=3))
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()
        self.assertEqual(observations, [])

    async def test_keeps_latest_when_duplicate_records(self) -> None:
        payload = _payload_for(
            [
                _record(code="X.44", value=1.0, observed_str="2026-05-17 13:00:00"),
                _record(code="X.44", value=2.5, observed_str="2026-05-17 13:55:00"),
            ]
        )
        client, http = _build_client(payload)
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()
        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].value, 2.5)

    async def test_ignores_partial_records_with_missing_fields(self) -> None:
        payload = _payload_for(
            [
                {"station": {"tele_station_oldcode": "X.44"}, "waterlevel_msl": 1.0},
                {"station": {"tele_station_oldcode": "X.173A"}},  # No value.
                _record(code="X.174", value=2.0),
            ]
        )
        client, http = _build_client(payload)
        try:
            observations = await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()
        ids = sorted(obs.station_id for obs in observations)
        self.assertEqual(ids, ["X.174"])

    async def test_raises_on_non_200_response(self) -> None:
        client, http = _build_client({"error": "bad gateway"}, status_code=502)
        try:
            with self.assertRaises(ThaiwaterIngestionError):
                await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http.aclose()

    async def test_raises_on_invalid_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="https://thaiwater.test")
        client = ThaiwaterStationClient(
            http_client=http_client,
            base_url="https://thaiwater.test/api",
        )
        try:
            with self.assertRaises(ThaiwaterIngestionError):
                await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http_client.aclose()

    async def test_authorization_header_included_when_api_key_set(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_payload_for([_record(code="X.44", value=1.0)]))

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="https://thaiwater.test")
        client = ThaiwaterStationClient(
            http_client=http_client,
            base_url="https://thaiwater.test/api",
            api_key="secret-token",
        )
        try:
            await client.fetch_latest_water_levels(now=NOW)
        finally:
            await http_client.aclose()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers["Authorization"], "Bearer secret-token")


# ---------------------------------------------------------------------------
# Service-level water-level aggregation tests
# ---------------------------------------------------------------------------


class _FakeStationClient:
    """Stub :class:`StationObservationClient` for service-level tests."""

    provider = THAIWATER_PROVIDER_NAME

    def __init__(
        self,
        observations: Iterable[StationObservation] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._observations = list(observations or ())
        self._error = error
        self.calls = 0

    async def fetch_latest_water_levels(self) -> list[StationObservation]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._observations)


def _observation_from_seed(
    *,
    station_id: str,
    value: float,
    observed_at: datetime,
    retrieved_at: datetime,
) -> StationObservation:
    seed = next(s for s in PHASE1_STATION_SEEDS if s.station_id == station_id)
    return StationObservation(
        provider=THAIWATER_PROVIDER_NAME,
        source_system="api",
        station_id=seed.station_id,
        station_name_th=seed.name_th,
        station_name_en=seed.name_en,
        canal_or_lake_th=seed.canal_or_lake_th,
        canal_or_lake_en=seed.canal_or_lake_en,
        location={"type": "Point", "coordinates": (seed.longitude, seed.latitude)},
        variable=StationVariable.WATER_LEVEL,
        value=value,
        unit="m",
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        quality_flag=StationQualityFlag.OK,
        warning_level_m=seed.warning_level_m,
        critical_level_m=seed.critical_level_m,
        provenance_url="https://thaiwater.test/api/waterlevel_load",
    )


class WaterLevelServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_real_data_with_is_mock_false_when_fresh(self) -> None:
        obs = _observation_from_seed(
            station_id="X.44",
            value=3.0,
            observed_at=NOW - timedelta(minutes=15),
            retrieved_at=NOW,
        )
        client = _FakeStationClient([obs])
        response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(len(response.stations), 1)
        self.assertFalse(response.freshness.is_mock)
        self.assertEqual(response.freshness.source, THAIWATER_PROVIDER_NAME)
        self.assertEqual(response.stations[0].station_id, "X.44")
        # value 3.0 vs warning 6.0; watch ratio 0.8 → watch 4.8 → green.
        self.assertEqual(response.stations[0].risk_level.value, "green")

    async def test_classifies_at_orange_when_above_warning(self) -> None:
        obs = _observation_from_seed(
            station_id="X.44",
            value=6.5,  # > warning 6.0, < critical 7.0
            observed_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW,
        )
        client = _FakeStationClient([obs])
        response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(response.stations[0].risk_level.value, "orange")

    async def test_classifies_at_red_when_above_critical(self) -> None:
        obs = _observation_from_seed(
            station_id="X.44",
            value=7.5,
            observed_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW,
        )
        client = _FakeStationClient([obs])
        response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(response.stations[0].risk_level.value, "red")

    async def test_marks_source_stale_when_no_fresh_records(self) -> None:
        client = _FakeStationClient([])
        response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(response.stations, [])
        self.assertFalse(response.freshness.is_mock)
        self.assertTrue(response.freshness.source.endswith(":stale"))
        self.assertIsNone(response.freshness.valid_at)

    async def test_marks_source_unavailable_when_client_errors(self) -> None:
        client = _FakeStationClient(error=ThaiwaterIngestionError("boom"))
        response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(response.stations, [])
        self.assertTrue(response.freshness.source.endswith(":unavailable"))

    async def test_persists_to_repository_when_supplied(self) -> None:
        obs = _observation_from_seed(
            station_id="X.44",
            value=3.0,
            observed_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW,
        )
        client = _FakeStationClient([obs])
        repository = DryRunStationRepository()
        await get_water_levels(
            client=client,
            repository=repository,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        stored = await repository.latest_per_station()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].station_id, "X.44")

    async def test_falls_back_to_repository_when_provider_fails(self) -> None:
        cached = _observation_from_seed(
            station_id="X.44",
            value=2.5,
            observed_at=NOW - timedelta(minutes=10),
            retrieved_at=NOW - timedelta(minutes=10),
        )
        repository = DryRunStationRepository()
        await repository.upsert_many([cached])
        client = _FakeStationClient(error=ThaiwaterIngestionError("network down"))
        response = await get_water_levels(
            client=client,
            repository=repository,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        self.assertEqual(len(response.stations), 1)
        self.assertEqual(response.freshness.source, THAIWATER_PROVIDER_NAME)


# ---------------------------------------------------------------------------
# Integration tests through the FastAPI route.
# ---------------------------------------------------------------------------


def _build_test_app(client: StationObservationClient) -> object:
    """Construct a test FastAPI app with an injected stub client."""
    settings = Settings(thaiwater_max_age_hours=3)
    app = create_app(
        settings=settings,
        station_repository=DryRunStationRepository(),
        thaiwater_client=client,
    )
    return app


class StationsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_water_level_endpoint_returns_real_data(self) -> None:
        obs = _observation_from_seed(
            station_id="X.44",
            value=4.0,
            observed_at=datetime.now(UTC) - timedelta(minutes=5),
            retrieved_at=datetime.now(UTC),
        )
        client = _FakeStationClient([obs])
        app = _build_test_app(client)
        with TestClient(app) as http:
            response = http.get("/api/stations/water-level")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["freshness"]["is_mock"], False)
        self.assertEqual(payload["freshness"]["source"], THAIWATER_PROVIDER_NAME)
        self.assertEqual(len(payload["stations"]), 1)
        self.assertEqual(payload["stations"][0]["station_id"], "X.44")

    async def test_water_level_endpoint_marks_stale_when_provider_empty(self) -> None:
        client = _FakeStationClient([])
        app = _build_test_app(client)
        with TestClient(app) as http:
            response = http.get("/api/stations/water-level")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["stations"], [])
        self.assertTrue(payload["freshness"]["source"].endswith(":stale"))
        self.assertEqual(payload["freshness"]["is_mock"], False)


# ---------------------------------------------------------------------------
# Risk-engine compatibility test: real-shaped records must be allowed to raise risk.
# ---------------------------------------------------------------------------


class RiskEngineCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_records_can_raise_risk_unlike_mock(self) -> None:
        """Real fresh station data with is_mock=False must raise the public level.

        This guards the inverse of the mock gating in risk_rules:
        ``not station.is_mock or settings.allow_mock_water_level_raise``.
        """
        from app.services.risk_rules import (
            RainfallRiskInput,
            RainfallThreshold,
            RiskRuleSettings,
            WaterLevelRiskInput,
            calculate_current_risk,
        )

        observation = _observation_from_seed(
            station_id="X.44",
            value=6.5,  # above warning 6.0
            observed_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW,
        )
        client = _FakeStationClient([observation])
        water_response = await get_water_levels(
            client=client,
            repository=None,
            max_age=timedelta(hours=3),
            now=NOW,
        )
        # Translate the public response back into risk inputs (mirrors the route).
        risk_inputs = [
            WaterLevelRiskInput(
                station_id=station.station_id,
                station_name=station.station_name.en,
                water_level_m=station.water_level_m,
                warning_level_m=station.warning_level_m,
                critical_level_m=station.critical_level_m,
                observed_at=station.observed_at,
                source=water_response.freshness.source,
                is_mock=water_response.freshness.is_mock,
            )
            for station in water_response.stations
        ]
        rainfall_inputs = [
            RainfallRiskInput(
                area_id="utapao-canal",
                area_name="U-Tapao Canal",
                rainfall_mm=5.0,
                accumulation_hours=6,
                source="phase-1-normalized-mock",
                model_run_time=NOW - timedelta(hours=1),
                valid_time=NOW + timedelta(hours=6),
                retrieved_at=NOW - timedelta(hours=1),
                is_mock=True,
            )
        ]
        settings = RiskRuleSettings(
            rainfall_thresholds={
                6: RainfallThreshold(window_hours=6, yellow_mm=60, orange_mm=100, red_mm=150)
            },
            fresh_run_max_age_hours=12,
            aging_run_max_age_hours=18,
            expected_forecast_cells=1,
            expected_water_stations=1,
            minimum_coverage_ratio=0.6,
            water_watch_ratio=0.8,
            water_station_max_age_hours=3,
            allow_mock_water_level_raise=False,
        )
        result = calculate_current_risk(
            forecasts=rainfall_inputs,
            water_levels=risk_inputs,
            settings=settings,
            generated_at=NOW,
        )
        # Rainfall is below yellow; station is above warning. Real data must raise to orange.
        self.assertEqual(result.computed_level.value, "orange")
        self.assertEqual(result.level.value, "orange")


# Suppress unused-import linter warnings for re-exported symbols used only by hints.
_ = (json, replace)


if __name__ == "__main__":
    unittest.main()
