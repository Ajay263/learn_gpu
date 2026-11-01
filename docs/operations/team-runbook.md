# Team Runbook

Each procedure is marked **[TESTED yyyy-mm-dd]** (executed successfully in GitHub Codespaces, 2 vCPU / 8 GB)
or **[UNTESTED]**. Do not remove an UNTESTED marker until someone has run the procedure and seen the
expected result.

All commands run from the repository root unless stated otherwise.

---

## 1. First-time setup [TESTED 2026-11-01]

```bash
make env          # writes .env with random secrets (mode 600); refuses to overwrite
pip install --user uv
cd services/app && ~/.local/bin/uv sync && cd -
```

Expected: `Wrote .env with random secrets (mode 600).`

Secrets live **only** in `.env` (git-ignored). To see a credential: `grep NIFI_ADMIN .env`.

## 2. Start the CPU-side infrastructure [TESTED 2026-11-01]

```bash
make up-core
```

Starts MinIO, SFTPGo, NiFi, Postgres and RabbitMQ, waits for every healthcheck, then runs the
idempotent bootstrap jobs (`minio-init`, `sftpgo-init`). Takes ~65 s warm, ~3 min on first image start.

Expected tail:
```
minio-init: buckets and service users ready
sftpgo-init: created user newsdesk      # "updated user" on later runs
```

Check:
```bash
make ps           # every long-running service shows (healthy)
```

| Service | Host address (Codespaces: forward the port, keep it **private**) | Login |
|---|---|---|
| MinIO console | http://localhost:9001 | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` |
| MinIO S3 API | http://localhost:9000 | service keys |
| SFTP | `sftp -P 2022 newsdesk@localhost` | `SFTPGO_UPLOADER_*` |
| SFTPGo admin | http://localhost:8090/web/admin | `SFTPGO_ADMIN_*` |
| NiFi | https://localhost:8443/nifi (self-signed cert) | `NIFI_ADMIN_*` |
| RabbitMQ mgmt | http://localhost:15672 | `RABBITMQ_*` |
| Postgres | localhost:5432 | per-database roles |

## 3. Verify the infrastructure [TESTED 2026-11-01]

```bash
cd services/app && .venv/bin/pytest -m integration tests/integration/test_infra.py -q
```

Expected: `7 passed`. The tests prove bucket layout, bronze versioning, least-privilege MinIO
identities, SFTP upload → `landing/uploads/<user>/`, extension blocking, Postgres DBs + pgvector.

## 4. Stop the system safely [TESTED 2026-11-01]

```bash
make down         # stops containers, KEEPS all data volumes
```

`make down-volumes` deletes all data and asks for confirmation. Never run it on a shared environment.

## 5. Restart one service [TESTED 2026-11-01]

```bash
docker compose --env-file .env -f infra/compose/docker-compose.yml restart nifi
make logs S=nifi
```

## 6. Upload a news video [TESTED 2026-11-01]

```bash
sftp -P 2022 newsdesk@localhost       # password: SFTPGO_UPLOADER_PASSWORD in .env
sftp> put my-bulletin.mp4
```

