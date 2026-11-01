"""M9: operator actions — retry resets only failed work; requeue skips RUNNING unless asked."""

from datetime import timedelta

import pytest

from newsintel.admin import main as admin_main
from newsintel.pipeline.ops import requeue_stuck, retry_video
from newsintel.pipeline.stages import Stage, StageStatus

V1, V2 = "vid_" + "1" * 24, "vid_" + "2" * 24


class FakeRepo:
    def __init__(self, states: dict[Stage, StageStatus]):
        self.states = dict(states)
        self.stuck_query = None

    def stage_states(self, video_id):
        return dict(self.states)

    def set_stage(self, video_id, stage, status, **kw):
        self.states[stage] = status

    def stuck_stages(self, statuses, older_than):
        self.stuck_query = set(statuses)
        rows = [(V1, Stage.ASR, StageStatus.QUEUED), (V2, Stage.OCR, StageStatus.RUNNING)]
        return [
            {"video_id": v, "stage": s.value, "status": st.value, "attempts": 2} for v, s, st in rows if st in statuses
        ]


class FakePublisher:
    def __init__(self):
        self.jobs = []

    def publish(self, job):
        self.jobs.append(job)


def test_retry_resets_failed_and_skipped_only_then_enqueues():
    done = {s: StageStatus.DONE for s in Stage}
    repo = FakeRepo({**done, Stage.OCR: StageStatus.FAILED})
    pub = FakePublisher()
    assert retry_video(repo, pub, V1) == [Stage.OCR]
    assert repo.states[Stage.OCR] == StageStatus.QUEUED and repo.states[Stage.ASR] == StageStatus.DONE
    assert [j.stage for j in pub.jobs] == [Stage.OCR]


def test_retry_with_nothing_failed_is_a_noop():
    pub = FakePublisher()
    assert retry_video(FakeRepo({s: StageStatus.DONE for s in Stage}), pub, V1) == []
    assert pub.jobs == []


def test_requeue_stuck_excludes_running_by_default():
    repo, pub = FakeRepo({}), FakePublisher()
    assert requeue_stuck(repo, pub, timedelta(minutes=45)) == 1
    assert StageStatus.RUNNING not in repo.stuck_query
    assert [(j.video_id, j.stage, j.attempt) for j in pub.jobs] == [(V1, Stage.ASR, 2)]
    assert requeue_stuck(FakeRepo({}), FakePublisher(), include_running=True) == 2


@pytest.mark.parametrize("argv", [["retry", "vid_x"], ["requeue-stuck"]])
def test_mutating_admin_commands_require_operator(argv, capsys):
    with pytest.raises(SystemExit) as e:
        admin_main(argv)
    assert e.value.code == 2 and "--operator" in capsys.readouterr().err
