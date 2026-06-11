# Ops Runbook: Ingestion Pipeline Observability

Operator guide for the Hat Yai flood warning backend ingestion pipelines
(HFT-75). Covers the three monitored pipelines, how failures and staleness are
detected, how to check freshness, and how to recover.

## Monitored pipelines

| Pipeline   | Source                          | Records                          | Default staleness threshold | Env var |
|------------|---------------------------------|----------------------------------|-----------------------------|---------|
| `gfs`      | NOAA GFS via NOMADS             | Rainfall forecast runs/frames    | 6 h                         | `HFT_DATA_QUALITY_GFS_MAX_AGE_HOURS` |
| `ecmwf`    | ECMWF Open Data (IFS)           | Rainfall forecast runs/frames    | 12 h                        | `HFT_DATA_QUALITY_ECMWF_MAX_AGE_HOURS` |
| `stations` | ThaiWater / HAII public API     | Water-level station observations | 3 h                         | `HFT_DATA_QUALITY_STATION_MAX_AGE_HOURS` |

Thresholds are evaluated against the newest record's timestamp (forecast run
time, or station `observed_at`) using timezone-aware UTC.

## Detection model

Detection lives in `app/services/data_quality.py` and runs on every scheduled
ingestion tick (`app/ingestion/forecast_cli.py --mongo`) and on every
`GET /health` request. Each pipeline is classified independently:

- `fresh` — newest record age is at or below the threshold.
- `stale` — records exist but the newest one exceeds the threshold
  (`staleness_breach` ops event).
- `partial` — the latest ingestion run stored fewer frames than requested
  (`ingestion_partial` ops event).
- `failed` — the latest ingestion run failed, or no records are stored at all
  (`ingestion_failure` ops event).

`failed`/`partial` take precedence over age so an actively broken run is never
masked by a recent retrieval timestamp.

Breaching pipelines are handed to the **ops notifier interface**
(`app/services/ops_notifier.py`, `OpsNotifier` protocol). The current
implementation is `LoggingOpsNotifier`: one structured JSON `ERROR` log line
per event.

## How to check per-pipeline freshness

### 1. Health endpoint (first stop)

```bash
curl -s https://<backend-host>/health | jq .pipelines
```

Each of `gfs`, `ecmwf`, `stations` reports:

- `lastSuccessAt` — newest successfully ingested record (ISO 8601 UTC), or
  `null` when nothing usable is stored.
- `ageHours` — age of the newest record.
- `thresholdHours` — the staleness threshold in effect.
- `stale` — `true` when the pipeline breaches its threshold (status `stale`,
  `partial`, or `failed`).
- `status` / `reason` — classification plus an operator-readable explanation.

The older `dataQuality` block remains for backward compatibility.

### 2. Railway log stream

Query the structured ops alerts emitted by the scheduler:

- `event:"ops_pipeline_alert"` — one line per breaching pipeline per tick,
  with `kind` (`ingestion_failure` | `ingestion_partial` |
  `staleness_breach`), `pipeline`, `ageHours`, `thresholdHours`, `reason`,
  `detectedAt`.
- `event:"data_quality_stale_alert"` — legacy Phase 2 event name, still
  emitted by callers of `emit_stale_data_alert`.

### 3. MongoDB (deep dive)

The `forecast_runs` collection stores one document per ingestion attempt,
including failures (`status: "failed"` with `errorReason`). Station
observations live in the station time-series collection keyed by
`(station_id, observed_at)`.

## Common failure modes

| Symptom | Likely cause | First checks |
|---|---|---|
| `gfs` failed, reason mentions discovery | NOMADS outage or cycle not yet published | https://www.nco.ncep.noaa.gov/status/ ; retry next cycle |
| `ecmwf` stale ~12-20 h | Normal dissemination lag (~9-12 h after nominal run) extended by CDN delay | https://status.ecmwf.int/ ; confirm `data.ecmwf.int` reachable |
| `stations` stale or failed | ThaiWater API outage, schema change, or station offline | `curl` the ThaiWater endpoint; check ingestion logs for parse errors |
| All pipelines failed | MongoDB unreachable or scheduler job not running | Railway/Mongo Atlas status; check the cron job's last execution |
| `partial` on a forecast pipeline | Provider returned fewer forecast hours than requested | Inspect the run's `errorReason`; usually self-heals next cycle |

## How to recover

1. **Re-run ingestion manually** (idempotent; safe to repeat):

   ```bash
   # Forecast pipelines against the configured MongoDB
   uv run python -m app.ingestion.forecast_cli --provider all --mongo

   # Single provider
   uv run python -m app.ingestion.forecast_cli --provider gfs --mongo
   ```

   The CLI exits non-zero when any provider fails and always persists a
   `failed` run record so the failure stays visible.

2. **Check provider status** (links above) before escalating — most staleness
   resolves itself when the provider publishes the next cycle.

3. **Verify recovery**: `curl -s <host>/health | jq .pipelines` and confirm
   `stale: false` for the affected pipeline.

4. **Tune thresholds only deliberately** via the env vars in the table above;
   they are shared by the health endpoint and the scheduler alert.

## Where ops alerts are delivered

Today: Railway log stream only (`LoggingOpsNotifier`). Once **HFT-81** (LINE
Notification Platform epic HFT-79) lands, a LINE-backed implementation of the
same `OpsNotifier` protocol will deliver `ops_pipeline_alert` events to the
operators' LINE ops channel; detection code in `data_quality.py` does not
change. Until then, operators must watch the log stream or poll `/health`.
