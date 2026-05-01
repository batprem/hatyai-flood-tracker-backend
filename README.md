## Backend

FastAPI backend skeleton for the Hat Yai flood warning app.

Run locally:

```bash
uv sync
uv run uvicorn main:app --reload
```

Useful endpoints:

- `GET /health`
- `GET /api/forecast/rainfall`
- `GET /api/stations/water-level`
- `GET /api/risk/current`
- `GET /api/map/layers`

Configure CORS with `HFT_CORS_ORIGINS` as a comma-separated list. Vercel preview URLs are allowed by default through `HFT_CORS_ORIGIN_REGEX`.
