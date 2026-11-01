"""M9: API with real Keycloak tokens — authn, authz boundaries, playback, clips, audit.

Needs: make up-all (or up-app FAKE=1 + keycloak), make dev-users, at least one indexed video in
collection-general (test_pipeline_e2e).
"""

import httpx
import pytest

pytestmark = pytest.mark.integration
API = "http://localhost:8000"
KC = "http://localhost:8081/realms/newsintel/protocol/openid-connect/token"


@pytest.fixture(scope="module")
def tokens(env):
    out = {}
    for user in ("analyst1", "viewer2", "admin1"):
        r = httpx.post(KC, data={"grant_type": "password", "client_id": "newsintel-cli", "username": user,
                                 "password": env["DEV_USER_PASSWORD"]})  # fmt: skip
        if r.status_code != 200:
            pytest.skip(f"cannot get token for {user}: {r.text[:200]} (run make dev-users)")
        out[user] = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return out


@pytest.fixture(scope="module")
def general_video(tokens):
    vids = [v for v in httpx.get(f"{API}/api/v1/videos", headers=tokens["analyst1"]).json() if v["status"] == "indexed"]
    if not vids:
        pytest.skip("no indexed video in collection-general")
    return vids[0]


def test_unauthenticated_requests_are_rejected():
    assert httpx.get(f"{API}/api/v1/videos").status_code == 401
    assert httpx.get(f"{API}/api/v1/videos", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401


def test_identity_reflects_keycloak_roles_and_groups(tokens):
    me = httpx.get(f"{API}/api/v1/me", headers=tokens["analyst1"]).json()
    assert me == {"username": "analyst1", "roles": ["analyst"], "collections": ["collection-general"]}


def test_collection_isolation_returns_404_not_403(tokens, general_video):
    vid = general_video["video_id"]
    assert all(
        v["collection"] == "collection-restricted"
        for v in httpx.get(f"{API}/api/v1/videos", headers=tokens["viewer2"]).json()
    )
    assert httpx.get(f"{API}/api/v1/videos/{vid}", headers=tokens["viewer2"]).status_code == 404
    assert (
        httpx.post(f"{API}/api/v1/search", headers=tokens["viewer2"], json={"query": "payment issue"}).json()["results"]
        == []
    )


def test_search_and_ask_return_evidence_with_media_links(tokens):
    res = httpx.post(
        f"{API}/api/v1/search", headers=tokens["analyst1"], json={"query": "payment issue"}, timeout=60
    ).json()
    top = res["results"][0]
    assert top["start_s"] == 10.0 and top["open_url"].startswith("/watch/")
    q = {"question": "Who announced a government policy on digital payments?"}
    ans = httpx.post(f"{API}/api/v1/ask", headers=tokens["analyst1"], json=q, timeout=60).json()
    assert ans["sufficient_evidence"] and ans["citations"]
    assert "keyframe_uri" not in ans["evidence"][0]  # internal URIs never leak


def test_playback_streams_with_range_and_rejects_tampering(tokens, general_video):
    pb = httpx.post(f"{API}/api/v1/videos/{general_video['video_id']}/playback?t=10", headers=tokens["analyst1"]).json()
    assert pb["url"].endswith("#t=10.0")
    url = API + pb["url"].split("#")[0]
    r = httpx.get(url, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-range"].startswith("bytes 0-99/")
    assert r.content[4:8] == b"ftyp"  # MP4 signature: the real original
    assert httpx.get(url[:-3] + "AAA").status_code == 403


def test_clip_generation_requires_analyst_and_is_cached(tokens, general_video):
    body = {"video_id": general_video["video_id"], "start_s": 10, "end_s": 15}
    assert httpx.post(f"{API}/api/v1/clips", headers=tokens["viewer2"], json=body).status_code in (403, 404)
    first = httpx.post(f"{API}/api/v1/clips", headers=tokens["analyst1"], json=body, timeout=180)
    assert first.status_code == 200, first.text
    clip = httpx.get(API + first.json()["url"])
    assert clip.status_code == 200 and clip.content[4:8] == b"ftyp" and len(clip.content) > 1000
    second = httpx.post(f"{API}/api/v1/clips", headers=tokens["analyst1"], json=body, timeout=60).json()
    assert second["cached"] is True
    bad = httpx.post(f"{API}/api/v1/clips", headers=tokens["analyst1"], json={**body, "start_s": 20, "end_s": 10})
    assert bad.status_code == 422


def test_jobs_retry_and_audit_permissions(tokens, general_video):
    jobs = httpx.get(f"{API}/api/v1/jobs", headers=tokens["analyst1"]).json()
    assert any(j["video_id"] == general_video["video_id"] for j in jobs)
    assert httpx.get(f"{API}/api/v1/jobs", headers=tokens["viewer2"]).status_code == 403
    r = httpx.post(f"{API}/api/v1/videos/{general_video['video_id']}/retry", headers=tokens["analyst1"]).json()
    assert r["reset_stages"] == []  # nothing failed → nothing reset
    assert httpx.get(f"{API}/api/v1/audit", headers=tokens["analyst1"]).status_code == 403
    events = httpx.get(f"{API}/api/v1/audit?limit=50", headers=tokens["admin1"]).json()
    actions = {e["action"] for e in events}
    assert {"search", "ask", "clip.create", "video.play"} <= actions
    assert any(e["outcome"] == "denied" for e in events)


def test_failed_stage_is_visible_and_retried_via_api(tokens, repo):
    """Simulates retries exhausted on OCR (e.g. Kaggle gone > 30 min): partial → retry → indexed."""
    from newsintel.pipeline.stages import Stage, StageStatus
    from tests.integration.conftest import _load_env
    from tests.integration.test_ingestion import wait_for
    from tests.integration.test_pipeline_e2e import status_of, upload_unique

    vid = upload_unique(_load_env())
    wait_for(lambda: status_of(repo, vid) == "indexed", timeout=240, interval=3, what="indexed")
    repo.set_stage(vid, Stage.OCR, StageStatus.FAILED, error="InferenceUnavailable: simulated")
    assert status_of(repo, vid) == "partial"
    job = next(j for j in httpx.get(f"{API}/api/v1/jobs", headers=tokens["analyst1"]).json() if j["video_id"] == vid)
    assert job["status"] == "partial" and "simulated" in job["errors"]["ocr"]

    r = httpx.post(f"{API}/api/v1/videos/{vid}/retry", headers=tokens["analyst1"]).json()
    assert r["reset_stages"] == ["ocr"]
    wait_for(lambda: status_of(repo, vid) == "indexed", timeout=120, interval=2, what="indexed after retry")
