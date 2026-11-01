"""M4: Pixeltable repository against the compose Postgres. Uses an isolated Pixeltable directory."""

import os
import uuid

import numpy as np
import pytest

from newsintel.pipeline.stages import Stage, StageStatus, VideoStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def repo():
    from tests.integration.conftest import REPO_ROOT

    os.environ["NEWSINTEL_ENV_FILE"] = str(REPO_ROOT / ".env")
    os.environ["PIXELTABLE_DIR"] = f"it_{uuid.uuid4().hex[:8]}"
    from newsintel.config import Settings
    from newsintel.index.repository import Repository

    r = Repository(Settings())
    yield r
    import pixeltable as pxt

    pxt.drop_dir(os.environ.pop("PIXELTABLE_DIR"), force=True)


def test_register_is_idempotent_and_initialises_stages(repo):
    sha = uuid.uuid4().hex * 2
    vid, created = repo.register_video(
        sha256=sha,
        original_uri=f"s3://bronze/videos/{sha}/original.mp4",
        collection="collection-general",
        filename="a.mp4",
    )
    assert created
    vid2, created2 = repo.register_video(sha256=sha, original_uri="ignored", collection="x")
    assert (vid2, created2) == (vid, False)
    assert repo.get_video(vid)["status"] == VideoStatus.REGISTERED
    assert set(repo.stage_states(vid).values()) == {StageStatus.PENDING}


def test_stage_updates_drive_video_status(repo):
    sha = uuid.uuid4().hex * 2
    vid, _ = repo.register_video(sha256=sha, original_uri="s3://bronze/x", collection="c")
    assert repo.set_stage(vid, Stage.PROBE, StageStatus.RUNNING, attempts=1) == VideoStatus.PROCESSING
    for s in Stage:
        repo.set_stage(vid, s, StageStatus.DONE, model="fake/x@1")
    assert repo.get_video(vid)["status"] == VideoStatus.INDEXED
    assert {r["model"] for r in repo.stages(vid)} == {"fake/x@1"}


def test_segment_replace_is_idempotent_and_searchable(repo):
    vid = "vid_test_" + uuid.uuid4().hex[:6]
    dim = repo.settings.embed_text_dim
    rows = [
        {
            "segment_id": f"{vid}:c0:s{i}",
            "video_id": vid,
            "chunk_idx": 0,
            "start_s": i * 5.0,
            "end_s": i * 5.0 + 5,
            "text": f"text {i}",
        }
        for i in range(3)
    ]
    repo.replace_segments(vid, 0, rows)
    repo.replace_segments(vid, 0, rows)  # re-run must not duplicate
    assert len(repo.segments(vid)) == 3
    assert len(repo.segments_missing_embeddings(vid)) == 3
    vec = np.zeros(dim, dtype=np.float32)
    vec[1] = 1
    repo.set_segment_embeddings([(f"{vid}:c0:s1", vec)], model="fake/embed")
    seg = repo.t.segments
    sim = seg.embedding.similarity(vector=vec)
    top = list(
        seg.where((seg.video_id == vid) & (seg.embedding != None))
        .order_by(sim, asc=False)  # noqa: E711
        .select(seg.segment_id)
        .limit(1)
        .collect()
    )
    assert top[0]["segment_id"] == f"{vid}:c0:s1"
    assert repo.transcribed_chunks(vid) == {0}
    assert [r["text"] for r in repo.segments(vid, start_s=6, end_s=9)] == ["text 1"]
