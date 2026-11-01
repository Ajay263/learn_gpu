# FastAPI application

Code: `services/app/src/newsintel/api/` — `main.py` (app factory, middleware, health, metrics),
`routes_user.py` (user API), `routes_internal.py` (NiFi callback), `auth.py`, `media_tokens.py`, `services.py`.
Interactive docs (non-production only): `http://localhost:8000/api/docs`.

## Endpoints

| Method & path | Role | Purpose |
|---|---|---|
| `GET /healthz` | — | liveness (process up) |
| `GET /readyz` | — | readiness (Pixeltable, RabbitMQ, MinIO reachable) — 503 if degraded |
| `GET /metrics` | — | Prometheus metrics (keep on the internal network) |
| `GET /api/v1/me` | any | username, roles, collections from the token |
| `GET /api/v1/videos` | viewer | videos in the caller's collections |
| `GET /api/v1/videos/{id}` | viewer | metadata + per-stage status |
| `GET /api/v1/videos/{id}/transcript?start_s=&end_s=` | viewer | timestamped segments |
| `GET /api/v1/videos/{id}/scenes` | viewer | scenes with captions and keyframe URLs |
| `POST /api/v1/videos/{id}/playback?t=` | viewer | signed media URL ending in `#t=<seconds>` |
| `POST /api/v1/search` `{query, top_k, sources}` | viewer | ranked evidence (no LLM) |
| `POST /api/v1/ask` `{question}` | viewer | grounded answer + citations + evidence |
| `POST /api/v1/clips` `{video_id, start_s, end_s}` | analyst | cut/cached clip → signed URL |
| `GET /api/v1/jobs` | analyst | per-video stage statuses and errors |
| `POST /api/v1/videos/{id}/retry` | analyst | reset failed/skipped stages, re-enqueue |
| `GET /api/v1/system/inference` | viewer | inference gateway reachability/models |
| `GET /api/v1/audit?limit=&actor=&resource_id=` | admin | audit events |
| `GET /api/v1/media/{token}` | token | Range-capable streaming from MinIO |
| `POST /internal/ingestions` | service token | NiFi registers an ingested file |

Error codes: `401` bad/missing token · `403` role too low or bad media token · `404` unknown *or not
permitted* video · `422` invalid input (e.g. clip end before start) · `503` inference or Keycloak unavailable.

## Handler pattern

Every user handler does: **authenticate** (`current_principal`) → **authorise** (`require(role)`,
`visible_video`) → **act** (service call) → **audit**. Long or blocking work (Pixeltable, boto3, ffmpeg)
runs in plain `def` handlers, which FastAPI executes in a thread pool.

## Clips

`POST /clips` validates bounds (`clip_bounds`: start ≥ 0, end > start, clamped to duration, ≤ `MAX_CLIP_S`),
then checks `evidence/clips/<video>/<start_ms:09d>-<end_ms:09d>.mp4`. If missing, ffmpeg reads the original over an
*internal* MinIO presigned URL (HTTP range requests, so only the needed part is read), writes to
`SCRATCH_DIR`, uploads, and returns a media URL. Identical requests are served from cache (`cached: true`).
Clip generation runs inside the API request; for clips near the 300 s limit on large files this can take
tens of seconds — move to the worker queue if that becomes a problem.

## Observability

- `X-Request-ID` is accepted or generated, returned, bound to every log line, and stored in audit events.
- `newsintel_http_request_duration_seconds{method, route, status}` histogram (route template, not raw path, to keep
  label cardinality bounded).
