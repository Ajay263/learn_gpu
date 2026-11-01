"""Pixeltable schema for the news index (Environment 2).

Design (see ADR-002/004 and docs/infrastructure/pixeltable.md):

* One global set of tables keyed by `video_id`, not one directory per video (the tutorial's pattern),
  so a single query searches every video and permission filters are a WHERE clause.
* Media stay in MinIO. Tables hold `s3://` URIs as plain strings. (Verified: inserting into a pxt.Image
  column downloads the object even with media_validation='on_read', so media columns are avoided.)
* Embeddings are PRECOMPUTED by the worker and stored in Array columns. Pixeltable builds pgvector
  indexes over them without calling any model during insert/update, so writes never depend on the
  GPU host being up. Queries pass a vector: `col.similarity(vector=q)`.
* Every model output column has a sibling `*_model` column for provenance.
* Pixeltable 0.7 column types are NOT NULL by default; `| None` marks optional columns (verified).

`ensure_schema()` is idempotent and safe to call at every service start.
"""

from dataclasses import dataclass

import pixeltable as pxt

from newsintel.config import Settings

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Tables:
    videos: pxt.Table
    stages: pxt.Table
    segments: pxt.Table
    scenes: pxt.Table


def _path(settings: Settings, name: str) -> str:
    return f"{settings.pixeltable_dir}.{name}"


def ensure_schema(settings: Settings) -> Tables:
    d = settings.pixeltable_dir
    pxt.create_dir(d, if_exists="ignore")
    text_vec = pxt.Array[(settings.embed_text_dim,), pxt.Float]  # type: ignore[misc]
    image_vec = pxt.Array[(settings.embed_image_dim,), pxt.Float]  # type: ignore[misc]

    videos = pxt.create_table(
        _path(settings, "videos"),
        {
            "video_id": pxt.String,
            "sha256": pxt.String,
            "original_uri": pxt.String,  # s3://bronze/videos/<sha>/original.<ext>
            "manifest_uri": pxt.String | None,
            "filename": pxt.String | None,
            "title": pxt.String | None,
            "collection": pxt.String,  # access-control unit
            "content_type": pxt.String | None,
            "size_bytes": pxt.Int | None,
            "duration_s": pxt.Float | None,
            "width": pxt.Int | None,
            "height": pxt.Int | None,
            "fps": pxt.Float | None,
            "language": pxt.String | None,
            "uploaded_by": pxt.String | None,
            "ingested_at": pxt.Timestamp | None,
            "status": pxt.String,  # registered | processing | indexed | partial | failed
            "updated_at": pxt.Timestamp | None,
        },
        primary_key="video_id",
        comment=f"Registered news videos (schema v{SCHEMA_VERSION})",
        if_exists="ignore",
    )

    stages = pxt.create_table(
        _path(settings, "video_stages"),
        {
            "video_id": pxt.String,
            "stage": pxt.String,
            "status": pxt.String,  # pending | running | done | retrying | failed | skipped
            "attempts": pxt.Int | None,
            "last_error": pxt.String | None,
            "model": pxt.String | None,  # provenance: model name@revision used by this stage
            "detail": pxt.Json | None,
            "started_at": pxt.Timestamp | None,
            "finished_at": pxt.Timestamp | None,
            "updated_at": pxt.Timestamp | None,
        },
        primary_key=["video_id", "stage"],
        comment="Per-stage processing state; one row per (video, stage)",
        if_exists="ignore",
    )

    segments = pxt.create_table(
        _path(settings, "transcript_segments"),
        {
            "segment_id": pxt.String,  # <video_id>:c<chunk>:s<idx>
            "video_id": pxt.String,
            "chunk_idx": pxt.Int | None,
            "start_s": pxt.Float,
            "end_s": pxt.Float,
            "text": pxt.String,
            "text_norm": pxt.String | None,  # normalised for keyword search
            "language": pxt.String | None,
            "confidence": pxt.Float | None,
            "asr_model": pxt.String | None,
            "embedding": text_vec | None,
            "embed_model": pxt.String | None,
        },
        primary_key="segment_id",
        comment="ASR transcript segments with timestamps",
        if_exists="ignore",
    )

    scenes = pxt.create_table(
        _path(settings, "scenes"),
        {
            "scene_id": pxt.String,  # <video_id>:sc<idx>
            "video_id": pxt.String,
            "scene_idx": pxt.Int,
            "start_s": pxt.Float,
            "end_s": pxt.Float,
            "keyframe_s": pxt.Float | None,
            "keyframe_uri": pxt.String | None,  # s3://derived/<video_id>/keyframes/...
            "caption": pxt.String | None,
            "caption_model": pxt.String | None,
            "caption_embedding": text_vec | None,
            "ocr_text": pxt.String | None,
            "ocr_text_norm": pxt.String | None,
            "ocr_lines": pxt.Json | None,
            "ocr_model": pxt.String | None,
            "ocr_embedding": text_vec | None,
            "image_embedding": image_vec | None,
            "embed_model": pxt.String | None,
            "image_embed_model": pxt.String | None,
        },
        primary_key="scene_id",
        comment="Detected scenes with keyframe, caption, OCR and embeddings",
        if_exists="ignore",
    )

    for tbl, col, name in (
        (segments, "embedding", "segments_text_idx"),
        (scenes, "caption_embedding", "scenes_caption_idx"),
        (scenes, "ocr_embedding", "scenes_ocr_idx"),
        (scenes, "image_embedding", "scenes_image_idx"),
    ):
        tbl.add_embedding_index(col, idx_name=name, metric="cosine", if_exists="ignore")

    return Tables(videos=videos, stages=stages, segments=segments, scenes=scenes)


def open_tables(settings: Settings) -> Tables:
    return Tables(
        videos=pxt.get_table(_path(settings, "videos")),
        stages=pxt.get_table(_path(settings, "video_stages")),
        segments=pxt.get_table(_path(settings, "transcript_segments")),
        scenes=pxt.get_table(_path(settings, "scenes")),
    )
