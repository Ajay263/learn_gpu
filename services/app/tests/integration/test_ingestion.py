"""M3 end-to-end ingestion: SFTP → SFTPGo → MinIO landing → NiFi → bronze + manifest → API → Pixeltable + queue.

Needs: make up-core, api container running, NiFi flow deployed (infra/nifi/deploy_flow.py).
"""

import hashlib
import io
import json
import os
import time
import uuid

import paramiko
import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bulletin-3scenes.mp4"


def wait_for(fn, timeout=120, interval=2, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def root_s3(env, s3_factory):
    return s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])


@pytest.fixture(scope="module")
def repo():
    os.environ["NEWSINTEL_ENV_FILE"] = str(REPO_ROOT / ".env")
    os.environ.pop("PIXELTABLE_DIR", None)  # same directory the API writes to
    from newsintel.config import Settings
    from newsintel.index.repository import Repository

    return Repository(Settings())


def sftp_put(env, data: bytes, remote: str) -> None:
    t = paramiko.Transport(("127.0.0.1", 2022))
    t.connect(username=env["SFTPGO_UPLOADER_USER"], password=env["SFTPGO_UPLOADER_PASSWORD"])
    sftp = paramiko.SFTPClient.from_transport(t)
    try:
        sftp.putfo(io.BytesIO(data), remote)
    finally:
        sftp.close()
        t.close()


def landing_gone(root_s3, key):
    return lambda: "Contents" not in root_s3.list_objects_v2(Bucket="landing", Prefix=key)


@pytest.fixture(scope="module")
def unique_video() -> tuple[bytes, str]:
    if not FIXTURE.exists():
        pytest.skip("run scripts/make-test-video.sh first")
    data = FIXTURE.read_bytes() + uuid.uuid4().bytes  # trailing bytes: still playable, unique checksum
    return data, hashlib.sha256(data).hexdigest()


def test_valid_upload_is_stored_registered_and_queued(env, root_s3, repo, unique_video):
    data, sha = unique_video
    name = f"bulletin-{sha[:8]}.mp4"
    sftp_put(env, data, name)
    wait_for(landing_gone(root_s3, f"uploads/{env['SFTPGO_UPLOADER_USER']}/{name}"), what="NiFi to consume upload")

    head = root_s3.head_object(Bucket="bronze", Key=f"videos/{sha}/original.mp4")
    assert head["ContentLength"] == len(data)
    manifests = root_s3.list_objects_v2(Bucket="manifests", Prefix=f"ingest/{sha}/")["Contents"]
    manifest = json.loads(root_s3.get_object(Bucket="manifests", Key=manifests[0]["Key"])["Body"].read())
    assert manifest["sha256"] == sha and manifest["duplicate"] == "false" and manifest["mime_type"] == "video/mp4"

    from newsintel.index.repository import video_id_for
    from newsintel.pipeline.stages import Stage, StageStatus

    video = wait_for(lambda: repo.get_video(video_id_for(sha)), what="registration")
    assert video["collection"] == "collection-general" and video["uploaded_by"] == env["SFTPGO_UPLOADER_USER"]
    assert video["original_uri"] == f"s3://bronze/videos/{sha}/original.mp4"
    assert repo.stage_states(video["video_id"])[Stage.PROBE] in (
        StageStatus.QUEUED,
        StageStatus.RUNNING,
        StageStatus.DONE,
    )


def test_duplicate_upload_does_not_create_second_video(env, root_s3, repo, unique_video):
    data, sha = unique_video
    name = f"copy-of-{sha[:8]}.mp4"
    sftp_put(env, data, name)
    wait_for(landing_gone(root_s3, f"uploads/{env['SFTPGO_UPLOADER_USER']}/{name}"), what="duplicate consumed")
    keys = [o["Key"] for o in root_s3.list_objects_v2(Bucket="manifests", Prefix=f"ingest/{sha}/")["Contents"]]
    assert len(keys) == 2  # one manifest per upload event
    dup = [json.loads(root_s3.get_object(Bucket="manifests", Key=k)["Body"].read()) for k in keys]
    assert {m["duplicate"] for m in dup} == {"true", "false"}
    versions = root_s3.list_object_versions(Bucket="bronze", Prefix=f"videos/{sha}/").get("Versions", [])
    assert len(versions) == 1  # original was not rewritten
    v = repo.t.videos
    assert v.where(v.sha256 == sha).count() == 1


def test_non_video_disguised_as_mp4_is_rejected_with_reason(env, root_s3):
    name = f"not-a-video-{uuid.uuid4().hex[:6]}.mp4"
    sftp_put(env, b"this is plain text, not a video\n" * 10, name)
    wait_for(landing_gone(root_s3, f"uploads/{env['SFTPGO_UPLOADER_USER']}/{name}"), what="rejection")
    rejected = root_s3.list_objects_v2(Bucket="landing", Prefix="rejected/")["Contents"]
    assert any(o["Key"].endswith(name) for o in rejected)
    reasons = []
    for o in root_s3.list_objects_v2(Bucket="manifests", Prefix="rejected/")["Contents"]:
        m = json.loads(root_s3.get_object(Bucket="manifests", Key=o["Key"])["Body"].read())
        if m.get("landing_key", "").endswith(name):
            reasons.append(m["reject_reason"])
    assert reasons and "unsupported content type" in reasons[0]
