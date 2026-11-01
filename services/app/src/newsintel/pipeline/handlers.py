"""Stage implementations. Each handler is idempotent and returns provenance for the stage row.

Error contract (interpreted by the worker loop):
  InferenceUnavailable, S3/network errors, TimeoutError → transient: retry later
  InferenceRejected, MediaError, PermanentStageError    → permanent: fail the stage now
"""

import io
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from newsintel import media
from newsintel.config import Settings
from newsintel.index.repository import Repository
from newsintel.inference import InferenceClient
from newsintel.logging import get_logger
from newsintel.pipeline.stages import Stage
from newsintel.storage import Keys, ObjectRef, ObjectStore
from newsintel.textnorm import normalize

log = get_logger(__name__)
OCR_MIN_CONFIDENCE = 0.5


class PermanentStageError(RuntimeError):
    """Input can never succeed (e.g. video missing from registry)."""


@dataclass
class StageOutcome:
    model: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    settings: Settings
    repo: Repository
    store: ObjectStore
    inference: InferenceClient

    def scratch(self, video_id: str) -> Path:
        p = Path(self.settings.scratch_dir) / video_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def local_original(self, video: dict) -> Path:
        """Download the original once per worker host; reused by probe/audio/scenes."""
        ref = ObjectRef.parse(video["original_uri"])
        dest = self.scratch(video["video_id"]) / f"original{Path(ref.key).suffix}"
        if not dest.exists():
            self.store.download_file(ref, dest)
        return dest

    def cleanup(self, video_id: str) -> None:
        shutil.rmtree(Path(self.settings.scratch_dir) / video_id, ignore_errors=True)

    def video(self, video_id: str) -> dict:
        v = self.repo.get_video(video_id)
        if v is None:
            raise PermanentStageError(f"{video_id} is not registered")
        return v

    def keyframe(self, uri: str) -> Image.Image:
        return Image.open(io.BytesIO(self.store.get_bytes(ObjectRef.parse(uri)))).convert("RGB")


def _model(ref) -> str:
    return f"{ref.name}@{ref.revision}"


# ── probe ────────────────────────────────────────────────────────────────────
def run_probe(ctx: Context, video_id: str) -> StageOutcome:
    video = ctx.video(video_id)
    info = media.probe(ctx.local_original(video))
    ctx.repo.update_video(video_id, duration_s=info.duration_s, width=info.width, height=info.height, fps=info.fps)
    return StageOutcome(model="ffprobe", detail=info.__dict__)


# ── audio ────────────────────────────────────────────────────────────────────
def run_audio(ctx: Context, video_id: str) -> StageOutcome:
    video = ctx.video(video_id)
    src = ctx.local_original(video)
    info = media.probe(src)
    if not info.has_audio:
        return StageOutcome(model="ffmpeg", detail={"chunks": 0, "no_audio": True})
    chunk_s = ctx.settings.audio_chunk_s
    chunks = media.extract_audio_chunks(src, ctx.scratch(video_id) / "audio", chunk_s, info.duration_s)
    for i, path in enumerate(chunks):
        ctx.store.upload_file(path, ObjectRef(ctx.settings.bucket_derived, Keys.audio_chunk(video_id, i)), "audio/wav")
    return StageOutcome(model="ffmpeg", detail={"chunks": len(chunks), "chunk_s": chunk_s, "sample_rate": 16000})


# ── asr ──────────────────────────────────────────────────────────────────────
def offset_segments(segments, chunk_idx: int, chunk_s: int, video_id: str, language: str, model: str) -> list[dict]:
    """Chunk-relative ASR timestamps → absolute video timestamps, as transcript_segments rows."""
    base = chunk_idx * chunk_s
    rows = []
    for i, seg in enumerate(segments):
        text = seg.text.strip()
        if not text:
            continue
        rows.append(
            {
                "segment_id": f"{video_id}:c{chunk_idx:04d}:s{i:04d}",
                "video_id": video_id,
                "chunk_idx": chunk_idx,
                "start_s": round(base + seg.start, 3),
                "end_s": round(base + max(seg.end, seg.start), 3),
                "text": text,
                "text_norm": normalize(text),
                "language": seg.language or language,
                "confidence": seg.confidence,
                "asr_model": model,
            }
        )
    return rows


