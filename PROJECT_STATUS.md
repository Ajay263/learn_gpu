# Project Status

Last updated: 2026-11-01

Legend: ✅ done & tested · 🟡 in progress · ⬜ not started · ⚠️ blocked/limitation

## Milestones

| # | Milestone | Status | Evidence |
|---|---|---|---|
| M0 | Repo inspection, reference comparison, plan | ✅ | `docs/architecture/reference-comparison.md`, `docs/architecture/implementation-plan.md` |
| M1 | Infrastructure: MinIO, SFTPGo, NiFi, RabbitMQ, Postgres, Keycloak (Compose) | ✅ core · 🟡 Keycloak boot test deferred to M9 | `tests/integration/test_infra.py` 7 passed; `make up-core` 65 s |
| M2 | Core library: config, storage client, inference client, logging; inference contract + gateway + fake backend | ✅ | `tests/unit/test_core.py` 14 passed |
| M3 | Ingestion: SFTPGo → NiFi → MinIO, checksum, manifest, dedupe, enqueue; API callback; audit log; app image | ✅ | `test_ingestion.py` 3 passed (new / duplicate / rejected); queue + audit verified |
| M4 | Pixeltable schema, repository, stage graph | ✅ | `test_stages.py` 8 passed; `test_repository.py` 3 passed (integration) |
| M5 | Inference service (Kaggle) + tunnel + `make set-llm-url` | ⬜ | |
| M6 | Processing worker: audio, ASR, scenes, frames, captions, OCR, embeddings, retries, DLQ | ✅ (fake inference) | `test_worker.py` 9 passed; `test_pipeline_e2e.py` 2 passed incl. outage recovery |
| M7 | Search + RAG (hybrid, rerank, grounded answers) | ✅ (fake inference) | `test_search.py` unit 12 + integration 6 passed |
| M8 | MCP server + agent orchestration | ⬜ | |
| M9 | FastAPI app, auth, audit, clips, playback | ⬜ | |
| M10 | Frontend | ⬜ | |
| M11 | Observability (Prometheus/Grafana) | ⬜ | |
| M12 | E2E acceptance test, runbook, tutorials, security review | ⬜ | |

## Test results

| Date | Suite | Result |
|---|---|---|
| 2026-11-01 | `pytest -m integration tests/integration/test_infra.py` | 7 passed |
| 2026-11-01 | `pytest` (unit) | 14 passed; ruff clean |
| 2026-11-01 | unit 22 passed; integration 10 passed | after M4 |
| 2026-11-01 | unit 44 passed; `test_search.py` (integration) 6 passed | after M7 |
| 2026-11-01 | `test_pipeline_e2e.py` | 2 passed (full pipeline; inference outage → retry → indexed) |
| 2026-11-01 | `test_ingestion.py` | 3 passed (E2E SFTP→NiFi→MinIO→API→Pixeltable→RabbitMQ) |
| 2026-11-01 | Pixeltable spike: external Postgres, `s3://` MinIO read, UDF embedding index | all 3 confirmed |

## Architectural decisions

See `docs/architecture/architecture-decisions.md`.

## Known issues / risks

| Risk | Impact | Mitigation |
|---|---|---|
| Pixeltable has no first-class MinIO *write* target | Cannot use `destination=` | Worker writes derived media to MinIO; Pixeltable holds `s3://` references (read path **tested** via `AWS_ENDPOINT_URL_S3`). |
| Official MinIO images unavailable; using `pgsty/minio` (owner decision 2026-11-01) | Third-party supply chain; AGPLv3 | Pin by release tag; record digest; legal review of AGPL before production. |
| Codespace: 2 vCPU / 7 GB RAM | NiFi + Keycloak + Pixeltable concurrently may be tight | Memory limits per container; profiles to start subsets. |
| No system ffmpeg, no sudo in Codespace | Media processing | Worker runs in a container with ffmpeg; host dev uses `imageio-ffmpeg` static binary. |
| Kaggle sessions are ephemeral (≤12 h, tunnel URL changes) | Inference unavailable intermittently | Durable queue, stage-level retries with backoff, `make set-llm-url`. |
| Refusal threshold (`MIN_RERANK`) depends on reranker calibration | Unrelated questions could be answered with real models if mis-tuned | Re-tune on eval set after M5 |
| Query parsing is rule-based (language, video ids) | Dates/entities not extracted as filters | Semantic retrieval covers them; LLM-based extraction is a follow-up |
| Model licences not yet verified | Cannot download weights | M5 licence review before any download. |
