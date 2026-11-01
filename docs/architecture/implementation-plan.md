# Implementation Plan

## Target topology

```
                        ┌──────────────────── Environment 1: Ingestion & object storage ───────────────────┐
 uploader ──SFTP──► SFTPGo ──(shared volume)──► NiFi: validate type/size, SHA-256, dedupe, manifest       │
                        │                         │                                                       │
                        │                         ├──PutS3Object──► MinIO  landing/ bronze/ manifests/    │
                        │                         └──POST /internal/ingestions (service token) ──┐        │
                        └────────────────────────────────────────────────────────────────────────│────────┘
┌──────────────── Environment 4: Application & orchestration ───────────────┐                    │
│ Frontend ──► FastAPI (OIDC via Keycloak, audit) ◄───────────────────────────┼────────────────────┘
│                 │      ▲                                                    │
│                 │      └── Agent (RAG orchestrator) ──MCP──► MCP server (restricted tools)
│                 ▼                                                         │
│            RabbitMQ (durable jobs, retry, DLQ) ──► Worker (ffmpeg, PySceneDetect)
└─────────────────┬────────────────────────────────────────┬────────────────┘
┌──── Env 2: Data & indexing ────┐                ┌──── Env 3: AI inference (Kaggle GPU dev / on-prem GPU prod) ─┐
│ Pixeltable (catalog in Postgres│ ◄── results ── │ Inference gateway (API key) → ASR, VLM, LLM(vLLM),           │
│ + pgvector), news.* tables     │                │ text emb, image emb, OCR, reranker                           │
└────────────────────────────────┘                └──────────── reached via AI_INFERENCE_BASE_URL ───────────────┘
```

## Key decisions (summary — full ADRs in `architecture-decisions.md`)

1. **One Python package `newsintel`** (Python 3.12) used by API, worker and MCP server → no duplicated config/clients.
2. **MinIO is the system of record** for originals and derived media. Pixeltable stores object keys, text, thumbnails and vectors.
3. **Pixeltable uses the shared Postgres** (`pgvector/pgvector` image) via `PIXELTABLE_DB_CONNECT_STR`.
4. **Inference is called by the worker, not by Pixeltable computed columns**, so every stage is retryable, idempotent and independently statused.
5. **RabbitMQ** for durable jobs: one message per (video, stage), retry queue with TTL backoff, dead-letter queue.
6. **One inference gateway** on the GPU host exposes stable REST routes (`/v1/asr`, `/v1/embed/text`, …) and proxies to model-specific servers (vLLM for LLM/VLM). The app never sees Kaggle.
7. **Keycloak** for OIDC; FastAPI validates JWTs; video-level ACL via `collection` + role.

## Milestones

| # | Deliverable | Done when |
|---|---|---|
| M1 | Compose stack: MinIO (+bucket/policy init), SFTPGo, NiFi, RabbitMQ, Postgres/pgvector, Keycloak | All healthchecks green; buckets exist; smoke script passes |
| M2 | `newsintel` core: settings, structured logging, MinIO client, inference client (retry/circuit), tests | Unit tests pass |
| M3 | Ingestion flow: NiFi flow definition + `/internal/ingestions` endpoint; checksum, dedupe, manifest | SFTP upload → object in `bronze/` + manifest + job queued |
| M4 | Pixeltable schema `news.*`, registration, status | Integration test against compose Postgres |
| M5 | Inference gateway + Kaggle notebook + Cloudflare quick tunnel + `make set-llm-url`; model licence review | `/health` reachable from Codespaces with API key |
| M6 | Worker stages: probe, audio, ASR, scenes/frames, captions, OCR, embeddings; retries & DLQ | Video processed end-to-end with fake inference (CI) and real inference (Kaggle) |
| M7 | Search: semantic + keyword over transcripts/scenes/OCR, image search, RRF fusion, rerank; grounded answer | Eval set returns expected video/timestamp |
| M8 | MCP server (8 tools max) + agent orchestrator | Tool tests incl. permission denial |
| M9 | FastAPI: auth, ACL, audit, playback URLs, clip generation, job status/retry | API tests |
| M10 | Frontend: login, upload instructions, search/chat, evidence player, jobs page | Manual E2E |
| M11 | Prometheus metrics + Grafana dashboard | Metrics scraped |
| M12 | Acceptance test, runbook, tutorials, security review | All 20 criteria checked with evidence |

## Testing strategy

* **Unit**: pure logic (validation, chunking, fusion, prompt building) — no network.
* **Integration**: against the compose stack (MinIO, RabbitMQ, Postgres/Pixeltable).
* **Fake inference server**: deterministic stub implementing the same API contract, so the full
  pipeline runs in Codespaces/CI without a GPU. Real-model runs are a separate, documented step.
