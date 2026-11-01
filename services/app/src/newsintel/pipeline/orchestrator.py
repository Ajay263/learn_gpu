"""Advances a video through the stage graph: marks skips, enqueues runnable stages."""

from typing import Protocol

from newsintel.logging import get_logger
from newsintel.pipeline.stages import Stage, StageStatus, resolve
from newsintel.queue import StageJob

log = get_logger(__name__)


class _Repo(Protocol):
    def stage_states(self, video_id: str) -> dict[Stage, StageStatus]: ...
    def set_stage(self, video_id: str, stage: Stage, status: StageStatus, **kw): ...


class _Publisher(Protocol):
    def publish(self, job: StageJob) -> None: ...


def advance(repo: _Repo, publisher: _Publisher, video_id: str) -> list[Stage]:
    """Idempotent: only PENDING stages are considered, and each is marked QUEUED before publishing.

    If publishing fails after QUEUED was written, the stage is picked up by `requeue_stuck`
    (`newsintel-admin requeue-stuck`), so nothing is lost.
    """
    to_run, new_states = resolve(repo.stage_states(video_id))
    current = repo.stage_states(video_id)
    for stage, status in new_states.items():
        if status == StageStatus.SKIPPED and current.get(stage) != StageStatus.SKIPPED:
            repo.set_stage(video_id, stage, StageStatus.SKIPPED, error="hard dependency failed")
    for stage in to_run:
        repo.set_stage(video_id, stage, StageStatus.QUEUED)
        publisher.publish(StageJob(video_id=video_id, stage=stage))
    if to_run:
        log.info("stages_enqueued", video_id=video_id, stages=[s.value for s in to_run])
    return to_run
