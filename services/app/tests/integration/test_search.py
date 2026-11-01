"""M7: retrieval against videos indexed by the running pipeline (fake inference on localhost:9100)."""

import os

import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def search(repo):
    os.environ["AI_INFERENCE_BASE_URL"] = "http://localhost:9100"  # host view of the fake gateway
    from newsintel.config import Settings
    from newsintel.inference import InferenceClient
    from newsintel.search.service import SearchService

    s = Settings()
    svc = SearchService(s, repo, InferenceClient(s))
    if not svc.retriever.allowed_videos(["collection-general"]):
        pytest.skip("no indexed videos — run test_pipeline_e2e first")
    return svc


def test_transcript_question_returns_correct_moment(search):
    res = search.search("Show me the videos where a speaker discussed a payment issue", ["collection-general"])
    top = res["results"][0]
    assert top["source"] == "transcript" and top["start_s"] == 10.0 and "payment issue" in top["excerpt"]
    assert top["video_title"] and top["relevance"] is not None


def test_language_filter_restricts_to_arabic_speech(search):
    res = search.search("weather today in Arabic الطقس", ["collection-general"])
    assert res["results"] and all(r["language"] == "ar" for r in res["results"])
    assert "language=ar" in res["hints"]


def test_visual_query_finds_matching_keyframe(search):
    res = search.search("a red studio background", ["collection-general"], sources={"image"})
    assert res["results"][0]["source"] == "image" and res["results"][0]["start_s"] == 0.0


def test_permission_filter_hides_other_collections(search):
    assert search.search("payment issue", ["collection-restricted"])["results"] == []
    assert search.search("payment issue", [])["results"] == []


def test_ask_returns_grounded_answer_with_citation(search):
    ans = search.ask("Which video discussed the new government policy on digital payments?", ["collection-general"])
    assert ans.sufficient_evidence and ans.citations
    cited = next(e for e in ans.evidence if e.id == ans.citations[0])
    assert cited.video_id.startswith("vid_") and cited.end_s > cited.start_s


def test_ask_about_unindexed_topic_is_not_verified(search):
    ans = search.ask("What did the football coach say about the transfer window?", ["collection-general"])
    assert not ans.sufficient_evidence and "could not be verified" in ans.answer


_ = REPO_ROOT
