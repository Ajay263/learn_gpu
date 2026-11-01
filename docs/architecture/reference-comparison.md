# Reference Tutorial vs. Target Architecture

Source studied: `reference/multimodal-agents-course/` ("Kubrick", ~2,000 lines of Python across
`kubrick-mcp`, `kubrick-api`, `kubrick-ui`). Pixeltable verified against the actual
`pixeltable==0.7.22` wheel (latest on PyPI at time of writing), not against blog posts.

## 1. What the tutorial does

```
UI ──► kubrick-api (FastAPI + Groq agent) ──MCP──► kubrick-mcp (FastMCP + Pixeltable)
                                                        │
                                   OpenAI APIs ◄────────┘ (transcription, captions, embeddings)
```

Per uploaded video, `VideoProcessor` creates a **new Pixeltable directory** (`cache_<4 hex>`) containing:

| Object | Kind | Columns / computed columns |
|---|---|---|
| `cache_x.table` | table | `video` (pxt.Video), `audio_extract = extract_audio(video, 'mp3')` |
| `cache_x.table_audio_chunks` | view, `AudioSplitter` iterator (10 s, 1 s overlap) | `transcription = openai.transcriptions(...)`, `chunk_text`, embedding index on `chunk_text` |
| `cache_x.table_frames` | view, `FrameIterator(num_frames=45)` | `resized_frame`, CLIP embedding index, `im_caption = openai.vision(...)`, embedding index on caption |

Retrieval (`VideoSearchEngine`) = `col.similarity(query)` + `order_by(sims)` + `limit(k)`.
Clip extraction = `ffmpeg -ss -to` re-encode into `./shared_media`. A JSON file on disk
(`registry_<timestamp>.json`) maps video name → Pixeltable table names. The agent does
route → pick tool → call MCP tool → answer, with conversation memory in a Pixeltable table.

## 2. Verified Pixeltable facts that change our design

| Question | Finding (from 0.7.22 source) | Consequence |
|---|---|---|
| Can Pixeltable call our own model endpoints? | `pixeltable.functions.openai` builds `AsyncOpenAI(base_url=...)` from config key `openai.base_url` / `OPENAI_BASE_URL`. | We *could* point Pixeltable at an OpenAI-compatible gateway on Kaggle — but see retry note below. |
| MinIO as storage? | Object-store parser recognises `s3://` (AWS, region-detected), R2/B2/Tigris/Azure by **hostname match**. A generic `https://minio:9000/...` URL is classified `HTTP_STORE` (read-only download). No first-class MinIO target. | Do **not** rely on Pixeltable `destination=` writing to MinIO. Spike planned: `s3://` + `AWS_ENDPOINT_URL_S3` env var (boto3 honours it) — unverified until tested. |
| Media storage | Pixeltable stores media file *references*; remote media are downloaded into a local file cache on access. Its catalog is an embedded Postgres (`pixeltable-pgserver`) under `PIXELTABLE_HOME`. | MinIO stays the system of record for originals. Pixeltable stores the MinIO object key + derived data. |
| Built-in scene detection | `pixeltable.functions.video.scene_detect` exists. | Option, but CLAUDE.md names PySceneDetect explicitly; we run it in the worker so it is unit-testable and independent of Pixeltable. |
| Python support | `Requires-Python >=3.11`. Codespace default is 3.14; `python3.12` is also installed. | Pin the app to Python 3.12. |
| Error handling | `add_computed_column(on_error='ignore')` stores `errormsg`/`errortype` per row instead of aborting. | Useful, but row-level errors inside Pixeltable are not a durable job queue. |

## 3. Reuse / modify / replace

| Tutorial component | Verdict | Why |
|---|---|---|
| Pixeltable table + view + iterator pattern | **Reuse (modified)** | Right abstraction. But one **global schema** (`news.videos`, `news.transcript_segments`, `news.scenes`, …) keyed by `video_id`, not one directory per video. Per-video tables make cross-video search impossible — our core use case. |
| `col.similarity()` + `order_by` retrieval | **Reuse** | Works across all rows of a global table; add `where(video_id IN allowed)` for permission filtering. |
| Embedding index via `add_embedding_index(string_embed=..., image_embed=...)` | **Reuse** with our own `@pxt.udf` embedding functions that call `AI_INFERENCE_BASE_URL`. | Keeps vectors (pgvector) inside Pixeltable; model location is config. |
| `openai.transcriptions` / `openai.vision` computed columns | **Replace** | Inline computed columns run *during insert*. If Kaggle is down, insert fails or rows get `None`. We need durable, retryable stages → the **worker** calls inference explicitly, then writes results to Pixeltable. |
| FastAPI `BackgroundTasks` + in-memory `bg_task_states` dict | **Replace** | Lost on restart, no retry. → RabbitMQ (durable queue, DLQ) + job/stage status table. |
| JSON registry files on disk | **Replace** | Not transactional, not multi-process safe. → Pixeltable `news.videos` table is the registry. |
| Local `shared_media/` upload + `/media` static serving | **Replace** | → SFTPGo → NiFi → MinIO; playback via short-lived presigned URLs after authZ check. |
| `extract_video_clip` (ffmpeg `-ss -to`) | **Reuse (modified)** | Same command; read from MinIO, write clip to `evidence/` bucket, validate times against video duration, cache by `(video_id,start,end)` hash. |
| `re_encode_video` | **Modify** | Its `finally` block always re-encodes even on success (bug). Use `ffprobe` validation in ingestion. |
| FastMCP server | **Reuse** library | Tools are redesigned: search tools return structured evidence with `video_id` + timestamps; no `process_video` tool exposed to the agent; per-call auth context + audit. |
| Groq agent + Opik prompts | **Replace** | External APIs are forbidden in production. → LLM on our inference API (OpenAI-compatible chat endpoint, served by vLLM on Kaggle). Prompts versioned in repo. |
| Agent "route → tool → answer" | **Modify** | Our flow is retrieval-first RAG: extract filters → multi-source search → rerank → grounded answer with citations; refuse when evidence is weak. |
| Memory in Pixeltable table, dropped on reset | **Modify** | Conversations per user in Postgres-backed table; not wiped globally. |
| React/Vite UI | **Reference only** | We build a smaller UI focused on search, evidence, timestamp playback and job status. |
| `CORS allow_origins=["*"]`, no auth | **Replace** | Keycloak OIDC, JWT validation in FastAPI, role checks. |

## 4. Gaps the tutorial does not cover at all

Ingestion validation, checksums, deduplication, provenance, OCR, Arabic ASR, reranking, keyword
search (BM25-style), permissions, audit log, retries/DLQ, observability, production portability.
