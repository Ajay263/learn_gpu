"""User-facing REST API (OIDC-authenticated). Every handler: authenticate → authorise → act → audit."""

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from newsintel.api.auth import Principal, current_principal, require
from newsintel.api.media_tokens import MediaTokenError
from newsintel.api.services import Services
from newsintel.clips import ClipError, make_clip
from newsintel.inference import InferenceError
from newsintel.pipeline.ops import retry_video
from newsintel.storage import ObjectRef

router = APIRouter(prefix="/api/v1")
media_router = APIRouter(prefix="/api/v1/media")


def svc(request: Request) -> Services:
    return request.app.state.services


def audit(request: Request, p: Principal, action: str, resource_id: str | None = None, outcome: str = "success",
          **detail) -> None:  # fmt: skip
    svc(request).audit.record(
        actor=p.username, actor_type=p.actor_type, action=action, resource_type="video" if resource_id else None,
        resource_id=resource_id, outcome=outcome, request_id=getattr(request.state, "request_id", None), detail=detail,
    )  # fmt: skip


def visible_video(request: Request, p: Principal, video_id: str) -> dict:
    """404 (not 403) for videos outside the user's collections: existence is not disclosed."""
    v = svc(request).repo.get_video(video_id)
    if v is None or v["collection"] not in p.collections:
        if v is not None:
            audit(request, p, "video.access", video_id, outcome="denied")
        raise HTTPException(404, "video not found")
    return v


def media_url(request: Request, p: Principal, ref: ObjectRef, content_type: str, fragment: str = "") -> str:
    token = request.app.state.media_tokens.issue(ref, p.subject, content_type)
    return f"/api/v1/media/{token}{fragment}"


def video_summary(v: dict) -> dict:
    keys = ("video_id", "title", "filename", "collection", "status", "duration_s", "width", "height", "language",
            "uploaded_by", "ingested_at", "size_bytes", "sha256")  # fmt: skip
    return {k: v.get(k) for k in keys}


# ── identity ─────────────────────────────────────────────────────────────────
@router.get("/me", tags=["identity"])
def me(p: Principal = Depends(current_principal)) -> dict:
    return {"username": p.username, "roles": sorted(p.roles), "collections": list(p.collections)}


# ── videos ───────────────────────────────────────────────────────────────────
@router.get("/videos", tags=["videos"])
def list_videos(request: Request, p: Principal = Depends(require("viewer"))) -> list[dict]:
    return [video_summary(v) for v in svc(request).repo.list_videos(p.collections)]


