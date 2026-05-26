"""Per-station flood alert thresholds backed by MongoDB.

Alert thresholds (watch / warning / danger) are slowly-changing reference
data sourced from RID/ThaiWater published alert levels, not telemetry. They
therefore live in a plain ``station_thresholds`` collection keyed on
``station_id`` rather than a time-series collection. A unique index on
``station_id`` makes the startup seed idempotent: re-running the seed replaces
each document in place instead of duplicating rows.

The seed values ship as ``backend/data/station_thresholds.json`` so operators
can adjust thresholds without redeploying code; the FastAPI lifespan upserts
the fixture at startup. The risk endpoint reads the collection to attach a
threshold-aware ``water_level_contributions`` block to the public response.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

STATION_THRESHOLDS_COLLECTION = "station_thresholds"
STATION_THRESHOLDS_FIXTURE = (
    Path(__file__).resolve().parents[2] / "data" / "station_thresholds.json"
)


class StationThreshold(BaseModel):
    """Model curated flood alert thresholds for a single station."""

    station_id: str = Field(description="Provider-stable station code, e.g. 'X.44'.")
    station_name_en: str = Field(description="English display name for the station.")
    station_name_th: str = Field(description="Thai display name for the station.")
    watch_level_m: float = Field(gt=0, description="Watch alert level in metres (yellow).")
    warning_level_m: float = Field(gt=0, description="Warning alert level in metres (orange).")
    danger_level_m: float = Field(gt=0, description="Danger alert level in metres (red).")
    source: str = Field(description="Attribution for the published alert levels.")
    basin: str = Field(description="Basin identifier, e.g. 'utapao'.")

    model_config = ConfigDict(extra="forbid")


def load_station_threshold_fixture(
    path: Path = STATION_THRESHOLDS_FIXTURE,
) -> list[StationThreshold]:
    """Load and validate station thresholds from the seed JSON fixture.

    Args:
        path: Filesystem path to the threshold fixture. Defaults to the
            packaged ``data/station_thresholds.json``.

    Returns:
        A list of validated ``StationThreshold`` records.

    Raises:
        FileNotFoundError: When the fixture file does not exist.
    """
    if not path.exists():
        msg = f"station threshold fixture not found at {path}"
        raise FileNotFoundError(msg)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [StationThreshold.model_validate(entry) for entry in raw]


async def seed_station_thresholds(
    database: AsyncIOMotorDatabase,
    *,
    thresholds: list[StationThreshold] | None = None,
) -> int:
    """Upsert station alert thresholds into MongoDB idempotently.

    Ensures a unique index on ``station_id`` and replaces each threshold
    document keyed on ``station_id`` so the seed can run on every startup
    without duplicating rows. When ``thresholds`` is omitted the packaged
    JSON fixture is loaded.

    Args:
        database: Motor database handle to write the thresholds into.
        thresholds: Threshold records to upsert. Defaults to ``None``, which
            loads the packaged fixture.

    Returns:
        The number of threshold documents upserted.
    """
    records = thresholds if thresholds is not None else load_station_threshold_fixture()
    collection = database[STATION_THRESHOLDS_COLLECTION]
    await collection.create_index(
        [("station_id", ASCENDING)],
        name="station_id_unique",
        unique=True,
    )
    for record in records:
        await collection.replace_one(
            {"station_id": record.station_id},
            record.model_dump(mode="python"),
            upsert=True,
        )
    logger.info("Seeded %d station thresholds into %s", len(records), STATION_THRESHOLDS_COLLECTION)
    return len(records)


async def get_station_thresholds(
    database: AsyncIOMotorDatabase,
    *,
    station_ids: list[str] | None = None,
) -> dict[str, StationThreshold]:
    """Return station thresholds keyed by ``station_id`` from MongoDB.

    Args:
        database: Motor database handle to read thresholds from.
        station_ids: Filter to specific stations. Defaults to ``None``, which
            returns all configured thresholds.

    Returns:
        A mapping of ``station_id`` to its ``StationThreshold`` record.
    """
    collection = database[STATION_THRESHOLDS_COLLECTION]
    query: dict[str, object] = {}
    if station_ids is not None:
        query["station_id"] = {"$in": station_ids}
    cursor = collection.find(query)
    result: dict[str, StationThreshold] = {}
    async for document in cursor:
        document.pop("_id", None)
        record = StationThreshold.model_validate(document)
        result[record.station_id] = record
    return result


__all__ = [
    "STATION_THRESHOLDS_COLLECTION",
    "STATION_THRESHOLDS_FIXTURE",
    "StationThreshold",
    "get_station_thresholds",
    "load_station_threshold_fixture",
    "seed_station_thresholds",
]
