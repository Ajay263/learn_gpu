"""Evidence-grounded answer generation.

Guarantees enforced in code (not just requested in the prompt):
1. The LLM sees only retrieved evidence, numbered [E1..En], and must return JSON with citations.
2. Citations must reference provided evidence ids; unknown ids are dropped.
3. An answer with no valid citation, or flagged insufficient, becomes "could not be verified".
4. Video ids, timestamps and excerpts in the response come from our records, never from LLM text.
5. Weak retrieval (low rerank scores) short-circuits before the LLM is called.
"""

import json
import re
from dataclasses import dataclass, field

from newsintel_inference.contract import ChatMessage

from newsintel.inference import InferenceClient, InferenceUnavailable
from newsintel.logging import get_logger
from newsintel.search.retrieval import Hit

log = get_logger(__name__)

NOT_VERIFIED = "The information could not be verified from the indexed videos."
NOT_VERIFIED_AR = "تعذّر التحقق من هذه المعلومة من مقاطع الفيديو المفهرسة."
MIN_RERANK = 0.05  # below this the best evidence is unrelated; tuned in docs/ai/evaluation.md

SYSTEM_PROMPT = """You are a news-archive analyst. Answer ONLY from the numbered evidence provided.

Rules:
- Every factual statement must cite evidence ids in square brackets, e.g. [E2].
- Never invent names, identities, quotes, numbers, dates, events or timestamps.
- Do not identify people by name unless the name appears verbatim in the evidence text.
- Transcript evidence is machine transcription; scene/image evidence is machine description;
  both can contain errors. Hedge accordingly.
- If the evidence does not answer the question, set sufficient_evidence to false.
- Answer in the language of the question.

Return JSON only: {"answer": string, "citations": ["E1", ...], "sufficient_evidence": boolean}"""


@dataclass
class Evidence:
    id: str
    source: str
    video_id: str
    video_title: str | None
    start_s: float
    end_s: float
    excerpt: str
    keyframe_uri: str | None
    language: str | None
    model: str | None
    relevance: float | None
    retrievers: list[str]


@dataclass
class Answer:
    answer: str
    sufficient_evidence: bool
    citations: list[str]
    evidence: list[Evidence]
    quality: str  # high | medium | low | none
    model: str | None = None
    notes: list[str] = field(default_factory=list)


def to_evidence(hits: list[Hit], titles: dict[str, str | None]) -> list[Evidence]:
    return [
        Evidence(
            id=f"E{i}",
            source=h.source,
            video_id=h.video_id,
            video_title=titles.get(h.video_id),
            start_s=round(h.start_s, 2),
            end_s=round(h.end_s, 2),
            excerpt=h.text[:1200],
            keyframe_uri=h.keyframe_uri,
            language=h.language,
            model=h.model,
            relevance=round(h.rerank, 4) if h.rerank is not None else None,
            retrievers=sorted(h.scores),
        )
        for i, h in enumerate(hits, start=1)
    ]


def quality(evidence: list[Evidence]) -> str:
    if not evidence:
        return "none"
    best = max((e.relevance or 0.0) for e in evidence)
    corroborated = len({e.video_id for e in evidence if (e.relevance or 0) >= best * 0.8}) > 1 or any(
        len(e.retrievers) > 1 for e in evidence
    )
    if best >= 0.5 and corroborated:
        return "high"
    if best >= 0.2:
        return "medium"
    return "low"


def _fmt_ts(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_prompt(question: str, evidence: list[Evidence]) -> list[ChatMessage]:
    blocks = [
        f'[{e.id}] source={e.source} video="{e.video_title or e.video_id}" '
        f"time={_fmt_ts(e.start_s)}-{_fmt_ts(e.end_s)}\n{e.excerpt}"
        for e in evidence
    ]
    user = f"Question: {question}\n\nEvidence:\n" + "\n\n".join(blocks)
    return [ChatMessage(role="system", content=SYSTEM_PROMPT), ChatMessage(role="user", content=user)]


def _is_arabic(text: str) -> bool:
    return sum("؀" <= c <= "ۿ" for c in text) > len(text) * 0.3


def _parse(content: str) -> dict:
    m = re.search(r"\{.*\}", content, re.S)  # tolerate prose or code fences around the JSON
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


def answer_question(question: str, evidence: list[Evidence], inference: InferenceClient) -> Answer:
    not_verified = NOT_VERIFIED_AR if _is_arabic(question) else NOT_VERIFIED
    q = quality(evidence)
    if not evidence or max((e.relevance or 0.0) for e in evidence) < MIN_RERANK:
        return Answer(not_verified, False, [], evidence, "none" if not evidence else "low",
                      notes=["no sufficiently relevant evidence; LLM not called"])  # fmt: skip
    try:
        resp = inference.chat(build_prompt(question, evidence), json_mode=True, temperature=0.0, max_tokens=800)
    except InferenceUnavailable:
        # Retrieval still works without the LLM: return the evidence, clearly labelled.
        return Answer("The answer generator is temporarily unavailable. The most relevant evidence is listed below.",
                      False, [], evidence, q, notes=["llm_unavailable"])  # fmt: skip
    model = f"{resp.model.name}@{resp.model.revision}"
    try:
        data = _parse(resp.content)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("llm_unparseable_answer", error=str(e), content=resp.content[:300])
        return Answer(not_verified, False, [], evidence, q, model, ["llm_output_unparseable"])
    valid_ids = {e.id for e in evidence}
    cited = [c for c in data.get("citations", []) if isinstance(c, str) and c in valid_ids]
    inline = [c for c in re.findall(r"\[(E\d+)\]", str(data.get("answer", ""))) if c in valid_ids]
    citations = list(dict.fromkeys(cited + inline))
    notes = []
    if len(cited) != len(data.get("citations", [])):
        notes.append("dropped citations to unknown evidence ids")
    if not data.get("sufficient_evidence", False) or not citations:
        return Answer(not_verified, False, [], evidence, q, model, notes + ["model reported insufficient evidence"])
    text = re.sub(r"\[(E\d+)\]", lambda m: m.group(0) if m.group(1) in valid_ids else "", str(data["answer"])).strip()
    return Answer(text, True, citations, evidence, q, model, notes)