The file appears as `landing/uploads/newsdesk/my-bulletin.mp4` in MinIO. Allowed extensions:
mp4, mov, mkv, webm, m4v (MVP processes video only). Max size: `MAX_UPLOAD_BYTES` (1 GiB default).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make up-core` → `container newsintel-nifi-1 is unhealthy` with HTTP 400 inside the container | NiFi TLS cert in `nifi-conf` volume was issued for a different hostname | Compose pins `hostname: nifi`. If the volume predates that: `docker compose ... rm -sf nifi && docker volume rm newsintel_nifi-conf && make up-core` (only safe before a flow has been deployed — otherwise export the flow first). |
| `sftpgo-init` exits 127 / `sed: command not found` | Script relied on a tool missing from the image | Fixed: script uses bash regex only. |
| `up --wait` fails with `exited (0)` | Compose treats finished one-shots as failures | Init jobs run via `compose run`; use `make up-core`, not raw `up --wait`. |
| SFTP upload of `.exe` fails with "Permission denied" | Intended: extension allowlist | Rename only if the content really is an allowed type; NiFi re-checks magic bytes. |

Upload into a sub-folder named after a collection to restrict access, e.g. `put x.mp4 collection-restricted/x.mp4`
(`mkdir collection-restricted` first). Otherwise the video goes to `collection-general`.

## 7. Deploy / redeploy the NiFi ingestion flow [TESTED 2026-11-01]

Requires the API to be running (`make build && make up-app`).

```bash
make nifi-deploy       # first time; no-op if already deployed
make nifi-redeploy     # after editing infra/nifi/deploy_flow.py (drops FlowFiles queued inside NiFi)
```
Expected: `deployed 'news-ingestion' (25 processors, 39 connections) and started it`.

## 8. Verify ingestion [TESTED 2026-11-01]

```bash
make test-integration     # includes tests/integration/test_ingestion.py (3 tests)
```
Manual check after an upload (≈15–30 s; NiFi lists every 15 s):

| Where | Expected |
|---|---|
| `landing/uploads/newsdesk/<file>` | gone (consumed) |
| `bronze/videos/<sha256>/original.<ext>` | the original, byte-identical |
| `manifests/ingest/<sha256>/<ts>-<uuid>.json` | one manifest per upload event; `duplicate` true/false |
| `landing/rejected/…` + `manifests/rejected/…json` | only for rejected files, with `reject_reason` |
| RabbitMQ queue `stage.jobs` | +1 `probe` job for each *new* video |
| Audit (`audit_events` in DB `newsintel`) | `ingest.register` row |

```bash
# audit trail
docker exec -e PGPASSWORD=$(grep ^APP_DB_PASSWORD .env|cut -d= -f2) newsintel-postgres-1 \
  psql -U newsintel -d newsintel -c "SELECT ts, actor, action, resource_id, detail FROM audit_events ORDER BY id DESC LIMIT 10"