def run_asr(ctx: Context, video_id: str) -> StageOutcome:
    stage_rows = {r["stage"]: r for r in ctx.repo.stages(video_id)}
    audio_detail = stage_rows.get(Stage.AUDIO.value, {}).get("detail") or {}
    n_chunks = int(audio_detail.get("chunks", 0))
    chunk_s = int(audio_detail.get("chunk_s", ctx.settings.audio_chunk_s))
    if n_chunks == 0:
        return StageOutcome(model=None, detail={"segments": 0, "no_audio": True})
    done = ctx.repo.transcribed_chunks(video_id)  # resume: skip chunks finished in a previous attempt
    languages: Counter[str] = Counter()
    model = None
    for idx in range(n_chunks):
        if idx in done:
            continue
        wav = ctx.store.get_bytes(ObjectRef(ctx.settings.bucket_derived, Keys.audio_chunk(video_id, idx)))
        resp = ctx.inference.transcribe(wav, filename=f"chunk_{idx:04d}.wav")
        model = _model(resp.model)
        rows = offset_segments(resp.segments, idx, chunk_s, video_id, resp.language, model)
        ctx.repo.replace_segments(video_id, idx, rows)
        languages.update(r["language"] for r in rows)
        log.info(
            "asr_chunk_done", video_id=video_id, chunk=idx, of=n_chunks, segments=len(rows), language=resp.language
        )
    if languages:
        ctx.repo.update_video(video_id, language=languages.most_common(1)[0][0])
    total = len(ctx.repo.segments(video_id))
    return StageOutcome(model=model, detail={"segments": total, "chunks": n_chunks, "resumed_from": len(done)})


# ── scenes + keyframes ───────────────────────────────────────────────────────
def run_scenes(ctx: Context, video_id: str) -> StageOutcome:
    s = ctx.settings
    video = ctx.video(video_id)
    src = ctx.local_original(video)
    duration = float(video.get("duration_s") or media.probe(src).duration_s)
    scenes = media.detect_scenes(
        src, float(video.get("fps") or 25), s.scene_threshold, s.min_scene_s, duration, s.max_scenes_per_video
    )
    rows = []
    for sc in scenes:
        local = media.extract_frame(
            src, sc.keyframe_s, ctx.scratch(video_id) / "keyframes" / f"{sc.idx:04d}.jpg", s.keyframe_max_px
        )
        ref = ObjectRef(s.bucket_derived, Keys.keyframe(video_id, sc.idx))
        ctx.store.upload_file(local, ref, "image/jpeg")
        rows.append(
            {
                "scene_id": f"{video_id}:sc{sc.idx:04d}",
                "video_id": video_id,
                "scene_idx": sc.idx,
                "start_s": sc.start_s,
                "end_s": sc.end_s,
                "keyframe_s": sc.keyframe_s,
                "keyframe_uri": ref.uri,
            }
        )
    ctx.repo.replace_scenes(video_id, rows)
    return StageOutcome(model=f"pyscenedetect-content@{s.scene_threshold}", detail={"scenes": len(rows)})


# ── captions (VLM) ───────────────────────────────────────────────────────────
def run_captions(ctx: Context, video_id: str, batch: int = 8) -> StageOutcome:
    todo = ctx.repo.scenes_missing(video_id, "caption")
    model = None
    for i in range(0, len(todo), batch):
        part = todo[i : i + batch]
        resp = ctx.inference.caption(
            [ctx.keyframe(r["keyframe_uri"]) for r in part], prompt=ctx.settings.caption_prompt
        )
        model = _model(resp.model)
        ctx.repo.update_scenes(
            [
                {"scene_id": r["scene_id"], "caption": c.strip(), "caption_model": model}
                for r, c in zip(part, resp.captions, strict=True)
            ]
        )
    return StageOutcome(model=model, detail={"captioned": len(todo)})


