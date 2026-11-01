"""Data-access layer over the Pixeltable tables. The only module that reads/writes them directly.

Concurrency: Pixeltable calls are synchronous. A process-wide lock serialises them, which is ample for
the MVP (one worker process, one API process). Async callers use `run_in_threadpool`.
"""

import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import numpy as np

from newsintel.config import Settings
from newsintel.index.connection import configure_pixeltable
from newsintel.logging import get_logger
from newsintel.pipeline.stages import Stage, StageStatus, VideoStatus, video_status

log = get_logger(__name__)
_LOCK = threading.RLock()


def now() -> datetime:
    return datetime.now(UTC)


def video_id_for(sha256: str) -> str:
    """Stable, content-derived id: the same bytes always map to the same video (dedupe for free)."""
    return f"vid_{sha256[:24]}"


class Repository:
    def __init__(self, settings: Settings):
        self.settings = settings
        configure_pixeltable(settings)
        from newsintel.index.schema import ensure_schema

        with _LOCK:
            self.t = ensure_schema(settings)

    # ── videos ────────────────────────────────────────────────────────────────
    def register_video(
        self,
        *,
        sha256: str,
        original_uri: str,
        collection: str,
        manifest_uri: str | None = None,
        filename: str | None = None,
        title: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
        uploaded_by: str | None = None,
    ) -> tuple[str, bool]:
        """Idempotent. Returns (video_id, created)."""
        vid = video_id_for(sha256)
        with _LOCK:
            if self.get_video(vid) is not None:
                return vid, False
            ts = now()
            self.t.videos.insert(
                [
                    {
                        "video_id": vid,
                        "sha256": sha256,
                        "original_uri": original_uri,
                        "manifest_uri": manifest_uri,
                        "filename": filename,
                        "title": title or filename,
                        "collection": collection,
                        "content_type": content_type,
                        "size_bytes": size_bytes,
                        "uploaded_by": uploaded_by,
                        "ingested_at": ts,
                        "status": VideoStatus.REGISTERED.value,
                        "updated_at": ts,
                    }
                ]
            )
            self.t.stages.insert(
                [
                    {
                        "video_id": vid,
                        "stage": s.value,
                        "status": StageStatus.PENDING.value,
                        "attempts": 0,
                        "updated_at": ts,
                    }
                    for s in Stage
                ]
            )
        log.info("video_registered", video_id=vid, collection=collection, filename=filename)
        return vid, True

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        v = self.t.videos
        with _LOCK:
            rows = list(v.where(v.video_id == video_id).collect())
        return rows[0] if rows else None

    def list_videos(self, collections: Iterable[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        v = self.t.videos
        with _LOCK:
            q = v.where(v.collection.isin(list(collections))) if collections is not None else v
            return list(q.order_by(v.ingested_at, asc=False).limit(limit).collect())

    def update_video(self, video_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        with _LOCK:
            self.t.videos.batch_update([{"video_id": video_id, **fields}])

    def collections_of(self, video_ids: Iterable[str]) -> dict[str, str]:
        ids = list(set(video_ids))
        if not ids:
            return {}
        v = self.t.videos
        with _LOCK:
            return {
                r["video_id"]: r["collection"]
                for r in v.where(v.video_id.isin(ids)).select(v.video_id, v.collection).collect()
            }

    # ── stages ────────────────────────────────────────────────────────────────
    def stages(self, video_id: str) -> list[dict[str, Any]]:
        s = self.t.stages
        with _LOCK:
            return list(s.where(s.video_id == video_id).collect())

    def stage_states(self, video_id: str) -> dict[Stage, StageStatus]:
        return {Stage(r["stage"]): StageStatus(r["status"]) for r in self.stages(video_id)}

    def set_stage(
        self,
        video_id: str,
        stage: Stage,
        status: StageStatus,
        *,
        attempts: int | None = None,
        error: str | None = None,
        model: str | None = None,
        detail: dict | None = None,
    ) -> VideoStatus:
        """Update one stage row and recompute the video's overall status. Returns the new video status."""
        ts = now()
        row: dict[str, Any] = {"video_id": video_id, "stage": stage.value, "status": status.value, "updated_at": ts}
        if attempts is not None:
            row["attempts"] = attempts
        if status == StageStatus.RUNNING:
            row["started_at"] = ts
            row["last_error"] = None
        if status in (StageStatus.DONE, StageStatus.FAILED, StageStatus.SKIPPED):
            row["finished_at"] = ts
        if error is not None:
            row["last_error"] = error[:2000]
        if model is not None:
            row["model"] = model
        if detail is not None:
            row["detail"] = detail
        with _LOCK:
            self.t.stages.batch_update([row])
            new_status = video_status(self.stage_states(video_id))
            self.update_video(video_id, status=new_status.value)
        return new_status

    def failed_stages(self, video_id: str | None = None) -> list[dict[str, Any]]:
        s = self.t.stages
        with _LOCK:
            cond = s.status == StageStatus.FAILED.value
            if video_id:
                cond = cond & (s.video_id == video_id)
            return list(s.where(cond).collect())

    # ── transcript segments ───────────────────────────────────────────────────
    def replace_segments(self, video_id: str, chunk_idx: int, rows: list[dict[str, Any]]) -> None:
        """Idempotent per audio chunk: a re-run replaces that chunk's rows instead of duplicating them."""
        seg = self.t.segments
        with _LOCK:
            seg.delete(where=(seg.video_id == video_id) & (seg.chunk_idx == chunk_idx))
            if rows:
                seg.insert(rows)

    def transcribed_chunks(self, video_id: str) -> set[int]:
        seg = self.t.segments
        with _LOCK:
            return {
                r["chunk_idx"] for r in seg.where(seg.video_id == video_id).select(seg.chunk_idx).distinct().collect()
            }

    def segments(self, video_id: str, start_s: float | None = None, end_s: float | None = None) -> list[dict[str, Any]]:
        seg = self.t.segments
        cond = seg.video_id == video_id  # Pixeltable allows one where(); combine predicates with &
        if start_s is not None:
            cond = cond & (seg.end_s >= start_s)
        if end_s is not None:
            cond = cond & (seg.start_s <= end_s)
        q = seg.where(cond)
        with _LOCK:
            return list(
                q.select(seg.segment_id, seg.start_s, seg.end_s, seg.text, seg.language, seg.confidence, seg.asr_model)
                .order_by(seg.start_s)
                .collect()
            )

    def segments_missing_embeddings(self, video_id: str) -> list[dict[str, Any]]:
        seg = self.t.segments
        with _LOCK:
            return list(
                seg.where((seg.video_id == video_id) & (seg.embedding == None))  # noqa: E711 (Pixeltable expr)
                .select(seg.segment_id, seg.text)
                .collect()
            )

    def set_segment_embeddings(self, rows: list[tuple[str, np.ndarray]], model: str) -> None:
        with _LOCK:
            self.t.segments.batch_update(
                [{"segment_id": sid, "embedding": vec, "embed_model": model} for sid, vec in rows]
            )

    # ── scenes ────────────────────────────────────────────────────────────────
    def replace_scenes(self, video_id: str, rows: list[dict[str, Any]]) -> None:
        sc = self.t.scenes
        with _LOCK:
            sc.delete(where=sc.video_id == video_id)
            if rows:
                sc.insert(rows)

    def scenes(self, video_id: str) -> list[dict[str, Any]]:
        sc = self.t.scenes
        with _LOCK:
            return list(
                sc.where(sc.video_id == video_id)
                .select(
                    sc.scene_id, sc.scene_idx, sc.start_s, sc.end_s, sc.keyframe_s, sc.keyframe_uri,
                    sc.caption, sc.caption_model, sc.ocr_text, sc.ocr_model,
                )
                .order_by(sc.scene_idx)
                .collect()
            )  # fmt: skip

    def update_scenes(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            with _LOCK:
                self.t.scenes.batch_update(rows)

    def scenes_missing(self, video_id: str, column: str, source: str | None = None) -> list[dict[str, Any]]:
        """Scenes whose `column` is NULL (optionally only where `source` column is non-empty)."""
        sc = self.t.scenes
        cond = (sc.video_id == video_id) & (getattr(sc, column) == None)  # noqa: E711
        if source:
            cond = cond & (getattr(sc, source) != None) & (getattr(sc, source) != "")  # noqa: E711
        with _LOCK:
            return list(sc.where(cond).select(sc.scene_id, sc.keyframe_uri, sc.caption, sc.ocr_text).collect())
