"""M7: fusion, query parsing and the grounding guarantees of answer generation."""

import json

from newsintel_inference.contract import ChatResponse, ModelRef

from newsintel.inference import InferenceUnavailable
from newsintel.search.answer import NOT_VERIFIED, NOT_VERIFIED_AR, Evidence, answer_question, quality
from newsintel.search.query import parse_query
from newsintel.search.retrieval import Hit, rrf


def hit(ref, source="transcript", **scores):
    return Hit(source, "vid_x", 0, 1, f"text {ref}", ref, scores=scores)


def ev(i, relevance=0.9, video="vid_a", retrievers=("semantic",)):
    return Evidence(
        f"E{i}",
        "transcript",
        video,
        "t",
        10.0 * i,
        10.0 * i + 5,
        f"excerpt {i}",
        None,
        "en",
        "asr@1",
        relevance,
        list(retrievers),
    )


class StubLLM:
    def __init__(self, content=None, exc=None):
        self.content, self.exc, self.calls = content, exc, 0

    def chat(self, *a, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return ChatResponse(content=self.content, model=ModelRef(name="llm", revision="1"))


# ── fusion ───────────────────────────────────────────────────────────────────
def test_rrf_rewards_items_found_by_several_retrievers():
    semantic = [hit("a", semantic=0.9), hit("b", semantic=0.8)]
    keyword = [hit("b", keyword=2.0), hit("c", keyword=1.0)]
    fused = rrf([semantic, keyword])
    assert [h.ref_id for h in fused] == ["b", "a", "c"]
    assert set(fused[0].scores) == {"semantic", "keyword"}


# ── query parsing ────────────────────────────────────────────────────────────
def test_parse_language_filter_and_cleanup():
    p = parse_query("Show me the news videos where a speaker discussed a payment issue in Arabic.")
    assert p.filters.language == "ar" and p.filters.sources == {"transcript"}
    assert p.text == "a speaker discussed a payment issue"


def test_parse_arabic_language_phrase_and_video_id():
    p = parse_query("ما الذي قيل عن الطقس بالعربية vid_" + "0" * 24)
    assert p.filters.language == "ar" and p.filters.video_ids == ["vid_" + "0" * 24]


def test_parse_plain_question_keeps_all_sources():
    p = parse_query("Find the video where the minister discussed the new government policy")
    assert p.filters.language is None and "image" in p.filters.sources
    assert p.text == "the minister discussed the new government policy"


# ── grounding guarantees ─────────────────────────────────────────────────────
def test_grounded_answer_keeps_only_valid_citations():
    llm = StubLLM(
        json.dumps(
            {"answer": "Payments were discussed [E1] [E9].", "citations": ["E1", "E9"], "sufficient_evidence": True}
        )
    )
    a = answer_question("what about payments?", [ev(1), ev(2)], llm)
    assert a.sufficient_evidence and a.citations == ["E1"]
    assert "[E9]" not in a.answer and "dropped citations" in a.notes[0]


def test_answer_without_citations_is_not_verified():
    llm = StubLLM(json.dumps({"answer": "Yes, definitely.", "citations": [], "sufficient_evidence": True}))
    a = answer_question("did it happen?", [ev(1)], llm)
    assert a.answer == NOT_VERIFIED and not a.sufficient_evidence


def test_model_reporting_insufficient_evidence_is_respected_in_arabic():
    llm = StubLLM(json.dumps({"answer": "...", "citations": ["E1"], "sufficient_evidence": False}))
    a = answer_question("هل تحدث الوزير عن السياسة الجديدة؟", [ev(1)], llm)
    assert a.answer == NOT_VERIFIED_AR


def test_weak_evidence_short_circuits_before_llm():
    llm = StubLLM("{}")
    a = answer_question("unrelated question", [ev(1, relevance=0.01)], llm)
    assert llm.calls == 0 and a.answer == NOT_VERIFIED and a.quality == "low"


def test_no_evidence_means_no_answer():
    llm = StubLLM("{}")
    a = answer_question("anything", [], llm)
    assert llm.calls == 0 and a.quality == "none" and a.citations == []


def test_llm_outage_still_returns_evidence():
    a = answer_question("q", [ev(1)], StubLLM(exc=InferenceUnavailable("down")))
    assert not a.sufficient_evidence and a.evidence and "llm_unavailable" in a.notes


def test_unparseable_llm_output_is_not_verified():
    a = answer_question("q", [ev(1)], StubLLM("I think the answer is yes"))
    assert a.answer == NOT_VERIFIED and "llm_output_unparseable" in a.notes


def test_quality_levels():
    assert quality([]) == "none"
    assert quality([ev(1, 0.9, "a"), ev(2, 0.85, "b")]) == "high"  # strong and corroborated by 2 videos
    assert quality([ev(1, 0.3)]) == "medium"
    assert quality([ev(1, 0.1)]) == "low"
