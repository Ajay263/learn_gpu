#!/usr/bin/env bash
# Idempotent SFTPGo bootstrap via the REST API (https://sftpgo.stoplight.io/docs/sftpgo).
# Creates/updates the uploader account. Its filesystem is the MinIO `landing` bucket under
# uploads/<user>/ so uploads become visible to NiFi only once the S3 upload completes.
set -euo pipefail

# The image this runs in has bash + curl but no sed/jq, so parse with a bash regex.
resp=$(curl -fsS -u "$SFTPGO_ADMIN_USER:$SFTPGO_ADMIN_PASSWORD" "$SFTPGO_URL/api/v2/token")
[[ "$resp" =~ \"access_token\":\"([^\"]+)\" ]] || { echo "sftpgo-init: could not obtain admin token" >&2; exit 1; }
token="${BASH_REMATCH[1]}"

# Media + document types accepted at the edge. NiFi re-validates by content (magic bytes).
patterns='"*.mp4","*.MP4","*.mov","*.MOV","*.mkv","*.MKV","*.webm","*.WEBM","*.ts","*.TS","*.m4v","*.M4V","*.pdf","*.PDF","*.jpg","*.JPG","*.jpeg","*.JPEG","*.png","*.PNG"'

body=$(cat <<EOF
{
  "username": "$SFTPGO_UPLOADER_USER",
  "password": "$SFTPGO_UPLOADER_PASSWORD",
  "status": 1,
  "home_dir": "/srv/sftpgo/data/$SFTPGO_UPLOADER_USER",
  "permissions": {"/": ["list", "upload", "overwrite", "create_dirs"]},
  "filters": {
    "max_upload_file_size": $MAX_UPLOAD_BYTES,
    "denied_protocols": ["FTP", "DAV"],
    "file_patterns": [{"path": "/", "allowed_patterns": [$patterns], "deny_policy": 0}]
  },
  "filesystem": {
    "provider": 1,
    "s3config": {
      "bucket": "landing",
      "region": "us-east-1",
      "endpoint": "http://minio:9000",
      "access_key": "$MINIO_SFTPGO_ACCESS_KEY",
      "access_secret": {"status": "Plain", "payload": "$MINIO_SFTPGO_SECRET_KEY"},
      "key_prefix": "uploads/$SFTPGO_UPLOADER_USER/",
      "force_path_style": true
    }
  }
}
EOF
)

auth=(-H "Authorization: Bearer $token" -H "Content-Type: application/json")
code=$(curl -s -o /dev/null -w '%{http_code}' "${auth[@]}" "$SFTPGO_URL/api/v2/users/$SFTPGO_UPLOADER_USER")
if [ "$code" = "200" ]; then
  curl -fsS -X PUT "${auth[@]}" -d "$body" "$SFTPGO_URL/api/v2/users/$SFTPGO_UPLOADER_USER" >/dev/null
  echo "sftpgo-init: updated user $SFTPGO_UPLOADER_USER"
else
  curl -fsS -X POST "${auth[@]}" -d "$body" "$SFTPGO_URL/api/v2/users" >/dev/null
  echo "sftpgo-init: created user $SFTPGO_UPLOADER_USER"
fi
