"""M6: worker failure handling and ASR timestamp offsetting, with in-memory fakes."""

from concurrent.futures import Future

import pytest
from newsintel_inference.contract import AsrSegment

from newsintel.inference import InferenceRejected, InferenceUnavailable
from newsintel.media import MediaError
from newsintel.pipeline import handlers
from newsintel.pipeline.handlers import StageOutcome, offset_segments
from newsintel.pipeline.stages import Stage, StageStatus, video_status
from newsintel.queue import StageJob, retry_tier
from newsintel.worker.main import Worker, classify

VID = "vid_" + "a" * 24


class MemRepo:
    def __init__(self):
        self.states = {s: StageStatus.PENDING for s in Stage}
        self.errors: dict[Stage, str] = {}

    def stage_states(self, video_id):
        return dict(self.states)

    def set_stage(self, video_id, stage, status, *, error=None, **kw):
        self.states[stage] = status
        if error:
            self.errors[stage] = error
        return video_status(self.states)

    def get_video(self, video_id):
        return {"video_id": video_id, "status": video_status(self.states).value}


class MemPublisher:
    def __init__(self):
        self.published, self.retries, self.dead = [], [], []

    def publish(self, job):
        self.published.append(job)

    def publish_retry(self, job):
        self.retries.append(job)
        return retry_tier(job.attempt - 1)

    def publish_dead(self, job):
        self.dead.append(job)


class _Ctx:
    def __init__(self, repo):
        self.repo = repo

    def cleanup(self, video_id):
        pass


def make_worker(settings, monkeypatch, handler):
    w = object.__new__(Worker)
    w.settings, w.ctx, w.publisher = settings, _Ctx(MemRepo()), MemPublisher()
    monkeypatch.setitem(handlers.HANDLERS, Stage.PROBE, handler)
    monkeypatch.setattr("newsintel.worker.main.HANDLERS", handlers.HANDLERS)
    w.pool = type("P", (), {"submit": staticmethod(lambda fn, *a: _run(fn, *a))})()
    return w


def _run(fn, *args) -> Future:
    f: Future = Future()
    try:
        f.set_result(fn(*args))
    except Exception as e:  # noqa: BLE001
        f.set_exception(e)
    return f


def wait(f: Future):
    return f.result()


def test_classification():
    assert classify(InferenceUnavailable("x")) == "transient"
    assert classify(InferenceRejected("x")) == "permanent"
    assert classify(MediaError("x")) == "permanent"
    assert classify(KeyError("bug")) == "unknown"


def test_success_marks_done_and_enqueues_next_stages(settings, monkeypatch):
    w = make_worker(settings, monkeypatch, lambda ctx, vid: StageOutcome(model="m", detail={"n": 1}))
    w.process(StageJob(video_id=VID, stage=Stage.PROBE), wait)
    assert w.ctx.repo.states[Stage.PROBE] == StageStatus.DONE
    assert [j.stage for j in w.publisher.published] == [Stage.AUDIO, Stage.SCENES]


def test_inference_outage_schedules_retry_with_incremented_attempt(settings, monkeypatch):
    def down(ctx, vid):
        raise InferenceUnavailable("HTTP 530 tunnel down")

    w = make_worker(settings, monkeypatch, down)
    w.process(StageJob(video_id=VID, stage=Stage.PROBE, attempt=1), wait)
    assert w.ctx.repo.states[Stage.PROBE] == StageStatus.RETRYING
    assert w.publisher.retries[0].attempt == 2 and "530" in w.publisher.retries[0].reason
    assert not w.publisher.dead


def test_transient_failure_exhausting_attempts_fails_and_dead_letters(settings, monkeypatch):
    def down(ctx, vid):
        raise InferenceUnavailable("still down")

    w = make_worker(settings, monkeypatch, down)
    w.process(StageJob(video_id=VID, stage=Stage.PROBE, attempt=settings.max_stage_attempts), wait)
    assert w.ctx.repo.states[Stage.PROBE] == StageStatus.FAILED
    assert len(w.publisher.dead) == 1
    # probe failed → every dependent is skipped by the orchestrator
    assert w.ctx.repo.states[Stage.AUDIO] == StageStatus.SKIPPED


def test_corrupt_media_fails_immediately_without_retry(settings, monkeypatch):
    def corrupt(ctx, vid):
        raise MediaError("no video stream")

    w = make_worker(settings, monkeypatch, corrupt)
    w.process(StageJob(video_id=VID, stage=Stage.PROBE, attempt=1), wait)
    assert w.ctx.repo.states[Stage.PROBE] == StageStatus.FAILED
    assert not w.publisher.retries and not w.publisher.dead
    assert "no video stream" in w.ctx.repo.errors[Stage.PROBE]


def test_duplicate_delivery_of_settled_stage_is_ignored(settings, monkeypatch):
    calls = []
    w = make_worker(settings, monkeypatch, lambda ctx, vid: calls.append(1) or StageOutcome())
    w.ctx.repo.states[Stage.PROBE] = StageStatus.DONE
    w.process(StageJob(video_id=VID, stage=Stage.PROBE), wait)
    assert calls == []


@pytest.mark.parametrize("attempt,tier", [(1, "retry.30s"), (3, "retry.5m"), (7, "retry.30m")])
def test_retry_tiers(attempt, tier):
    assert retry_tier(attempt) == tier


def test_asr_segments_are_offset_to_absolute_video_time():
    segs = [AsrSegment(start=0.5, end=4.0, text=" مرحبا "), AsrSegment(start=4.0, end=9.5, text="  "),
            AsrSegment(start=9.5, end=12.0, text="Hello")]  # fmt: skip
    rows = offset_segments(segs, chunk_idx=2, chunk_s=300, video_id=VID, language="ar", model="asr@1")
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(600.5, 604.0), (609.5, 612.0)]  # blank segment dropped
    assert rows[0]["segment_id"] == f"{VID}:c0002:s0000" and rows[0]["text"] == "مرحبا"
