"""Search façade used by the API and the MCP tools: parse → permission filter → retrieve → answer."""

from dataclasses import asdict

from newsintel.config import Settings
from newsintel.index.repository import Repository
from newsintel.inference import InferenceClient
from newsintel.search.answer import Answer, answer_question, to_evidence
from newsintel.search.query import parse_query
from newsintel.search.retrieval import Hit, Retriever


class SearchService:
    def __init__(self, settings: Settings, repo: Repository, inference: InferenceClient):
        self.repo = repo
        self.retriever = Retriever(settings, repo, inference)
        self.inference = inference

    def _titles(self, hits: list[Hit]) -> dict[str, str | None]:
        out = {}
        for vid in {h.video_id for h in hits}:
            v = self.repo.get_video(vid)
            out[vid] = (v or {}).get("title") or (v or {}).get("filename")
        return out

    def search(self, question: str, collections: list[str], top_k: int = 10, sources: set[str] | None = None) -> dict:
        parsed = parse_query(question)
        if sources:
            parsed.filters.sources &= sources  # type: ignore[operator]
        allowed = self.retriever.allowed_videos(collections)
        hits = self.retriever.search(parsed.text, allowed, parsed.filters, top_k=top_k)
        evidence = to_evidence(hits, self._titles(hits))
        return {"query": parsed.text, "hints": parsed.hints, "results": [asdict(e) for e in evidence]}

    def ask(self, question: str, collections: list[str], top_k: int = 8) -> Answer:
        parsed = parse_query(question)
        allowed = self.retriever.allowed_videos(collections)
        hits = self.retriever.search(parsed.text, allowed, parsed.filters, top_k=top_k)
        return answer_question(question, to_evidence(hits, self._titles(hits)), self.inference)
