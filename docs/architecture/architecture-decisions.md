# Architecture Decision Records

Format: Context → Decision → Consequences. Status is "Accepted" unless stated.

## ADR-001 Single shared Python package
**Context.** API, worker and MCP server need the same settings, storage client, inference client and Pixeltable schema. The reference repo duplicated config between two packages.
**Decision.** One package `newsintel` (Python 3.12, `services/app`) with entrypoints `newsintel-api`, `newsintel-worker`, `newsintel-mcp`. One container image, different commands.
**Consequences.** No drift between services; images are larger than strictly necessary (acceptable for MVP).

## ADR-002 MinIO is the system of record for media
**Context.** Pixeltable 0.7.22 has no first-class MinIO target (verified: generic `https://host/...` URLs are read-only `HTTP_STORE`; `s3://` assumes AWS).
**Decision.** Originals, audio, keyframes, clips and manifests live in MinIO. Pixeltable stores object keys, text, small thumbnails (for image-embedding indexes) and vectors.
**Spike result (2026-11-01, tested).** With boto3's standard `AWS_ENDPOINT_URL_S3=http://minio:9000`, Pixeltable *reads* `s3://bucket/key` media from MinIO (downloads to its file cache on access). Writing via `destination=` was not tested and is not used.
**Consequences.** Pixeltable media columns hold `s3://` references, never copies; its file cache is disposable. `AWS_ENDPOINT_URL_S3` is process-wide, which is fine because MinIO is the only S3 endpoint the app uses. The worker still downloads originals to scratch for ffmpeg/PySceneDetect.

## ADR-008 Pixeltable UDFs live in the `newsintel` package
**Context.** Pixeltable 0.7.22 refuses UDFs defined in a script's global namespace; it persists them by module path (verified).
**Decision.** All UDFs live in `newsintel.index.udfs`. Every process that opens the tables must have the same package version installed.
**Consequences.** Renaming/moving a UDF is a schema migration. Pin the package version per deployment.

## ADR-003 Pixeltable catalog on shared Postgres
**Context.** Default Pixeltable runs an embedded Postgres under `~/.pixeltable`, owned by one process/host. We have three processes (API, worker, MCP).
**Decision.** Set `PIXELTABLE_DB_CONNECT_STR` to the compose Postgres (pgvector image), database `pixeltable`. `PIXELTABLE_HOME` (media/file cache) is a shared volume.
**Consequences.** Postgres is justified (also needed by Keycloak and for app state). Back up Postgres + the Pixeltable home volume together.

## ADR-004 Explicit, durable processing stages instead of inline computed columns
**Context.** The tutorial puts OpenAI calls in computed columns, evaluated during `insert()`. With Kaggle offline that insert fails, and nothing retries it later.
**Decision.** The worker runs stages (`probe → audio → asr → scenes → captions → ocr → embed`) as RabbitMQ messages. Each stage is idempotent (keyed by `video_id` + stage + input checksum), records status, and is retried with backoff; exhausted messages go to a DLQ. Results are inserted into Pixeltable tables. Embedding *indexes* still use Pixeltable `add_embedding_index` with UDFs that call the inference API (needed for query-time embedding).
**Consequences.** OCR failure does not invalidate ASR. Inserting rows into indexed tables requires inference to be up — the embed stage is itself retryable.

## ADR-005 Provider-independent inference gateway
**Decision.** The app knows only `AI_INFERENCE_BASE_URL` + `AI_INFERENCE_API_KEY` and a fixed REST contract (`docs/ai/inference-services.md`). On Kaggle the gateway runs behind a Cloudflare quick tunnel; on-prem it is an internal URL.
**Consequences.** Swapping a model or server is a gateway change. A fake gateway implements the same contract for tests.

## ADR-006 RabbitMQ as the job queue
**Context.** Need durability across restarts, retries, DLQ; workload is small.
**Decision.** RabbitMQ 4.x with quorum queues, a delayed-retry queue via per-message TTL + dead-lettering, and a DLQ.
**Consequences.** One more service, but no polling loops and standard operational tooling (management UI kept internal).

## ADR-007 Keycloak for authentication
**Decision.** Keycloak (OIDC). FastAPI validates RS256 JWTs against the realm JWKS. Roles: `viewer`, `analyst`, `admin`. Videos carry a `collection`; users get collections via a group claim.
**Consequences.** Heavier than a homemade auth, but production-grade and on-prem friendly.
