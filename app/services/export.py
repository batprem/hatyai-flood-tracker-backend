"""Research export rendering for CSV and GeoJSON downloads.

Supports `GET /api/export` (HFT-78). Three datasets are exportable over a
bounded, inclusive UTC calendar-date range:

- ``forecast_frames``: normalized rainfall forecast frames from the forecast
  repository. One row/feature per frame. The raw per-cell grid is **omitted**
  from exports to keep files compact; per-frame summary statistics
  (mean/min/max rainfall in mm) plus grid metadata are included instead, and
  GeoJSON geometry is the frame's Phase 1 area bounding box rendered as a
  polygon. Full grids remain available from ``GET /api/forecast/frames``.
- ``risk_history``: the curated historical Hat Yai flood event dataset
  (the same records served by ``GET /api/events/historical``), filtered by
  ``event_date``. Events are basin-wide narratives without point geometry,
  so GeoJSON features carry ``"geometry": null`` (valid per RFC 7946).
- ``station_observations``: normalized water-level/rainfall station records
  filtered by ``observed_at``. GeoJSON geometry is the station's WGS84 point.

Conventions documented in every export:

- Timestamps are UTC, ISO 8601.
- Rainfall in mm, water levels in metres, coordinates EPSG:4326 (lon, lat).
- CSV files start with ``#``-prefixed comment rows describing the dataset,
  range, timezone, and units, followed by one header row.
- GeoJSON FeatureCollections carry the same metadata as top-level foreign
  members (``dataset``, ``range``, ``timezone``, ``units``, ``notes``).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from io import StringIO

from pydantic import JsonValue

from app.data.historical_events import HistoricalEvent
from app.ingestion.models import ForecastFrame
from app.ingestion.thaiwater_client import StationObservation

MAX_EXPORT_RANGE_DAYS = 92
"""Maximum inclusive calendar-day span accepted by the export endpoint."""


class ExportDataset(StrEnum):
    """Identify an exportable research dataset."""

    FORECAST_FRAMES = "forecast_frames"
    RISK_HISTORY = "risk_history"
    STATION_OBSERVATIONS = "station_observations"


class ExportFormat(StrEnum):
    """Identify a supported export file format."""

    CSV = "csv"
    GEOJSON = "geojson"


class ExportRangeError(ValueError):
    """Raise when a requested export date range is invalid or too large."""


_DATASET_UNITS: dict[ExportDataset, str] = {
    ExportDataset.FORECAST_FRAMES: (
        "rainfall mm (accumulation over window_start..window_end); "
        "grid resolution degrees; bbox coordinates EPSG:4326 lon/lat"
    ),
    ExportDataset.RISK_HISTORY: (
        "accumulated rainfall mm over 24h/48h/72h windows; risk levels green|yellow|orange|red"
    ),
    ExportDataset.STATION_OBSERVATIONS: (
        "water_level values and thresholds in metres; rainfall values in mm; "
        "coordinates EPSG:4326 lon/lat"
    ),
}

_DATASET_NOTES: dict[ExportDataset, str] = {
    ExportDataset.FORECAST_FRAMES: (
        "Raw per-cell grid values are omitted; mean/min/max summarize each "
        "frame. Full grids: GET /api/forecast/frames. Geometry is the Phase 1 "
        "processing-area bounding box."
    ),
    ExportDataset.RISK_HISTORY: (
        "Curated historical Hat Yai flood events with rule-based risk "
        "outputs (same records as GET /api/events/historical). Events are "
        "basin-wide; no point geometry."
    ),
    ExportDataset.STATION_OBSERVATIONS: (
        "Normalized ThaiWater/HAII station records; provider raw payloads are never exported."
    ),
}


def validate_export_range(start: date, end: date) -> tuple[datetime, datetime]:
    """Validate an inclusive calendar-date range and return UTC bounds.

    Args:
        start: First UTC calendar date included in the export.
        end: Last UTC calendar date included in the export.

    Returns:
        A ``(range_start, range_end_exclusive)`` pair of timezone-aware UTC
        datetimes covering the inclusive date range as a half-open interval.

    Raises:
        ExportRangeError: When ``end`` is before ``start`` or the inclusive
            span exceeds :data:`MAX_EXPORT_RANGE_DAYS` days.
    """
    if end < start:
        msg = f"end ({end.isoformat()}) must not be before start ({start.isoformat()})"
        raise ExportRangeError(msg)
    span_days = (end - start).days + 1
    if span_days > MAX_EXPORT_RANGE_DAYS:
        msg = (
            f"date range spans {span_days} days; maximum is "
            f"{MAX_EXPORT_RANGE_DAYS} days. Request a shorter range."
        )
        raise ExportRangeError(msg)
    range_start = datetime.combine(start, time.min, tzinfo=UTC)
    range_end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return range_start, range_end_exclusive


def export_filename(
    dataset: ExportDataset, export_format: ExportFormat, start: date, end: date
) -> str:
    """Return the download filename for an export request.

    Args:
        dataset: Dataset being exported.
        export_format: File format being exported.
        start: First UTC calendar date included in the export.
        end: Last UTC calendar date included in the export.

    Returns:
        A filename following ``hft_<dataset>_<start>_<end>.<ext>``.
    """
    extension = "csv" if export_format is ExportFormat.CSV else "geojson"
    return f"hft_{dataset.value}_{start.isoformat()}_{end.isoformat()}.{extension}"


def _iso(value: datetime) -> str:
    """Render a timezone-aware datetime as a UTC ISO 8601 string."""
    return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Per-dataset row builders (shared by CSV and GeoJSON)
# ---------------------------------------------------------------------------


def _frame_properties(frame: ForecastFrame) -> dict[str, JsonValue]:
    """Build the flat exportable property mapping for one forecast frame."""
    mean_mm = sum(frame.values_mm) / len(frame.values_mm)
    west, south, east, north = frame.area.bbox
    return {
        "frame_id": frame.frame_id,
        "run_id": frame.run_id,
        "provider": frame.provider.value,
        "model": frame.model,
        "variable": frame.variable.value,
        "statistic": frame.statistic.value,
        "unit": frame.unit,
        "run_time_utc": _iso(frame.run_time),
        "valid_time_utc": _iso(frame.valid_time),
        "window_start_utc": _iso(frame.window_start),
        "window_end_utc": _iso(frame.window_end),
        "accumulation_hours": frame.accumulation_hours,
        "forecast_hour": frame.forecast_hour,
        "mean_rainfall_mm": round(mean_mm, 3),
        "min_rainfall_mm": frame.quality.minimum_mm,
        "max_rainfall_mm": frame.quality.maximum_mm,
        "grid_width": frame.grid.width,
        "grid_height": frame.grid.height,
        "grid_resolution_degrees": frame.grid.resolution_degrees,
        "area_name": frame.area.name,
        "bbox_west": west,
        "bbox_south": south,
        "bbox_east": east,
        "bbox_north": north,
        "retrieved_at_utc": _iso(frame.retrieved_at),
        "attribution": frame.source.attribution,
    }


def _frame_geometry(frame: ForecastFrame) -> dict[str, JsonValue]:
    """Build a GeoJSON Polygon ring from the frame's area bounding box."""
    west, south, east, north = frame.area.bbox
    ring: list[JsonValue] = [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _event_properties(event: HistoricalEvent) -> dict[str, JsonValue]:
    """Build the flat exportable property mapping for one historical event."""
    return {
        "event_id": event.event_id,
        "event_date": event.event_date,
        "event_name_en": event.event_name_en,
        "event_name_th": event.event_name_th,
        "accumulated_24h_mm": event.accumulated_24h_mm,
        "accumulated_48h_mm": event.accumulated_48h_mm,
        "accumulated_72h_mm": event.accumulated_72h_mm,
        "flooded": event.flooded,
        "rule_output": event.rule_output.value,
        "risk_24h": event.per_window_risk.window_24h.value,
        "risk_48h": event.per_window_risk.window_48h.value,
        "risk_72h": event.per_window_risk.window_72h.value,
        "threshold_adjustments_made": event.threshold_adjustments_made,
        "source_citation": event.source_citation,
    }


def _observation_properties(observation: StationObservation) -> dict[str, JsonValue]:
    """Build the flat exportable property mapping for one station observation."""
    longitude, latitude = observation.location.coordinates
    return {
        "provider": observation.provider,
        "source_system": observation.source_system,
        "station_id": observation.station_id,
        "station_name_en": observation.station_name_en,
        "station_name_th": observation.station_name_th,
        "canal_or_lake_en": observation.canal_or_lake_en,
        "variable": observation.variable.value,
        "value": observation.value,
        "unit": observation.unit,
        "observed_at_utc": _iso(observation.observed_at),
        "retrieved_at_utc": _iso(observation.retrieved_at),
        "quality_flag": observation.quality_flag.value,
        "warning_level_m": observation.warning_level_m,
        "critical_level_m": observation.critical_level_m,
        "longitude": longitude,
        "latitude": latitude,
        "attribution": observation.attribution,
    }


_CSV_COLUMNS: dict[ExportDataset, list[str]] = {
    ExportDataset.FORECAST_FRAMES: [
        "frame_id",
        "run_id",
        "provider",
        "model",
        "variable",
        "statistic",
        "unit",
        "run_time_utc",
        "valid_time_utc",
        "window_start_utc",
        "window_end_utc",
        "accumulation_hours",
        "forecast_hour",
        "mean_rainfall_mm",
        "min_rainfall_mm",
        "max_rainfall_mm",
        "grid_width",
        "grid_height",
        "grid_resolution_degrees",
        "area_name",
        "bbox_west",
        "bbox_south",
        "bbox_east",
        "bbox_north",
        "retrieved_at_utc",
        "attribution",
    ],
    ExportDataset.RISK_HISTORY: [
        "event_id",
        "event_date",
        "event_name_en",
        "event_name_th",
        "accumulated_24h_mm",
        "accumulated_48h_mm",
        "accumulated_72h_mm",
        "flooded",
        "rule_output",
        "risk_24h",
        "risk_48h",
        "risk_72h",
        "threshold_adjustments_made",
        "source_citation",
    ],
    ExportDataset.STATION_OBSERVATIONS: [
        "provider",
        "source_system",
        "station_id",
        "station_name_en",
        "station_name_th",
        "canal_or_lake_en",
        "variable",
        "value",
        "unit",
        "observed_at_utc",
        "retrieved_at_utc",
        "quality_flag",
        "warning_level_m",
        "critical_level_m",
        "longitude",
        "latitude",
        "attribution",
    ],
}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_csv(
    dataset: ExportDataset,
    rows: list[dict[str, JsonValue]],
    *,
    start: date,
    end: date,
    generated_at: datetime,
) -> str:
    """Render export rows as a CSV document with a commented metadata header.

    Args:
        dataset: Dataset being exported; selects the column set and unit note.
        rows: Flat property mappings, one per exported record. May be empty,
            in which case the output contains only comments and the header row.
        start: First UTC calendar date included in the export.
        end: Last UTC calendar date included in the export.
        generated_at: UTC timestamp recorded in the metadata comments.

    Returns:
        The full CSV document as a string (UTF-8 safe, ``#`` comment rows
        followed by a header row and data rows).
    """
    columns = _CSV_COLUMNS[dataset]
    buffer = StringIO()
    buffer.write("# Hat Yai flood warning research export\n")
    buffer.write(f"# dataset: {dataset.value}\n")
    buffer.write(
        f"# range: {start.isoformat()} to {end.isoformat()} (UTC calendar dates, inclusive)\n"
    )
    buffer.write("# timezone: all *_utc timestamps are UTC, ISO 8601\n")
    buffer.write(f"# units: {_DATASET_UNITS[dataset]}\n")
    buffer.write(f"# note: {_DATASET_NOTES[dataset]}\n")
    buffer.write(f"# generated_at: {_iso(generated_at)}\n")
    buffer.write(f"# record_count: {len(rows)}\n")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def render_geojson(
    dataset: ExportDataset,
    features: list[dict[str, JsonValue]],
    *,
    start: date,
    end: date,
    generated_at: datetime,
) -> str:
    """Render export features as a GeoJSON FeatureCollection document.

    Args:
        dataset: Dataset being exported; selects the unit and geometry notes.
        features: GeoJSON Feature objects, one per exported record. May be
            empty, in which case ``features`` is an empty array.
        start: First UTC calendar date included in the export.
        end: Last UTC calendar date included in the export.
        generated_at: UTC timestamp recorded in the collection metadata.

    Returns:
        The FeatureCollection serialized as a JSON string with dataset, range,
        timezone, and unit metadata as top-level foreign members.
    """
    collection: dict[str, JsonValue] = {
        "type": "FeatureCollection",
        "dataset": dataset.value,
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "note": "UTC calendar dates, inclusive",
        },
        "timezone": "All timestamps are UTC, ISO 8601",
        "units": _DATASET_UNITS[dataset],
        "notes": _DATASET_NOTES[dataset],
        "generated_at": _iso(generated_at),
        "record_count": len(features),
        "features": features,
    }
    return json.dumps(collection, ensure_ascii=False)


