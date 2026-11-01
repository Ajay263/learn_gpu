"""Service-to-service endpoints. Authenticated with INGEST_SERVICE_TOKEN, never with user tokens."""

import hmac
from pathlib import PurePosixPath

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from newsintel.api.services import Services
from newsintel.logging import get_logger
from newsintel.pipeline.orchestrator import advance
from newsintel.storage import Keys, ObjectRef

log = get_logger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"])


def services(request: Request) -> Services:
    return request.app.state.services


def require_service_token(request: Request, authorization: str = Header(default="")) -> str:
    expected = services(request).settings.ingest_service_token.get_secret_value()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        services(request).audit.record(actor="unknown", actor_type="service", action="ingest.notify", outcome="denied")
        raise HTTPException(401, "invalid service token")
    return "nifi"


class IngestionNotice(BaseModel):
    """Posted by NiFi after a validated original is in the bronze bucket. NiFi sends strings; pydantic coerces."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bronze_bucket: str
    bronze_key: str
    landing_key: str = Field(min_length=1, max_length=1024)
    original_name: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    mime_type: str
    duplicate: bool = False
    manifest_key: str | None = None


class IngestionResult(BaseModel):
    video_id: str
    created: bool
    status: str
    collection: str


def collection_for(landing_key: str, allowed: list[str], default: str) -> str:
    """uploads/<user>/<collection>/<file> → <collection> when allowed, else default."""
    parts = PurePosixPath(landing_key).parts
    if len(parts) >= 4 and parts[0] == "uploads" and parts[2] in allowed:
        return parts[2]
    return default


def uploader_of(landing_key: str) -> str | None:
    parts = PurePosixPath(landing_key).parts
    return parts[1] if len(parts) >= 3 and parts[0] == "uploads" else None


@router.post("/ingestions", response_model=IngestionResult)
def notify_ingestion(
    notice: IngestionNotice, request: Request, actor: str = Depends(require_service_token)
) -> IngestionResult:
    svc = services(request)
    s = svc.settings
    ext = PurePosixPath(notice.original_name).suffix
    # The caller may only register objects at the canonical content-addressed location.
    if notice.bronze_bucket != s.bucket_bronze or notice.bronze_key != Keys.original(notice.sha256, ext):
        raise HTTPException(422, "bronze location does not match the canonical key for this checksum")
    if notice.size_bytes > s.max_upload_bytes:
        raise HTTPException(422, "file exceeds the configured maximum size")
    if not notice.mime_type.startswith("video/"):
        raise HTTPException(422, f"unsupported media type {notice.mime_type}")

    ref = ObjectRef(notice.bronze_bucket, notice.bronze_key)
    try:
        head = svc.store.head(ref)
    except ClientError as e:
        raise HTTPException(422, f"original not found in object storage: {e.response['Error']['Code']}") from e
    if head["ContentLength"] != notice.size_bytes:
        raise HTTPException(422, "stored object size does not match the notice")

    collection = collection_for(notice.landing_key, s.collections, s.default_collection)
    video_id, created = svc.repo.register_video(
        sha256=notice.sha256,
        original_uri=ref.uri,
        manifest_uri=f"s3://{s.bucket_manifests}/{notice.manifest_key}" if notice.manifest_key else None,
        collection=collection,
        filename=notice.original_name,
        content_type=notice.mime_type,
        size_bytes=notice.size_bytes,
        uploaded_by=uploader_of(notice.landing_key),
    )
    if created:
        advance(svc.repo, svc.publisher, video_id)
    video = svc.repo.get_video(video_id) or {}
    svc.audit.record(
        actor=actor,
        actor_type="service",
        action="ingest.register",
        resource_type="video",
        resource_id=video_id,
        detail={
            "created": created,
            "duplicate_upload": notice.duplicate or not created,
            "landing_key": notice.landing_key,
        },
    )
    log.info("ingestion_registered", video_id=video_id, created=created, collection=collection)
    return IngestionResult(
        video_id=video_id, created=created, status=video.get("status", "unknown"), collection=collection
    )
