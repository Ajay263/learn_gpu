#!/usr/bin/env bash
# Idempotent MinIO bootstrap: buckets, versioning and least-privilege service users.
# Runs inside the minio image (which ships `mc`). Safe to re-run.
set -euo pipefail

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null

# landing   : raw SFTP uploads (uploads/<user>/...) and rejected files (rejected/...)
# bronze    : validated originals, immutable by policy, versioned
# derived   : audio, keyframes, thumbnails produced by the worker
# manifests : ingestion + processing manifests (JSON)
# evidence  : generated clips served to users
for b in landing bronze derived manifests evidence; do
  mc mb --ignore-existing "local/$b"
done
mc version enable local/bronze
mc version enable local/manifests

# Keep incomplete multipart uploads from piling up in landing.
mc ilm rule add --expire-delete-marker --noncurrent-expire-days 7 local/landing >/dev/null 2>&1 || true

policy() { # name, json
  printf '%s' "$2" > "/tmp/$1.json"
  mc admin policy create local "$1" "/tmp/$1.json" >/dev/null
}

bucket_rw() { # bucket -> statement allowing list + rw objects
  cat <<EOF
{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":["arn:aws:s3:::$1"]},
{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::$1/*"]}
EOF
}
bucket_ro() {
  cat <<EOF
{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::$1"]},
{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::$1/*"]}
EOF
}
bucket_write_no_delete() {
  cat <<EOF
{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":["arn:aws:s3:::$1"]},
{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::$1/*"]}
EOF
}

# SFTPGo: only the uploads/ prefix of landing.
policy sftpgo-landing '{"Version":"2012-10-17","Statement":[
{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":["arn:aws:s3:::landing"]},
{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::landing/uploads/*"]}]}'

# NiFi: consume landing, write originals + manifests. Cannot delete originals.
policy nifi-ingest "{\"Version\":\"2012-10-17\",\"Statement\":[
$(bucket_rw landing),
$(bucket_write_no_delete bronze),
$(bucket_write_no_delete manifests)]}"

# Application (API/worker/MCP): read originals, own derived + evidence.
policy app-processing "{\"Version\":\"2012-10-17\",\"Statement\":[
$(bucket_ro bronze),
$(bucket_write_no_delete manifests),
$(bucket_rw derived),
$(bucket_rw evidence)]}"

user() { # access, secret, policy
  mc admin user add local "$1" "$2" >/dev/null
  mc admin policy attach local "$3" --user "$1" >/dev/null 2>&1 || true  # already attached → non-zero
}
user "$MINIO_SFTPGO_ACCESS_KEY" "$MINIO_SFTPGO_SECRET_KEY" sftpgo-landing
user "$MINIO_NIFI_ACCESS_KEY"   "$MINIO_NIFI_SECRET_KEY"   nifi-ingest
user "$MINIO_APP_ACCESS_KEY"    "$MINIO_APP_SECRET_KEY"    app-processing

echo "minio-init: buckets and service users ready"
mc ls local
