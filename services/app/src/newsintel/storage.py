"""Object storage access (MinIO or any S3-compatible store) through boto3.

All object keys are built here so the layout is defined in exactly one place.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from newsintel.config import Settings

CHUNK = 8 * 1024 * 1024


def sha256_stream(fp: BinaryIO) -> str:
    h = hashlib.sha256()
    for block in iter(lambda: fp.read(CHUNK), b""):
        h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    @classmethod
    def parse(cls, uri: str) -> "ObjectRef":
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// uri: {uri}")
        bucket, _, key = uri[5:].partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed s3:// uri: {uri}")
        return cls(bucket, key)


class Keys:
    """Canonical object layout. Content-addressed originals give dedupe and stable ids for free."""

    @staticmethod
    def original(sha256: str, ext: str) -> str:
        return f"videos/{sha256}/original.{ext.lower().lstrip('.')}"

    @staticmethod
    def ingest_manifest(sha256: str) -> str:
        return f"ingest/{sha256}.json"

    @staticmethod
    def processing_manifest(video_id: str) -> str:
        return f"processing/{video_id}.json"

    @staticmethod
    def audio_chunk(video_id: str, idx: int) -> str:
        return f"{video_id}/audio/chunk_{idx:04d}.wav"

    @staticmethod
    def keyframe(video_id: str, scene_idx: int) -> str:
        return f"{video_id}/keyframes/scene_{scene_idx:04d}.jpg"

    @staticmethod
    def clip(video_id: str, start_ms: int, end_ms: int) -> str:
        return f"clips/{video_id}/{start_ms:09d}-{end_ms:09d}.mp4"


class ObjectStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
            ),
        )

    @property
    def client(self):
        return self._client

    def exists(self, ref: ObjectRef) -> bool:
        try:
            self._client.head_object(Bucket=ref.bucket, Key=ref.key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def head(self, ref: ObjectRef) -> dict:
        return self._client.head_object(Bucket=ref.bucket, Key=ref.key)

    def get_bytes(self, ref: ObjectRef) -> bytes:
        return self._client.get_object(Bucket=ref.bucket, Key=ref.key)["Body"].read()

    def put_bytes(self, ref: ObjectRef, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(Bucket=ref.bucket, Key=ref.key, Body=data, ContentType=content_type)

    def upload_file(self, path: str | Path, ref: ObjectRef, content_type: str) -> None:
        self._client.upload_file(str(path), ref.bucket, ref.key, ExtraArgs={"ContentType": content_type})

    def download_file(self, ref: ObjectRef, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        self._client.download_file(ref.bucket, ref.key, str(tmp))
        os.replace(tmp, dest)  # atomic: a crash never leaves a truncated file at `dest`
        return dest

    def presigned_get(self, ref: ObjectRef, ttl_s: int | None = None, filename: str | None = None) -> str:
        params: dict = {"Bucket": ref.bucket, "Key": ref.key}
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        return self._client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl_s or self.settings.presign_ttl_s
        )
