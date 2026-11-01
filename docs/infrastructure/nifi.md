# NiFi ingestion flow

Source of truth: `infra/nifi/deploy_flow.py` (flow as code). **Do not edit the flow on the canvas** —
changes are lost on the next `make nifi-redeploy`. Edit the script, redeploy, commit.

## What it does

```
ListLanding (ListS3 landing/uploads/, every 15 s, objects ≥ 5 s old)
  → CheckSize ──too_large/empty──► ReasonTooLarge ─┐
  → FetchUpload → DetectType (magic bytes) → IsVideo ──no──► ReasonType ─┤
  → Checksum (SHA-256) → ComputeKeys                                      │
  → CheckDuplicate (HEAD bronze/videos/<sha>/original.<ext>)              │
       found → MarkDuplicate ─┐                                           │
       not found → PutOriginal┴→ ManifestKey → ToJSON → PutManifest       │
  → NotifyAPI (POST /internal/ingestions, Bearer token)                   │
       2xx → DeleteLanding                                                │
       5xx / unreachable → RetryAPI (120 × 30 s penalty) → GiveUpAPI      │
       4xx → ReasonAPI ───────────────────────────────────────────────────┤
  RejectInfo → CopyToRejected (landing/rejected/) → RejectJSON → PutRejectManifest → DeleteRejected
```

Design points:

* **No partial files**: SFTPGo writes to MinIO via multipart upload; the object exists only when complete.
* **Content-addressed originals** make dedupe a single HEAD request and give stable ids.
* **Bronze is never overwritten**: duplicates skip `PutOriginal`; the NiFi MinIO identity cannot delete in bronze.
* **API is idempotent**, so a NiFi retry after a lost response cannot create a second video.
* **Landing contains only unfinished work**: success and rejection both delete the upload. `make nifi-relist`
  clears ListS3 state and re-ingests whatever remains — the recovery tool after an extended API outage.
* **Secrets** (MinIO keys, service token) are *sensitive parameters* in the `news-ingestion-params`
  Parameter Context, encrypted with `NIFI_SENSITIVE_PROPS_KEY`. Rotating a secret: update `.env`,
  `make nifi-redeploy`.

## Verified NiFi 2.12 specifics

* `AWSCredentialsProviderControllerService` treats *Access Key ID* as sensitive → its parameter must be sensitive.
* Parameter sensitivity cannot be changed in place → `--replace` recreates the parameter context.
* `CryptographicHashContent` writes `content_SHA-256`; `ListS3` writes `s3.length`.
* NiFi binds to its container hostname; the TLS cert is issued for `hostname: nifi` (see compose).

## Scaling later

For larger volumes: run NiFi clustered with ListS3 on the primary node, raise concurrent tasks on
`Checksum`/`PutOriginal`, and switch `FetchUpload` + `PutOriginal` to a streaming copy (CopyS3Object +
server-side checksum) to avoid pulling content through NiFi.
