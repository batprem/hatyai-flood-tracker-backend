## Backend

FastAPI backend skeleton for the Hat Yai flood warning app.

Run locally:

```bash
uv sync
uv run uvicorn main:app --reload
```

Run the forecast ingestion POC without network access or secrets:

```bash
uv run python -m app.ingestion.forecast_cli --provider all --forecast-hours 6,12 --mongo-preview
```

The command exercises the provider-oriented flow used for HFT-5: discover a GFS or ECMWF
Open Data model run, fetch small fixture artifacts, normalize rainfall forecast frames with
explicit `windowStart`/`windowEnd` accumulation windows, and write them to a dry-run repository
that preserves the MongoDB document shape. The `--mongo-preview` output converts native Python
`datetime` values to JSON for display only; storage adapters should pass native `datetime`
objects to MongoDB time-series collections.

Useful endpoints:

- `GET /health`
- `GET /api/forecast/rainfall`
- `GET /api/forecast/frames`
- `GET /api/stations/water-level`
- `GET /api/risk/current`
- `GET /api/map/layers`

Configure CORS with `HFT_CORS_ORIGINS` as a comma-separated list. Vercel preview URLs are allowed
by default through `HFT_CORS_ORIGIN_REGEX`. Public deployments may also set `FRONTEND_ORIGIN`
(or the `HFT_`-prefixed `HFT_FRONTEND_ORIGIN`) as a comma-separated list of public frontend
origins to merge into the allow-list. If unset, only the localhost dev origins are allowed.

### `GET /api/forecast/frames`

Public, provider-agnostic forecast frames endpoint. The response shape is normalized so that
fixture-backed ingestion (HFT-5) and the real GFS ingestion (HFT-11) surface unchanged through
the same contract.

Query parameters:

| Name             | Type             | Required | Default                              | Notes                                                                |
| ---------------- | ---------------- | -------- | ------------------------------------ | -------------------------------------------------------------------- |
| `provider`       | string           | no       | (all)                                | Normalized provider id, e.g. `gfs`, `ecmwf_open_data`.               |
| `model`          | string           | no       | (all)                                | Normalized model name, e.g. `gfs`, `ifs`.                            |
| `validTimeFrom`  | ISO 8601 UTC     | no       | (none)                               | Inclusive lower bound on frame `validTime`.                          |
| `validTimeTo`    | ISO 8601 UTC     | no       | (none)                               | Inclusive upper bound on frame `validTime`.                          |
| `area`           | string           | no       | `hatyai_utapao_songkhla_phase1`      | Configured area name. Phase 1 ships with a single Hat Yai bbox.      |

Response shape:

```jsonc
{
  "freshness": {
    "status": "fresh",                   // fresh | delayed | stale | partial | failed
    "retrievedAt": "2026-05-01T04:18:30Z",
    "thresholdHours": 7,                 // configured per provider/model
    "provider": "gfs",
    "model": "gfs",
    "runTime": "2026-05-01T00:00:00Z",
    "frameCount": 2
  },
  "query": {
    "provider": "gfs",
    "model": null,
    "area": "hatyai_utapao_songkhla_phase1",
    "validTimeFrom": null,
    "validTimeTo": null
  },
  "frames": [
    {
      "frameId": "gfs:gfs:2026050100:precipitation:f006",
      "runId": "gfs:gfs:2026050100",
      "provider": "gfs",
      "model": "gfs",
      "variable": "precipitation",
      "statistic": "accumulation",
      "unit": "mm",
      "runTime": "2026-05-01T00:00:00Z",
      "validTime": "2026-05-01T06:00:00Z",
      "windowStart": "2026-05-01T00:00:00Z",
      "windowEnd": "2026-05-01T06:00:00Z",
      "accumulationHours": 6,
      "providerAccumulationSemantics": "POC treats APCP as an accumulation over the previous forecast interval; real client must preserve GRIB step range semantics.",
      "forecastHour": 6,
      "retrievedAt": "2026-05-01T04:18:30Z",
      "processedAt": "2026-05-01T04:18:30Z",
      "area": {
        "name": "hatyai_utapao_songkhla_phase1",
        "bbox": [100.15, 6.55, 100.95, 7.35],
        "crs": "EPSG:4326"
      },
      "grid": {
        "type": "regular_lat_lon",
        "resolutionDegrees": 0.25,
        "width": 2,
        "height": 2
      },
      "valuesMm": [4.0, 7.2, 9.6, 5.1],
      "source": {
        "url": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/2026050100/pgrb2.0p25.apcp/f006",
        "product": "pgrb2.0p25.apcp",
        "license": "NOAA public data; attribution and redistribution review required before production",
        "attribution": "NOAA/NCEP Global Forecast System (GFS)",
        "rawArtifactRef": "gfs/gfs/20260501/00/pgrb2.0p25.apcp/f006"
      },
      "quality": {
        "status": "normalized",
        "missingValueCount": 0,
        "min": 4.0,
        "max": 9.6
      }
    }
  ]
}
```

Example:

```bash
curl 'http://127.0.0.1:8000/api/forecast/frames?provider=gfs&validTimeFrom=2026-05-01T00:00:00Z'
```

The endpoint reads through a lifespan-managed forecast repository. Until the real Mongo client
lands, the app uses an in-memory `DryRunForecastRepository`; ingest fixture frames first via
`app.ingestion.forecast_cli` (or call `run_dry_ingestion(..., repository=...)` from a startup
job) so that `GET /api/forecast/frames` returns data.

### Tests

```bash
uv run python -m unittest discover tests
```

Integration coverage for the public frames endpoint lives in
`tests/test_forecast_frames_api.py`. The test seeds the repository through fixture-backed
ingestion and asserts the documented response shape, the freshness block (status,
`retrievedAt`, `thresholdHours`), and provenance fields.
