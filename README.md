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

### Citizen flood reports (HFT-73)

Public, privacy-minimal crowd reports of observed flooding.

- `POST /api/reports` — submit a report. Accepts either `multipart/form-data`
  (`longitude`, `latitude`, `water_depth`, optional `note`, optional `photo`
  file) or a JSON body (`{longitude, latitude, water_depth, note}`). The
  location must be inside or near the U-Tapao basin (validated against the
  committed basin polygon with a small ~0.05° buffer for urban edges).
  `water_depth` is one of `ankle | knee | waist | above_waist`. Submissions are
  rate-limited per IP (default 5/hour). A new report is `pending` and is not
  publicly visible until approved. Returns `201` with `{id, status, has_photo,
  created_at}`.
- `GET /api/reports` — list **approved** reports only, newest first (`limit`
  query param, capped). Each report carries a relative `photo_url` when a photo
  is attached. Returns `{reports: [...], count}`.
- `GET /api/reports/{id}/photo` — stream an **approved** report's photo.
- Moderation (bearer-token protected via `REPORTS_MODERATION_TOKEN`, empty token
  rejects every request like `ALERTS_TEST_TOKEN`):
  - `GET /api/reports/moderation/pending` — list pending reports.
  - `GET /api/reports/moderation/{id}/photo` — view any report's photo.
  - `POST /api/reports/moderation/{id}/approve` — approve (becomes public).
  - `POST /api/reports/moderation/{id}/reject` — reject (stays hidden).

Photos are stored in MongoDB GridFS behind a small `PhotoStorage` interface
(`app/services/photo_storage.py`) so a GCS signed-URL backend can swap in later
without touching the report logic. In dry-run mode an in-memory storage backs
the same path.

#### PDPA / privacy note

This feature is designed for Thailand's PDPA data-minimization principle.

- **Collected and stored:** approximate flooding location (lon/lat), a coarse
  body-relative water-depth category, an optional free-text note (length-capped,
  no personal data requested), an optional photo, the moderation status, and a
  UTC submission timestamp.
- **Never collected:** no name, phone number, email, account, or any other
  personal identifier. The submission form does not ask for them.
- **Photos are sanitized:** every uploaded photo is fully re-encoded with Pillow
  before storage, which strips **all** EXIF metadata — including any embedded
  **GPS coordinates** that phones add by default. The original bytes are never
  persisted.
- **Submitter IP is transient:** the IP is used only to key the per-IP rate
  limiter, and only as a **salted SHA-256 hash** (`REPORTS_IP_HASH_SALT`); it is
  never written to a report document and cannot be recovered from the stored
  rate-limit counter, which itself expires automatically via a TTL index.
- **Moderation before exposure:** reports are hidden until a human moderator
  approves them, so accidental personal content in a note or photo can be
  rejected before it is ever public.

### Tests

```bash
uv run python -m unittest discover tests
```

Integration coverage for the public frames endpoint lives in
`tests/test_forecast_frames_api.py`. The test seeds the repository through fixture-backed
ingestion and asserts the documented response shape, the freshness block (status,
`retrievedAt`, `thresholdHours`), and provenance fields.
