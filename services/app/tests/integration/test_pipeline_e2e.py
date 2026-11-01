"""M6 end-to-end: upload → NiFi → API → worker (all stages) → indexed, including an inference outage.

Needs: make up-app FAKE=1, NiFi flow deployed, fixtures generated, FAKE_ASR_SCRIPT=/fixtures/asr-script.txt.
"""

import subprocess

import pytest

from newsintel.pipeline.stages import Stage, StageStatus
from tests.integration.conftest import REPO_ROOT
from tests.integration.test_ingestion import FIXTURE, sftp_put, wait_for

pytestmark = pytest.mark.integration
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(REPO_ROOT / ".env"),
    "-f",
    str(REPO_ROOT / "infra/compose/docker-compose.yml"),
]


def upload_unique(env) -> str:
    import hashlib
    import uuid

    data = FIXTURE.read_bytes() + uuid.uuid4().bytes
    sha = hashlib.sha256(data).hexdigest()
    sftp_put(env, data, f"e2e-{sha[:8]}.mp4")
    from newsintel.index.repository import video_id_for

    return video_id_for(sha)


def status_of(repo, vid):
    v = repo.get_video(vid)
    return v["status"] if v else None


def test_video_is_fully_processed_and_indexed(env, repo):
    vid = upload_unique(env)
    wait_for(lambda: status_of(repo, vid) == "indexed", timeout=240, interval=3, what="indexed")

    scenes = repo.scenes(vid)
    assert [(s["start_s"], s["end_s"]) for s in scenes] == [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    assert ["red" in scenes[0]["caption"], "blue" in scenes[1]["caption"], "green" in scenes[2]["caption"]] == [
        True
    ] * 3
    segs = repo.segments(vid)
    assert [s["language"] for s in segs] == ["en", "en", "ar"]
    assert "payment issue" in segs[1]["text"] and segs[1]["start_s"] == 10.0
    stages = {r["stage"]: r for r in repo.stages(vid)}
    assert all(r["status"] == "done" and r["model"] for r in stages.values())  # provenance on every stage
    sc = repo.t.scenes
    assert sc.where((sc.video_id == vid) & (sc.image_embedding == None)).count() == 0  # noqa: E711


def test_inference_outage_is_retried_and_recovers_without_intervention(env, repo):
    subprocess.run([*COMPOSE, "stop", "inference-fake"], check=True, capture_output=True)
    try:
        vid = upload_unique(env)

        def inference_stages_waiting():
            st = repo.stage_states(vid) if repo.get_video(vid) else {}
            return st.get(Stage.ASR) == StageStatus.RETRYING and st.get(Stage.SCENES) == StageStatus.DONE

        wait_for(inference_stages_waiting, timeout=180, interval=3, what="ASR retrying while scenes done")
        assert repo.get_video(vid)["status"] == "processing"
        err = next(r["last_error"] for r in repo.stages(vid) if r["stage"] == "asr")
        assert "InferenceUnavailable" in err
    finally:
        subprocess.run(
            [*COMPOSE, "--profile", "fake-inference", "start", "inference-fake"], check=True, capture_output=True
        )
    # First retry tier is 30 s; allow two tiers of slack.
    wait_for(lambda: status_of(repo, vid) == "indexed", timeout=420, interval=5, what="recovery after outage")
    asr = next(r for r in repo.stages(vid) if r["stage"] == "asr")
    assert asr["attempts"] >= 2 and asr["status"] == "done"
