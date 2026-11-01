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

## Pending sections (filled in as milestones complete)

Monitor processing · verify transcription · test AI inference · search · clips ·
failed jobs · update models · update configuration · backup/restore · production deployment.