```

### Ingestion troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| File stays in `landing/uploads/` | NiFi UI → `news-ingestion` → queued FlowFiles / bulletins | API down? `curl localhost:8000/readyz`. After fixing, NiFi retries automatically (up to 120 × 30 s). If the retry budget was exhausted: `make nifi-relist`. |
| File moved to `landing/rejected/` | `manifests/rejected/*.json` → `reject_reason` | Wrong type / too large / API 4xx (see `api_response`). Re-upload a corrected file. |
| Upload duplicate | Manifest shows `"duplicate": "true"` | Expected: same bytes = same video. No reprocessing. |

## 9. Start the processing worker [TESTED 2026-11-01]

```bash
make build                 # after code changes (~25 s cached, ~7 min cold)
make up-app FAKE=1         # core + api + worker + fake inference (no GPU needed)
make up-app                # same, but using the real AI_INFERENCE_BASE_URL (Kaggle / on-prem)
```

## 10. Monitor processing [TESTED 2026-11-01]

```bash
make logs S=worker | grep -E 'stage_(done|retry_scheduled|failed)|video_settled'
```
Expected for a healthy video: `stage_done` for probe, audio, scenes, asr, captions, ocr, embed, then
`video_settled ... indexed`. Status values: `registered → processing → indexed | partial | failed`.

Per-stage detail (status, attempts, last error, model) from Python:
```bash
cd services/app && NEWSINTEL_ENV_FILE=../../.env .venv/bin/python -c "
from newsintel.config import get_settings; from newsintel.index.repository import Repository
r=Repository(get_settings())
for v in r.list_videos(limit=5): print(v['video_id'], v['status'], v['filename'])
"
```
RabbitMQ (http://localhost:15672 → Queues): `stage.jobs` = waiting work, `stage.retry.*` = waiting out an
outage, `stage.dead` = needs a human.

## 11. Verify transcription [TESTED 2026-11-01 with fake inference]

```bash
cd services/app && NEWSINTEL_ENV_FILE=../../.env .venv/bin/python -c "
from newsintel.config import get_settings; from newsintel.index.repository import Repository
r=Repository(get_settings()); vid='vid_...'
for s in r.segments(vid): print(s['start_s'], s['end_s'], s['language'], s['text'])
"
```

## 12. What happens when inference (Kaggle) goes away [TESTED 2026-11-01]

Nothing needs to be done by hand. Inference stages move to `retrying` (`last_error` contains
`InferenceUnavailable`), non-inference stages finish, and jobs wait in `stage.retry.30s → 5m → 30m`.
When the endpoint is back (`make set-llm-url` after a new Kaggle session), the next retry succeeds.
After 8 attempts (~2 h) a stage becomes `failed` and a copy goes to `stage.dead`; see section 17 to resume.

Proven by `tests/integration/test_pipeline_e2e.py::test_inference_outage_is_retried_and_recovers_without_intervention`.

## 13. Create development users and get a token [TESTED 2026-11-01]

DEVELOPMENT ONLY. Needs Keycloak running (`make up-all`, or the `auth` profile).
```bash
make dev-users      # analyst1 (analyst, collection-general), viewer2 (viewer, collection-restricted), admin1 (admin, both)
set -a; . ./.env; set +a
token() { curl -s -d grant_type=password -d client_id=newsintel-cli -d username=$1 -d "password=$DEV_USER_PASSWORD" \
  http://localhost:8081/realms/newsintel/protocol/openid-connect/token | jq -r .access_token; }
TOKEN=$(token analyst1)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/me; echo
```
Expected: `{"username":"analyst1","roles":["analyst"],"collections":["collection-general"]}`.
Tokens last 15 min; re-run `TOKEN=$(token analyst1)` when you get `401 invalid token: ExpiredSignatureError`.

## 14. Search and ask questions [TESTED 2026-11-01 with fake inference]

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"payment issue","top_k":3}' localhost:8000/api/v1/search \
  | jq -c '.results[] | {video_id, start_s, source, relevance, excerpt: .excerpt[:60]}'

curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Who announced a government policy on digital payments?"}' localhost:8000/api/v1/ask \
  | jq '{answer, sufficient_evidence, citations, quality}'
```
Expected: search results with `start_s` = the timestamp of the matching segment (10.0 for the test fixture);
`ask` returns `sufficient_evidence: true` and citations like `["E2"]` that refer to the returned `evidence`.
A question the videos cannot answer returns `"The information could not be verified from the indexed videos."`
and `sufficient_evidence: false` — this is correct behaviour, not an error.

| Symptom | Cause / fix |
|---|---|
| `503 search temporarily unavailable` | Inference gateway down → `make set-llm-url` / check Kaggle (section 12) |
| Empty `results` | Video not in your collections, or not yet `indexed` (`/api/v1/jobs`) |
| Everything "could not be verified" with real models | Reranker threshold needs tuning (`docs/ai/embeddings-and-search.md`) |

## 15. Open a video at a timestamp [TESTED 2026-11-01]

```bash
VID=$(curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/videos | jq -r '[.[]|select(.status=="indexed")][0].video_id')
URL=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" "localhost:8000/api/v1/videos/$VID/playback?t=10" | jq -r .url)
curl -s -o /dev/null -w "%{http_code} %{size_download}B\n" -H "Range: bytes=0-1023" "localhost:8000${URL%%#*}"
```
Expected: `206 1024B`. In a browser, `http://localhost:8000$URL` starts playing at 10 s (`#t=10.0`).
The link expires after `PRESIGN_TTL_S` (default 15 min) → `403 media link invalid: expired`; request a new one.

## 16. Generate a clip [TESTED 2026-11-01]

Requires role analyst.
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"video_id\":\"$VID\",\"start_s\":12,\"end_s\":18}" localhost:8000/api/v1/clips | tee clip.json | jq -c '{cached, start_s, end_s}'
curl -s -o clip.mp4 "localhost:8000$(jq -r .url clip.json)"
```
Expected: `{"cached":false,"start_s":12.0,"end_s":18.0}` the first time, `cached: true` afterwards; `clip.mp4`
is a 6 s H.264/AAC MP4. Clips are stored in the `evidence` bucket under `clips/<video_id>/`.
`422` = invalid bounds (end ≤ start, start past the end of the video, or longer than `MAX_CLIP_S`).

## 17. Troubleshoot and retry failed processing [TESTED 2026-11-01]

**Find problems** (analyst):
```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/jobs \
  | jq -c '.[] | select(.status!="indexed") | {video_id, status, errors}'
```
`partial` = searchable but some stage failed (e.g. OCR); `failed` = nothing searchable. `errors` holds the
last error per failed/retrying stage. Worker logs: `make logs S=worker | grep <video_id>`.

**Retry** after fixing the cause (e.g. inference back up). Only failed/skipped stages run again;
finished stages are kept, and ASR resumes from its last saved chunk.
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/videos/$VID/retry
# or, as an operator on the host (no token needed, recorded in the audit log):
docker exec newsintel-api-1 newsintel-admin status $VID
docker exec newsintel-api-1 newsintel-admin retry $VID --operator <your-name> [--stage ocr]
```
Expected: `{"video_id": "...", "reset_stages": ["ocr"]}` and the video returns to `indexed` once the worker
finishes. `reset_stages: []` means nothing was failed.
Proven by `tests/integration/test_api.py::test_failed_stage_is_visible_and_retried_via_api`.

**Stuck in `queued`/`retrying` for a long time** (a lost queue message, e.g. RabbitMQ data was wiped):
```bash
docker exec newsintel-api-1 newsintel-admin requeue-stuck --operator <your-name>     # default: unchanged > 45 min
```
Expected: `{"requeued": N}`. Re-publishing is safe: the worker ignores jobs for stages that already finished.
Stages in `running` are not requeued by default (RabbitMQ redelivers a dead worker's job by itself); use
`--include-running` only if a stage has been `running` far longer than the video could take.

## 18. Read the audit log [TESTED 2026-11-01]

```bash
curl -s -H "Authorization: Bearer $(token admin1)" "localhost:8000/api/v1/audit?limit=20" \
  | jq -c '.[] | {ts, actor, action, resource_id, outcome}'
```
Requires role admin (others get 403). Denied access attempts appear with `outcome: "denied"`.

## 19. Start the Kaggle GPU inference server [UNTESTED — requires Kaggle account + GPU]

Full instructions: `docs/development/kaggle-gpu-setup.md`.

Quick summary:
1. Open `notebooks/kaggle_inference_server.ipynb` in Kaggle with GPU T4 × 2
2. Add Kaggle secret `INFERENCE_API_KEY` = value from your `.env`
3. Edit Cell 3: replace `YOUR_USERNAME` with your GitHub username
4. Run all cells — Cell 4 downloads ~14 GB (first time only, ~10 min)
5. Copy the tunnel URL from Cell 6 output (e.g. `https://abc.trycloudflare.com`)
6. In Codespaces: `make set-llm-url URL=https://abc.trycloudflare.com`
7. Verify: `make check-llm` → should show `"backend": "gpu"` and all capabilities

When the Kaggle session expires (up to 12 h), stages retry automatically.
Start a new session and repeat from step 5 with the new URL.

To fall back to fake inference (e.g. for a demo without Kaggle):
```bash
make set-llm-url URL=http://inference-fake:9100
make up-app FAKE=1
```

## Pending sections (filled in as milestones complete)

Update models · update configuration · backup/restore · production deployment.
