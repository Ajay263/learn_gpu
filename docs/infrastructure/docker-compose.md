# Docker Compose stack

File: `infra/compose/docker-compose.yml`. Always invoke through `make` so `--env-file .env` and the
project name `newsintel` are applied.

## Services and pinned images

| Service | Image | Why this image | Memory cap |
|---|---|---|---|
| minio | `pgsty/minio:RELEASE.2026-08-04T00-00-00Z` | Official MinIO images are no longer publicly pullable (Docker Hub repo removed, quay.io requires auth — tested 2026-11-01). Pigsty's community rebuild chosen by project owner. AGPLv3. Includes `mc`. | 512 MiB |
| minio-init | same | Reuses bundled `mc`; one-shot | — |
| sftpgo | `drakkan/sftpgo:v2.7.6-alpine` | Upstream official | 256 MiB |
| sftpgo-init | minio image (bash+curl) | Avoids another image | — |
| nifi | `apache/nifi:2.12.0` | Upstream official | 1.6 GiB (heap 1 GiB) |
| postgres | `pgvector/pgvector:0.8.7-pg17-trixie` | Postgres 17 + pgvector (Pixeltable embedding indexes) | 768 MiB |
| rabbitmq | `rabbitmq:4.3.7-management-alpine` | Official; management UI for ops | 512 MiB |
| keycloak (profile `auth`) | `quay.io/keycloak/keycloak:26.8.1` | Official | 900 MiB |

## Security posture

* Every port binds to `127.0.0.1`. In Codespaces forwarded ports are private by default — keep them so.
* No credentials in the compose file: every secret is `${VAR:?}` and fails fast if missing.
* Separate MinIO identities: `svc-sftpgo` (landing/uploads only), `svc-nifi` (landing rw, bronze/manifests
  write-no-delete), `svc-app` (bronze read-only, derived/evidence rw). Policies: `infra/minio/minio-init.sh`.
* Separate Postgres roles/databases: `pixeltable`, `keycloak`, `newsintel`.
* SFTPGo: FTP and WebDAV disabled for the uploader; upload size cap; extension allowlist; no download/delete permission.

## Buckets

| Bucket | Contents | Who writes | Notes |
|---|---|---|---|
| `landing` | `uploads/<user>/…` raw uploads, `rejected/…` | SFTPGo, NiFi | transient |
| `bronze` | validated originals `videos/<sha256>/original.<ext>` | NiFi | versioned, no delete for services |
| `derived` | audio, keyframes, thumbnails | worker | regenerable |
| `manifests` | ingestion + processing manifests | NiFi, worker | versioned |
| `evidence` | generated clips | API | cacheable, can be purged |

## Why SFTPGo writes straight into MinIO

SFTPGo's S3 filesystem backend uploads with multipart upload; the object becomes visible only when
the upload completes. NiFi therefore never sees partial files — no "wait until size stops changing"
heuristics, and no shared volume between containers.
