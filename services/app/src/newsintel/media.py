"""FFmpeg / ffprobe / PySceneDetect wrappers. No pipeline or storage knowledge here.

All subprocesses run with argument lists (never a shell), explicit timeouts, and raise MediaError with
ffmpeg's stderr tail so failures are diagnosable from the stage's `last_error`.
"""

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


class MediaError(RuntimeError):
    """The media itself is unusable (corrupt, no video stream). Permanent: retrying will not help."""


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    format_name: str


@dataclass(frozen=True)
class Scene:
    idx: int
    start_s: float
    end_s: float

    @property
    def keyframe_s(self) -> float:
        return round(self.start_s + (self.end_s - self.start_s) / 2, 3)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"{args[0]} timed out after {timeout}s") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-800:]
        raise MediaError(f"{Path(args[0]).name} exited {proc.returncode}: {tail}")
    return proc


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    num, _, den = rate.partition("/")
    return float(num) / float(den or 1)


def probe(path: Path) -> ProbeResult:
    out = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1),
        None,
    )
    if video is None:
        raise MediaError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("could not determine duration")
    return ProbeResult(
        duration_s=round(duration, 3),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=round(_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")), 3),
        has_audio=audio is not None,
        video_codec=video.get("codec_name", "unknown"),
        audio_codec=audio.get("codec_name") if audio else None,
        format_name=data.get("format", {}).get("format_name", "unknown"),
    )


def extract_audio_chunks(src: Path, out_dir: Path, chunk_s: int, duration_s: float) -> list[Path]:
    """16 kHz mono PCM WAV chunks of `chunk_s` seconds (ASR-friendly, bounded request size, resumable)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("chunk_*.wav"):
        old.unlink()
    _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(chunk_s), "-reset_timestamps", "1",
         str(out_dir / "chunk_%04d.wav")],
        timeout=max(300, duration_s * 2),
    )  # fmt: skip
    chunks = sorted(out_dir.glob("chunk_*.wav"))
    expected = max(1, math.ceil(duration_s / chunk_s))
    if not chunks or abs(len(chunks) - expected) > 1:
        raise MediaError(f"audio segmentation produced {len(chunks)} chunks, expected ~{expected}")
    return chunks


def detect_scenes(
    src: Path, fps: float, threshold: float, min_scene_s: float, duration_s: float, max_scenes: int
) -> list[Scene]:
    """Content-based cut detection. Falls back to one scene; caps the count by merging the shortest neighbours."""
    from scenedetect import ContentDetector, detect

    min_len = max(1, int(round(min_scene_s * (fps or 25))))
    raw = detect(str(src), ContentDetector(threshold=threshold, min_scene_len=min_len), start_in_scene=True)
    bounds = [(s.seconds, e.seconds) for s, e in raw] or [(0.0, duration_s)]
    while len(bounds) > max_scenes:
        i = min(
            range(len(bounds) - 1), key=lambda k: (bounds[k][1] - bounds[k][0]) + (bounds[k + 1][1] - bounds[k + 1][0])
        )
        bounds[i : i + 2] = [(bounds[i][0], bounds[i + 1][1])]
    return [Scene(idx=i, start_s=round(s, 3), end_s=round(e, 3)) for i, (s, e) in enumerate(bounds)]


def extract_frame(src: Path, at_s: float, dest: Path, max_px: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    scale = f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'"
    _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-y", "-ss", f"{at_s:.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", scale, "-q:v", "3", str(dest)],
        timeout=120,
    )  # fmt: skip
    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError(f"no frame extracted at {at_s}s")
    return dest


def cut_clip(src: Path, start_s: float, end_s: float, dest: Path) -> Path:
    """Re-encoded H.264/AAC MP4 clip (frame-accurate; `-c copy` would snap to keyframes)."""
    if end_s <= start_s:
        raise ValueError("end must be after start")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-y", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(dest)],
        timeout=max(120, (end_s - start_s) * 4),
    )  # fmt: skip
    return dest
