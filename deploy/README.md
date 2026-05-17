# Cloud Run Deployment

Two components are deployed on Google Cloud Run:

## API Service

Already deployed as a Cloud Run **Service**. Managed via the Google Cloud Console or:

```bash
gcloud run deploy hatyai-flood-api \
  --image IMAGE \
  --region REGION \
  --platform managed \
  --set-env-vars HFT_FORECAST_REPOSITORY_BACKEND=mongo
```

## GFS Ingestion Cron Job

The GFS ingestion runs as a Cloud Run **Job** triggered by Cloud Scheduler at `5 0,6,12,18 * * *` UTC (5 minutes after each GFS model cycle).

### Create / update the job

```bash
gcloud run jobs replace deploy/gfs-ingest-job.yaml --region REGION
```

Or imperatively:

```bash
gcloud run jobs create gfs-ingest \
  --image IMAGE \
  --region REGION \
  --command python \
  --args "-m,app.ingestion.forecast_cli,--provider,gfs,--mongo" \
  --set-env-vars HFT_FORECAST_REPOSITORY_BACKEND=mongo \
  --set-secrets HFT_MONGODB_URI=mongodb-uri:latest \
  --max-retries 1 \
  --task-timeout 600
```

### Create the Cloud Scheduler trigger

```bash
gcloud scheduler jobs create http gfs-ingest-trigger \
  --schedule "5 0,6,12,18 * * *" \
  --time-zone "UTC" \
  --location REGION \
  --uri "https://REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/PROJECT_ID/jobs/gfs-ingest:run" \
  --message-body '{}' \
  --oauth-service-account-email SCHEDULER_SA@PROJECT_ID.iam.gserviceaccount.com
```

The scheduler service account needs the `roles/run.invoker` IAM role on the `gfs-ingest` job.

### Verify a run

```bash
gcloud run jobs execute gfs-ingest --region REGION --wait
gcloud run jobs executions list --job gfs-ingest --region REGION
```

Check `GET /api/freshness` to confirm the run wrote a `forecast_runs` row.
