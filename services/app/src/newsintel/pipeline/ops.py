"""Operator actions on processing state: retry failed work, requeue stuck stages."""

from datetime import UTC, datetime, timedelta

from newsintel.index.repository import Repository
from newsintel.pipeline.orchestrator import advance
from newsintel.pipeline.stages import ACTIVE, Stage, StageStatus
from newsintel.queue import JobPublisher, StageJob


def retry_video(
    repo: Repository, publisher: JobPublisher, video_id: str, stages: list[Stage] | None = None
) -> list[Stage]:
    """Reset FAILED (and the SKIPPED stages that depended on them) to PENDING, then advance.

    Stages that already succeeded are not repeated; resumable stages (ASR) continue where they stopped.
    """
    reset = []
    for stage, status in repo.stage_states(video_id).items():
        if status in (StageStatus.FAILED, StageStatus.SKIPPED) and (stages is None or stage in stages):
            repo.set_stage(video_id, stage, StageStatus.PENDING, attempts=0, error="")
            reset.append(stage)
    if reset:
        advance(repo, publisher, video_id)
    return reset


def requeue_stuck(
    repo: Repository,
    publisher: JobPublisher,
    older_than: timedelta = timedelta(minutes=45),
    include_running: bool = False,
) -> int:
    """Re-publish stages left QUEUED/RETRYING (optionally RUNNING) longer than `older_than` (lost message).

    RUNNING is excluded by default: if a worker dies, RabbitMQ redelivers its unacked message by itself,
    and a long ASR job can legitimately run for a long time — requeuing it would only duplicate work.
    The default threshold exceeds the longest retry tier (30 min), so a scheduled retry is not mistaken
    for a lost one. Safe because handlers are idempotent and the worker ignores jobs for settled stages.
    """
    statuses = ACTIVE if include_running else ACTIVE - {StageStatus.RUNNING}
    rows = repo.stuck_stages(statuses, datetime.now(UTC) - older_than)
    for r in rows:
        publisher.publish(StageJob(video_id=r["video_id"], stage=Stage(r["stage"]), attempt=max(1, r["attempts"] or 1)))
    return len(rows)