def _feature(
    geometry: dict[str, JsonValue] | None,
    properties: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Wrap geometry and properties into a GeoJSON Feature object."""
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def frames_to_rows(frames: list[ForecastFrame]) -> list[dict[str, JsonValue]]:
    """Convert forecast frames into flat CSV-ready export rows.

    Args:
        frames: Forecast frames within the requested valid-time range.

    Returns:
        One flat property mapping per frame.
    """
    return [_frame_properties(frame) for frame in frames]


def frames_to_features(frames: list[ForecastFrame]) -> list[dict[str, JsonValue]]:
    """Convert forecast frames into GeoJSON Features with bbox polygons.

    Args:
        frames: Forecast frames within the requested valid-time range.

    Returns:
        One Feature per frame; geometry is the Phase 1 area bounding box and
        properties exclude the redundant ``bbox_*`` columns.
    """
    features: list[dict[str, JsonValue]] = []
    for frame in frames:
        properties = _frame_properties(frame)
        for key in ("bbox_west", "bbox_south", "bbox_east", "bbox_north"):
            properties.pop(key)
        features.append(_feature(_frame_geometry(frame), properties))
    return features


def filter_events(
    events: list[HistoricalEvent], *, start: date, end: date
) -> list[HistoricalEvent]:
    """Return historical events whose event date falls within the range.

    Args:
        events: Candidate historical flood events.
        start: First UTC calendar date included in the export.
        end: Last UTC calendar date included in the export.

    Returns:
        Events with ``event_date`` between ``start`` and ``end`` inclusive,
        sorted by event date.
    """
    selected = [event for event in events if start <= date.fromisoformat(event.event_date) <= end]
    selected.sort(key=lambda event: event.event_date)
    return selected


def events_to_rows(events: list[HistoricalEvent]) -> list[dict[str, JsonValue]]:
    """Convert historical events into flat CSV-ready export rows.

    Args:
        events: Historical flood events within the requested date range.

    Returns:
        One flat property mapping per event.
    """
    return [_event_properties(event) for event in events]


def events_to_features(events: list[HistoricalEvent]) -> list[dict[str, JsonValue]]:
    """Convert historical events into GeoJSON Features with null geometry.

    Events describe basin-wide flooding without a representative point, so
    each Feature carries ``"geometry": null`` as permitted by RFC 7946.

    Args:
        events: Historical flood events within the requested date range.

    Returns:
        One Feature per event with null geometry.
    """
    return [_feature(None, _event_properties(event)) for event in events]


def observations_to_rows(
    observations: list[StationObservation],
) -> list[dict[str, JsonValue]]:
    """Convert station observations into flat CSV-ready export rows.

    Args:
        observations: Station observations within the requested range.

    Returns:
        One flat property mapping per observation.
    """
    return [_observation_properties(observation) for observation in observations]


def observations_to_features(
    observations: list[StationObservation],
) -> list[dict[str, JsonValue]]:
    """Convert station observations into GeoJSON Point Features.

    Args:
        observations: Station observations within the requested range.

    Returns:
        One Feature per observation; geometry is the station's WGS84 point
        and properties exclude the redundant ``longitude``/``latitude``
        columns.
    """
    features: list[dict[str, JsonValue]] = []
    for observation in observations:
        properties = _observation_properties(observation)
        properties.pop("longitude")
        properties.pop("latitude")
        longitude, latitude = observation.location.coordinates
        geometry: dict[str, JsonValue] = {
            "type": "Point",
            "coordinates": [longitude, latitude],
        }
        features.append(_feature(geometry, properties))
    return features