# ── OCR ──────────────────────────────────────────────────────────────────────
def run_ocr(ctx: Context, video_id: str, batch: int = 8) -> StageOutcome:
    todo = ctx.repo.scenes_missing(video_id, "ocr_text")
    model, with_text = None, 0
    for i in range(0, len(todo), batch):
        part = todo[i : i + batch]
        resp = ctx.inference.ocr([ctx.keyframe(r["keyframe_uri"]) for r in part])
        model = _model(resp.model)
        updates = []
        for r, res in zip(part, resp.results, strict=True):
            lines = [ln for ln in res.lines if ln.confidence >= OCR_MIN_CONFIDENCE]
            text = "\n".join(ln.text for ln in lines).strip()
            with_text += bool(text)
            updates.append(
                {
                    "scene_id": r["scene_id"],
                    "ocr_text": text,  # "" (not NULL) marks "processed, nothing found"
                    "ocr_text_norm": normalize(text),
                    "ocr_lines": [ln.model_dump() for ln in lines],
                    "ocr_model": model,
                }
            )
        ctx.repo.update_scenes(updates)
    return StageOutcome(model=model, detail={"processed": len(todo), "with_text": with_text})


# ── embeddings ───────────────────────────────────────────────────────────────
def run_embed(ctx: Context, video_id: str, batch: int = 32) -> StageOutcome:
    counts, models = Counter(), {}
    segs = ctx.repo.segments_missing_embeddings(video_id)
    for i in range(0, len(segs), batch):
        part = segs[i : i + batch]
        resp = ctx.inference.embed_texts([r["text"] for r in part], purpose="document")
        models["text"] = _model(resp.model)
        ctx.repo.set_segment_embeddings(
            [(r["segment_id"], np.asarray(v, dtype=np.float32)) for r, v in zip(part, resp.vectors, strict=True)],
            models["text"],
        )
        counts["segments"] += len(part)

    for column, source in (("caption_embedding", "caption"), ("ocr_embedding", "ocr_text")):
        rows = ctx.repo.scenes_missing(video_id, column, source=source)
        for i in range(0, len(rows), batch):
            part = rows[i : i + batch]
            resp = ctx.inference.embed_texts([r[source] for r in part], purpose="document")
            models["text"] = _model(resp.model)
            ctx.repo.update_scenes(
                [{"scene_id": r["scene_id"], column: np.asarray(v, dtype=np.float32), "embed_model": models["text"]}
                 for r, v in zip(part, resp.vectors, strict=True)]
            )  # fmt: skip
            counts[column] += len(part)

    imgs = ctx.repo.scenes_missing(video_id, "image_embedding")
    for i in range(0, len(imgs), 16):
        part = imgs[i : i + 16]
        resp = ctx.inference.embed_images([ctx.keyframe(r["keyframe_uri"]) for r in part])
        models["image"] = m = _model(resp.model)
        ctx.repo.update_scenes(
            [
                {"scene_id": r["scene_id"], "image_embedding": np.asarray(v, np.float32), "image_embed_model": m}
                for r, v in zip(part, resp.vectors, strict=True)
            ]
        )
        counts["image_embedding"] += len(part)

    return StageOutcome(model=", ".join(f"{k}={v}" for k, v in sorted(models.items())) or None, detail=dict(counts))


HANDLERS = {
    Stage.PROBE: run_probe,
    Stage.AUDIO: run_audio,
    Stage.ASR: run_asr,
    Stage.SCENES: run_scenes,
    Stage.CAPTIONS: run_captions,
    Stage.OCR: run_ocr,
    Stage.EMBED: run_embed,
}
