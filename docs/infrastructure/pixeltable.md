# Pixeltable (Environment 2: data & indexing)

Version pinned: **pixeltable 0.7.22**. Code: `services/app/src/newsintel/index/`.

## Role

Pixeltable is the multimodal index: the registry of videos, per-stage processing state with model
provenance, transcript segments, scenes (keyframe reference, caption, OCR) and all vector indexes.
MinIO remains the system of record for media bytes.

## Tables (directory `news`, configurable with `PIXELTABLE_DIR`)

| Table | Primary key | Purpose | Vector indexes (pgvector, cosine) |
|---|---|---|---|
| `news.videos` | `video_id` | Registry: checksum, MinIO URI, collection (ACL), media metadata, overall status | — |
| `news.video_stages` | `(video_id, stage)` | One row per stage: status, attempts, last error, model@revision, timings | — |
| `news.transcript_segments` | `segment_id` | ASR segments with start/end seconds, language, confidence, normalised text | `embedding` |
| `news.scenes` | `scene_id` | Scenes: time range, keyframe `s3://` URI, caption, OCR text/lines | `caption_embedding`, `ocr_embedding`, `image_embedding` |

`video_id = "vid_" + sha256[:24]` — content-derived, so re-uploading identical bytes cannot create a
second video.

## Behaviours verified against 0.7.22 (2026-11-01)

| Behaviour | Consequence for us |
|---|---|
| `PIXELTABLE_DB_CONNECT_STR` points Pixeltable at an external Postgres | API, worker and MCP share one catalog (ADR-003) |
| `s3://bucket/key` media are readable from MinIO when `AWS_ENDPOINT_URL_S3` is set | No duplication of originals |
| Inserting into a `pxt.Image` column **downloads** the object, even with `media_validation='on_read'` | We store URIs as `String`, not media columns |
| Bare column types are **NOT NULL**; `pxt.String \| None` makes a column nullable | All optional columns are declared `\| None` |
| `add_embedding_index` works on precomputed `Array` columns; query with `col.similarity(vector=v)` | No model calls during insert; inference outages never block writes (ADR-004) |
| UDFs must be defined in an importable module, not a script | ADR-008 |
| `primary_key=` + `batch_update(rows, if_not_exists='insert')` = upsert | Idempotent stages |
| Only **one** `.where()` per query; combine predicates with `&` | Repository builds a single predicate |

## Idempotency rules

* `register_video` returns the existing id if the sha256 is already registered.
* `replace_segments(video_id, chunk_idx, rows)` deletes then inserts one audio chunk's rows.
* `replace_scenes(video_id, rows)` replaces all scenes of a video (scene ids are positional).
* Enrichment stages only fill NULL columns (`scenes_missing`, `segments_missing_embeddings`).

## Operating it

```bash
# Python shell with the platform's Pixeltable configuration
cd services/app && NEWSINTEL_ENV_FILE=../../.env .venv/bin/python
>>> from newsintel.config import get_settings; from newsintel.index.repository import Repository
>>> r = Repository(get_settings())
>>> r.list_videos()[:3]
>>> r.t.segments.where(r.t.segments.video_id == 'vid_...').select(r.t.segments.start_s, r.t.segments.text).collect()
```

Backup = Postgres database `pixeltable` (`pg_dump`). `PIXELTABLE_HOME` is a disposable cache.

## Concurrency

Pixeltable calls are synchronous; the repository serialises them with a process-wide lock. Async
code (FastAPI, MCP) calls the repository via a thread pool. Adequate for the MVP's one API + one worker.
