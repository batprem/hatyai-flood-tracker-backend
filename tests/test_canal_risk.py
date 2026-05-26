"""Tests for U-Tapao canal stage risk integration (HFT-31).

Covers the threshold classification ladder, the per-station contribution
builder, the combined ``max(rainfall, water_level)`` behavior through the
risk engine, and the GET /api/risk/current wiring including the
``degraded_inputs`` flag when no fresh station observation is available.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.ingestion.repository import DryRunForecastRepository
from app.ingestion.station_repository import DryRunStationRepository
from app.ingestion.station_thresholds import (
    STATION_THRESHOLDS_COLLECTION,
    StationThreshold,
    get_station_thresholds,
    load_station_threshold_fixture,
    seed_station_thresholds,
)
from app.ingestion.thaiwater_client import (
    StationGeoPoint,
    StationObservation,
    StationQualityFlag,
    StationVariable,
)
from app.main import create_app
from app.schemas.common import RiskLevel
from app.schemas.risk import ThresholdApplied
from app.services.risk_rules import (
    RainfallRiskInput,
    RainfallThreshold,
    RiskRuleSettings,
    WaterLevelRiskInput,
    calculate_current_risk,
)
from app.services.water_level_contribution import (
    build_water_level_contributions,
    classify_threshold,
)

UTAPAO_THRESHOLD = StationThreshold(
    station_id="X.44",
    station_name_en="U-Tapao Canal Upstream (Rattaphum)",
    station_name_th="คลองอู่ตะเภา ที่รัตภูมิ",
    watch_level_m=2.0,
    warning_level_m=3.0,
    danger_level_m=4.0,
    source="RID published alert levels",
    basin="utapao",
)


def _risk_settings() -> RiskRuleSettings:
    return RiskRuleSettings(
        rainfall_thresholds={
            6: RainfallThreshold(window_hours=6, yellow_mm=60, orange_mm=100, red_mm=150),
        },
        fresh_run_max_age_hours=12,
        aging_run_max_age_hours=18,
        expected_forecast_cells=1,
        expected_water_stations=1,
        minimum_coverage_ratio=0.6,
        water_watch_ratio=0.8,
        water_station_max_age_hours=3,
        allow_mock_water_level_raise=True,
    )


def _rainfall_input(*, rainfall_mm: float, generated_at: datetime) -> RainfallRiskInput:
    model_run_time = generated_at - timedelta(hours=2)
    return RainfallRiskInput(
        area_id="utapao-canal",
        area_name="U-Tapao Canal",
        rainfall_mm=rainfall_mm,
        accumulation_hours=6,
        source="phase-1-normalized-mock",
        model_run_time=model_run_time,
        valid_time=model_run_time + timedelta(hours=6),
        retrieved_at=model_run_time + timedelta(minutes=30),
        is_mock=False,
    )


def _water_input(*, level_m: float, generated_at: datetime) -> WaterLevelRiskInput:
    return WaterLevelRiskInput(
        station_id="X.44",
        station_name="U-Tapao Canal Upstream",
        water_level_m=level_m,
        warning_level_m=3.0,
        critical_level_m=4.0,
        observed_at=generated_at - timedelta(minutes=10),
        source="thaiwater-haii",
        is_mock=False,
    )


class ClassifyThresholdTest(unittest.TestCase):
    def test_below_watch_is_none_green(self) -> None:
        self.assertEqual(classify_threshold(1.9, UTAPAO_THRESHOLD), ThresholdApplied.NONE)

    def test_at_watch_boundary_is_watch(self) -> None:
        self.assertEqual(classify_threshold(2.0, UTAPAO_THRESHOLD), ThresholdApplied.WATCH)

    def test_at_warning_boundary_is_warning(self) -> None:
        self.assertEqual(classify_threshold(3.0, UTAPAO_THRESHOLD), ThresholdApplied.WARNING)

    def test_at_danger_boundary_is_danger(self) -> None:
        self.assertEqual(classify_threshold(4.0, UTAPAO_THRESHOLD), ThresholdApplied.DANGER)

    def test_above_danger_is_danger(self) -> None:
        self.assertEqual(classify_threshold(5.5, UTAPAO_THRESHOLD), ThresholdApplied.DANGER)


class BuildContributionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)

    def test_watch_boundary_yields_yellow(self) -> None:
        contributions = build_water_level_contributions(
            stations=[_water_input(level_m=2.0, generated_at=self.generated_at)],
            thresholds={"X.44": UTAPAO_THRESHOLD},
        )
        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0].threshold_applied, ThresholdApplied.WATCH)
        self.assertEqual(contributions[0].risk_contribution, RiskLevel.YELLOW)
        self.assertEqual(contributions[0].watch_level_m, 2.0)

    def test_warning_boundary_yields_orange(self) -> None:
        contributions = build_water_level_contributions(
            stations=[_water_input(level_m=3.0, generated_at=self.generated_at)],
            thresholds={"X.44": UTAPAO_THRESHOLD},
        )
        self.assertEqual(contributions[0].threshold_applied, ThresholdApplied.WARNING)
        self.assertEqual(contributions[0].risk_contribution, RiskLevel.ORANGE)

    def test_danger_boundary_yields_red(self) -> None:
        contributions = build_water_level_contributions(
            stations=[_water_input(level_m=4.0, generated_at=self.generated_at)],
            thresholds={"X.44": UTAPAO_THRESHOLD},
        )
        self.assertEqual(contributions[0].threshold_applied, ThresholdApplied.DANGER)
        self.assertEqual(contributions[0].risk_contribution, RiskLevel.RED)

    def test_missing_threshold_stays_green_and_nulls(self) -> None:
        contributions = build_water_level_contributions(
            stations=[_water_input(level_m=9.9, generated_at=self.generated_at)],
            thresholds={},
        )
        self.assertEqual(contributions[0].threshold_applied, ThresholdApplied.NONE)
        self.assertEqual(contributions[0].risk_contribution, RiskLevel.GREEN)
        self.assertIsNone(contributions[0].watch_level_m)
        self.assertIsNone(contributions[0].danger_level_m)


class CombinedRiskTest(unittest.TestCase):
    def test_combined_risk_is_max_of_rainfall_and_water_level(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        # Rainfall 70 mm/6h -> yellow; canal at danger (>= critical 4.0) -> red.
        response = calculate_current_risk(
            forecasts=[_rainfall_input(rainfall_mm=70, generated_at=generated_at)],
            water_levels=[_water_input(level_m=4.5, generated_at=generated_at)],
            settings=_risk_settings(),
            generated_at=generated_at,
        )
        self.assertEqual(response.computed_level, RiskLevel.RED)
        self.assertEqual(response.level, RiskLevel.RED)

    def test_rainfall_wins_when_higher_than_water_level(self) -> None:
        generated_at = datetime(2026, 5, 1, 12, tzinfo=UTC)
        # Rainfall 160 mm/6h -> red; canal below warning -> green.
        response = calculate_current_risk(
            forecasts=[_rainfall_input(rainfall_mm=160, generated_at=generated_at)],
            water_levels=[_water_input(level_m=1.0, generated_at=generated_at)],
            settings=_risk_settings(),
            generated_at=generated_at,
        )
        self.assertEqual(response.computed_level, RiskLevel.RED)


class StationThresholdSeedTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_loads_expected_stations(self) -> None:
        records = load_station_threshold_fixture()
        ids = {record.station_id for record in records}
        self.assertIn("X.20A", ids)
        self.assertIn("X.44", ids)

    async def test_seed_is_idempotent(self) -> None:
        client = AsyncMongoMockClient()
        database = client["hatyai_flood_warning"]

        first = await seed_station_thresholds(database)
        second = await seed_station_thresholds(database)
        self.assertEqual(first, second)

        count = await database[STATION_THRESHOLDS_COLLECTION].count_documents({})
        self.assertEqual(count, first)

        thresholds = await get_station_thresholds(database, station_ids=["X.44"])
        self.assertIn("X.44", thresholds)
        self.assertEqual(thresholds["X.44"].danger_level_m, 4.0)


class _StubCursor:
    """Async-iterable cursor over a fixed list of threshold documents."""

    def __init__(self, documents: list[dict[str, object]]) -> None:
        self._documents = documents

    def __aiter__(self) -> _StubCursor:
        self._iter = iter(self._documents)
        return self

    async def __anext__(self) -> dict[str, object]:
        try:
            return next(self._iter)
        except StopIteration as exc:  # noqa: B904 - translate to async stop
            raise StopAsyncIteration from exc


class _StubCollection:
    """Minimal collection exposing the ``find`` contract used by the reader."""

    def __init__(self, documents: list[dict[str, object]]) -> None:
        self._documents = documents

    def find(self, query: dict[str, object]) -> _StubCursor:
        ids = query.get("station_id", {}).get("$in") if query else None
        if ids is None:
            return _StubCursor(list(self._documents))
        return _StubCursor([doc for doc in self._documents if doc["station_id"] in ids])


class _StubThresholdDatabase:
    """Stand in for an AsyncIOMotorDatabase scoped to station thresholds."""

    def __init__(self, thresholds: list[StationThreshold]) -> None:
        self._documents = [threshold.model_dump(mode="python") for threshold in thresholds]

    def __getitem__(self, _name: str) -> _StubCollection:
        return _StubCollection(self._documents)


class _FakeStationClient:
    """Return a fixed list of observations for the risk endpoint test."""

    provider = "thaiwater-haii"

    def __init__(self, observations: list[StationObservation]) -> None:
        self._observations = observations

    async def fetch_latest_water_levels(self) -> list[StationObservation]:
        return list(self._observations)


def _observation(*, level_m: float, observed_at: datetime) -> StationObservation:
    return StationObservation(
        provider="thaiwater-haii",
        source_system="api",
        station_id="X.44",
        station_name_th="คลองอู่ตะเภา ที่รัตภูมิ",
        station_name_en="U-Tapao Canal Upstream",
        canal_or_lake_th="คลองอู่ตะเภา",
        canal_or_lake_en="U-Tapao Canal",
        location=StationGeoPoint(coordinates=(100.4708, 7.0167)),
        variable=StationVariable.WATER_LEVEL,
        value=level_m,
        unit="m",
        observed_at=observed_at,
        retrieved_at=observed_at,
        quality_flag=StationQualityFlag.OK,
        warning_level_m=3.0,
        critical_level_m=4.0,
        provenance_url="https://example.test/thaiwater",
    )


async def _seed_forecast_repository(generated_now: datetime) -> DryRunForecastRepository:
    from tests.test_risk_rules import _frame  # reuse the frame helper

    repository = DryRunForecastRepository()
    frame = _frame(
        run_time=generated_now - timedelta(hours=2),
        accumulation_hours=6,
        forecast_hour=6,
        values_mm=[10.0, 20.0, 30.0, 40.0],
    )
    await repository.upsert_frames([frame])
    return repository


class CanalRiskEndpointTest(unittest.TestCase):
    # Synchronous TestCase: the sync TestClient drives the app on its own
    # event loop. Running this through IsolatedAsyncioTestCase nests a second
    # loop that desynchronizes ``app.state`` reads in dependency resolution.

    def test_no_station_data_sets_degraded_inputs(self) -> None:
        settings = Settings(risk_allow_mock_water_level_raise=True)
        repository = asyncio.run(_seed_forecast_repository(datetime.now(UTC)))
        app = create_app(
            settings=settings,
            forecast_repository=repository,
            station_repository=DryRunStationRepository(),
            thaiwater_client=_FakeStationClient([]),
        )
        with TestClient(app) as http:
            response = http.get("/api/risk/current")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["degraded_inputs"])
        self.assertEqual(payload["water_level_contributions"], [])

    def test_fresh_station_clears_degraded_and_attaches_contribution(self) -> None:
        generated_now = datetime.now(UTC)
        settings = Settings(risk_allow_mock_water_level_raise=True)
        repository = asyncio.run(_seed_forecast_repository(generated_now))
        client = _FakeStationClient([_observation(level_m=4.5, observed_at=generated_now)])
        app = create_app(
            settings=settings,
            forecast_repository=repository,
            station_repository=DryRunStationRepository(),
            thaiwater_client=client,
        )
        # Inject a thresholds-backed database that satisfies the read contract
        # used by ``get_station_thresholds``. A direct stub keeps the test free
        # of a live MongoDB while still exercising the full
        # route -> threshold lookup -> contribution wiring.
        app.state.threshold_database = _StubThresholdDatabase([UTAPAO_THRESHOLD])

        with TestClient(app) as http:
            response = http.get("/api/risk/current")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["degraded_inputs"])
        contributions = payload["water_level_contributions"]
        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0]["station_id"], "X.44")
        self.assertEqual(contributions[0]["threshold_applied"], "danger")
        self.assertEqual(contributions[0]["risk_contribution"], "red")
        self.assertEqual(payload["computed_level"], "red")


if __name__ == "__main__":
    unittest.main()