@router.get("/videos/{video_id}", tags=["videos"])
def get_video(video_id: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    v = visible_video(request, p, video_id)
    stages = [{k: r.get(k) for k in ("stage", "status", "attempts", "last_error", "model", "started_at", "finished_at")}
              for r in svc(request).repo.stages(video_id)]  # fmt: skip
    return {**video_summary(v), "stages": sorted(stages, key=lambda s: s["stage"])}


@router.get("/videos/{video_id}/transcript", tags=["videos"])
def transcript(video_id: str, request: Request, start_s: float | None = None, end_s: float | None = None,
               p: Principal = Depends(require("viewer"))) -> list[dict]:  # fmt: skip
    visible_video(request, p, video_id)
    return svc(request).repo.segments(video_id, start_s, end_s)


@router.get("/videos/{video_id}/scenes", tags=["videos"])
def scenes(video_id: str, request: Request, p: Principal = Depends(require("viewer"))) -> list[dict]:
    visible_video(request, p, video_id)
    out = []
    for s in svc(request).repo.scenes(video_id):
        thumb = media_url(request, p, ObjectRef.parse(s["keyframe_uri"]), "image/jpeg") if s["keyframe_uri"] else None
        out.append({**{k: v for k, v in s.items() if k != "keyframe_uri"}, "keyframe_url": thumb})
    return out


@router.post("/videos/{video_id}/playback", tags=["videos"])
def playback(
    video_id: str, request: Request, t: float = Query(0, ge=0), p: Principal = Depends(require("viewer"))
) -> dict:
    v = visible_video(request, p, video_id)
    audit(request, p, "video.play", video_id, t=t)
    url = media_url(request, p, ObjectRef.parse(v["original_uri"]), v.get("content_type") or "video/mp4", f"#t={t:.1f}")
    return {"url": url, "start_s": t, "expires_in_s": request.app.state.media_tokens.ttl_s}


# ── search / ask ─────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=10, ge=1, le=50)
    sources: list[Literal["transcript", "scene", "ocr", "image"]] | None = None


def _with_media(request: Request, p: Principal, evidence: list[dict]) -> list[dict]:
    for e in evidence:
        uri = e.pop("keyframe_uri", None)
        e["keyframe_url"] = media_url(request, p, ObjectRef.parse(uri), "image/jpeg") if uri else None
        e["open_url"] = f"/watch/{e['video_id']}?t={e['start_s']}"
    return evidence


@router.post("/search", tags=["search"])
def search(body: SearchRequest, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    try:
        res = svc(request).search.search(body.query, list(p.collections), body.top_k, set(body.sources or []) or None)
    except InferenceError as e:
        raise HTTPException(503, f"search temporarily unavailable: {e}") from e
    res["results"] = _with_media(request, p, res["results"])
    audit(request, p, "search", query=body.query, results=len(res["results"]))
    return res


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


@router.post("/ask", tags=["search"])
def ask(body: AskRequest, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    try:
        a = svc(request).search.ask(body.question, list(p.collections))
    except InferenceError as e:
        raise HTTPException(503, f"search temporarily unavailable: {e}") from e
    out = asdict(a)
    out["evidence"] = _with_media(request, p, out["evidence"])
    audit(request, p, "ask", question=body.question, sufficient=a.sufficient_evidence, citations=a.citations,
          model=a.model)  # fmt: skip
    return out


# ── clips ────────────────────────────────────────────────────────────────────
class ClipRequest(BaseModel):
    video_id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


@router.post("/clips", tags=["clips"])
def create_clip(body: ClipRequest, request: Request, p: Principal = Depends(require("analyst"))) -> dict:
    v = visible_video(request, p, body.video_id)
    s = svc(request)
    try:
        ref, created = make_clip(s.settings, s.store, v, body.start_s, body.end_s)
    except ClipError as e:
        raise HTTPException(422, str(e)) from e
    audit(request, p, "clip.create", body.video_id, start_s=body.start_s, end_s=body.end_s, cached=not created)
    return {"video_id": body.video_id, "start_s": body.start_s, "end_s": body.end_s, "cached": not created,
            "url": media_url(request, p, ref, "video/mp4")}  # fmt: skip


# ── processing status / retry ────────────────────────────────────────────────
@router.get("/jobs", tags=["processing"])
def jobs(request: Request, p: Principal = Depends(require("analyst"))) -> list[dict]:
    s = svc(request)
    out = []
    for v in s.repo.list_videos(p.collections):
        st = s.repo.stages(v["video_id"])
        errors = {r["stage"]: r["last_error"] for r in st if r["status"] in ("failed", "retrying")}
        out.append({**video_summary(v), "stages": {r["stage"]: r["status"] for r in st}, "errors": errors})
    return out


@router.post("/videos/{video_id}/retry", tags=["processing"])
def retry(video_id: str, request: Request, p: Principal = Depends(require("analyst"))) -> dict:
    visible_video(request, p, video_id)
    s = svc(request)
    reset = retry_video(s.repo, s.publisher, video_id)
    audit(request, p, "processing.retry", video_id, stages=[r.value for r in reset])
    return {"video_id": video_id, "reset_stages": [r.value for r in reset]}


# ── system / audit ───────────────────────────────────────────────────────────
@router.get("/system/inference", tags=["system"])
def inference_health(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    try:
        h = svc(request).inference.health()
        return {"reachable": True, **h.model_dump()}
    except InferenceError as e:
        return {"reachable": False, "error": str(e)[:300]}


@router.get("/audit", tags=["system"])
def audit_log(request: Request, limit: int = Query(100, le=1000), actor: str | None = None,
              resource_id: str | None = None, p: Principal = Depends(require("admin"))) -> list[dict]:  # fmt: skip
    return svc(request).audit.query(limit=limit, actor=actor, resource_id=resource_id)


# ── media streaming (token-authorised, Range-capable) ────────────────────────
@media_router.get("/{token}", tags=["media"])
def media(token: str, request: Request, range: str | None = Header(default=None)):  # noqa: A002
    try:
        ref, _subject, content_type = request.app.state.media_tokens.verify(token)
    except MediaTokenError as e:
        raise HTTPException(403, f"media link invalid: {e}") from e
    client = svc(request).store.client
    params = {"Bucket": ref.bucket, "Key": ref.key}
    if range:
        params["Range"] = range
    try:
        obj = client.get_object(**params)
    except client.exceptions.NoSuchKey as e:
        raise HTTPException(404, "media not found") from e
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(obj["ContentLength"]),
        "Cache-Control": "private, max-age=300",
    }
    if "ContentRange" in obj:
        headers["Content-Range"] = obj["ContentRange"]
    body = obj["Body"]
    if request.method == "HEAD":
        body.close()
        return Response(status_code=206 if range else 200, headers=headers, media_type=content_type)
    return StreamingResponse(body.iter_chunks(1 << 20), status_code=206 if range else 200, headers=headers,
                             media_type=content_type)  # fmt: skip
