"""Clip generation: cut [start, end] from an original into the `evidence` bucket (cached).

ffmpeg reads the original over HTTP from MinIO using a short-lived *internal* presigned URL and
seeks with range requests, so a 30 s clip never downloads a 1 GB original. The presigned URL is used
only inside the backend network and never returned to clients.
"""

import tempfile
from pathlib import Path

from newsintel import media
from newsintel.config import Settings
from newsintel.storage import Keys, ObjectRef, ObjectStore


class ClipError(ValueError):
    pass


def clip_bounds(start_s: float, end_s: float, duration_s: float | None, max_clip_s: float) -> tuple[float, float]:
    if start_s < 0 or end_s <= start_s:
        raise ClipError("end must be after start, start must be >= 0")
    if duration_s:
        end_s = min(end_s, duration_s)
        if start_s >= duration_s:
            raise ClipError("start is beyond the end of the video")
    if end_s - start_s > max_clip_s:
        raise ClipError(f"clip longer than {max_clip_s:.0f} s")
    return round(start_s, 3), round(end_s, 3)


def make_clip(
    settings: Settings, store: ObjectStore, video: dict, start_s: float, end_s: float
) -> tuple[ObjectRef, bool]:
    """Returns (clip object, created). Idempotent: identical requests reuse the cached clip."""
    start_s, end_s = clip_bounds(start_s, end_s, video.get("duration_s"), settings.max_clip_s)
    ref = ObjectRef(settings.bucket_evidence, Keys.clip(video["video_id"], int(start_s * 1000), int(end_s * 1000)))
    if store.exists(ref):
        return ref, False
    src_url = store.presigned_get(ObjectRef.parse(video["original_uri"]), ttl_s=600)
    with tempfile.TemporaryDirectory(dir=_scratch(settings)) as tmp:
        out = media.cut_clip(src_url, start_s, end_s, Path(tmp) / "clip.mp4")  # type: ignore[arg-type]
        store.upload_file(out, ref, "video/mp4")
    return ref, True


def _scratch(settings: Settings) -> str:
    Path(settings.scratch_dir).mkdir(parents=True, exist_ok=True)
    return settings.scratch_dir
