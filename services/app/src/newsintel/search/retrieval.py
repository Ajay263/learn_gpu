"""Multimodal retrieval: permission-filtered semantic + keyword search, RRF fusion, reranking.

Every function takes `allowed_video_ids` and applies it inside the Pixeltable query. Callers obtain it
from `allowed_videos()` for the requesting user — there is no code path that searches without it.
"""

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from newsintel.config import Settings
from newsintel.index.repository import _LOCK, Repository
from newsintel.inference import InferenceClient
from newsintel.logging import get_logger
from newsintel.textnorm import keywords

log = get_logger(__name__)

Source = Literal["transcript", "scene", "ocr", "image"]
SEARCHABLE_STATUSES = ["indexed", "partial"]
RRF_K = 60


@dataclass
class Hit:
    source: Source
    video_id: str
    start_s: float
    end_s: float
    text: str  # what the reranker / LLM sees
    ref_id: str  # segment_id or scene_id
    keyframe_uri: str | None = None
    language: str | None = None
    model: str | None = None  # provenance of the text (ASR / VLM / OCR model)
    scores: dict[str, float] = field(default_factory=dict)  # per-retriever raw scores
    fused: float = 0.0
    rerank: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.ref_id)


@dataclass
class QueryFilters:
    """Structured constraints extracted from the question (see search.query.parse_query)."""

    language: str | None = None
    video_ids: list[str] | None = None
    sources: set[Source] = field(default_factory=lambda: {"transcript", "scene", "ocr", "image"})


class Retriever:
    def __init__(self, settings: Settings, repo: Repository, inference: InferenceClient):
        self.settings, self.repo, self.inference = settings, repo, inference
        self._qcache: dict[tuple[str, str], np.ndarray] = {}
        self._qlock = threading.Lock()

    # ── permissions ──────────────────────────────────────────────────────────
    def allowed_videos(self, collections: Iterable[str]) -> list[str]:
        v = self.repo.t.videos
        with _LOCK:
            rows = v.where(v.collection.isin(list(collections)) & v.status.isin(SEARCHABLE_STATUSES)).select(v.video_id)
            return [r["video_id"] for r in rows.collect()]

    # ── query embeddings (cached: the same question hits several indexes) ────
    def _qvec(self, kind: str, text: str) -> np.ndarray:
        key = (kind, text)
        with self._qlock:
            if key in self._qcache:
                return self._qcache[key]
        if kind == "text":
            vec = self.inference.embed_texts([text], purpose="query").vectors[0]
        else:
            vec = self.inference.embed_image_queries([text]).vectors[0]
        arr = np.asarray(vec, dtype=np.float32)
        with self._qlock:
            if len(self._qcache) > 512:
                self._qcache.clear()
            self._qcache[key] = arr
        return arr

    # ── individual retrievers ────────────────────────────────────────────────
    def transcripts_semantic(self, q: str, ids: list[str], f: QueryFilters, k: int) -> list[Hit]:
        seg = self.repo.t.segments
        sim = seg.embedding.similarity(vector=self._qvec("text", q))
        cond = seg.video_id.isin(ids) & (seg.embedding != None)
        if f.language:
            cond = cond & (seg.language == f.language)
        with _LOCK:
            rows = (
                seg.where(cond)
                .order_by(sim, asc=False)
                .limit(k)
                .select(
                    seg.segment_id,
                    seg.video_id,
                    seg.start_s,
                    seg.end_s,
                    seg.text,
                    seg.language,
                    seg.asr_model,
                    score=sim,
                )
                .collect()
            )
        return [self._seg_hit(r, "semantic", r["score"]) for r in rows]

    def transcripts_keyword(self, q: str, ids: list[str], f: QueryFilters, k: int) -> list[Hit]:
        terms = keywords(q)
        if not terms:
            return []
        seg = self.repo.t.segments
        any_term = seg.text_norm.contains(terms[0])
        for t in terms[1:]:
            any_term = any_term | seg.text_norm.contains(t)
        cond = seg.video_id.isin(ids) & any_term
        if f.language:
            cond = cond & (seg.language == f.language)
        with _LOCK:
            rows = list(
                seg.where(cond)
                .limit(400)
                .select(
                    seg.segment_id,
                    seg.video_id,
                    seg.start_s,
                    seg.end_s,
                    seg.text,
                    seg.text_norm,
                    seg.language,
                    seg.asr_model,
                )
                .collect()
            )
        scored = sorted(rows, key=lambda r: _term_score(r["text_norm"], terms), reverse=True)[:k]
        return [self._seg_hit(r, "keyword", _term_score(r["text_norm"], terms)) for r in scored]

    def scenes_semantic(self, q: str, ids: list[str], k: int) -> list[Hit]:
        sc = self.repo.t.scenes
        sim = sc.caption_embedding.similarity(vector=self._qvec("text", q))
        with _LOCK:
            rows = (
                sc.where(sc.video_id.isin(ids) & (sc.caption_embedding != None))
                .order_by(sim, asc=False)
                .limit(k)
                .select(
                    sc.scene_id,
                    sc.video_id,
                    sc.start_s,
                    sc.end_s,
                    sc.caption,
                    sc.keyframe_uri,
                    sc.caption_model,
                    score=sim,
                )
                .collect()
            )
        return [
            Hit(
                "scene",
                r["video_id"],
                r["start_s"],
                r["end_s"],
                r["caption"],
                r["scene_id"],
                r["keyframe_uri"],
                model=r["caption_model"],
                scores={"semantic": float(r["score"])},
            )
            for r in rows
        ]

    def ocr_search(self, q: str, ids: list[str], k: int) -> list[Hit]:
        sc = self.repo.t.scenes
        hits: list[Hit] = []
        sim = sc.ocr_embedding.similarity(vector=self._qvec("text", q))
        cols = (sc.scene_id, sc.video_id, sc.start_s, sc.end_s, sc.ocr_text, sc.keyframe_uri, sc.ocr_model)
        with _LOCK:
            sem = sc.where(sc.video_id.isin(ids) & (sc.ocr_embedding != None)).order_by(sim, asc=False).limit(k)
            rows = list(sem.select(*cols, score=sim).collect())
        hits += [self._ocr_hit(r, "semantic", r["score"]) for r in rows]
        terms = keywords(q)
        if terms:
            any_term = sc.ocr_text_norm.contains(terms[0])
            for t in terms[1:]:
                any_term = any_term | sc.ocr_text_norm.contains(t)
            with _LOCK:
                kw = list(
                    sc.where(sc.video_id.isin(ids) & any_term).limit(200).select(*cols, sc.ocr_text_norm).collect()
                )
            kw.sort(key=lambda r: _term_score(r["ocr_text_norm"], terms), reverse=True)
            hits += [self._ocr_hit(r, "keyword", _term_score(r["ocr_text_norm"], terms)) for r in kw[:k]]
        return hits

    def images(self, q: str, ids: list[str], k: int) -> list[Hit]:
        sc = self.repo.t.scenes
        sim = sc.image_embedding.similarity(vector=self._qvec("image", q))
        with _LOCK:
            rows = (
                sc.where(sc.video_id.isin(ids) & (sc.image_embedding != None))
                .order_by(sim, asc=False)
                .limit(k)
                .select(
                    sc.scene_id,
                    sc.video_id,
                    sc.start_s,
                    sc.end_s,
                    sc.caption,
                    sc.keyframe_uri,
                    sc.image_embed_model,
                    score=sim,
                )
                .collect()
            )
        return [
            Hit(
                "image",
                r["video_id"],
                r["start_s"],
                r["end_s"],
                r["caption"] or "(keyframe)",
                r["scene_id"],
                r["keyframe_uri"],
                model=r["image_embed_model"],
                scores={"image": float(r["score"])},
            )
            for r in rows
        ]

    # ── combined search ──────────────────────────────────────────────────────
    def search(
        self,
        q: str,
        allowed_video_ids: list[str],
        f: QueryFilters | None = None,
        top_k: int = 10,
        per_list: int = 20,
        rerank: bool = True,
    ) -> list[Hit]:
        f = f or QueryFilters()
        ids = [i for i in allowed_video_ids if not f.video_ids or i in f.video_ids]
        if not ids:
            return []
        lists: list[list[Hit]] = []
        if "transcript" in f.sources:
            lists += [self.transcripts_semantic(q, ids, f, per_list), self.transcripts_keyword(q, ids, f, per_list)]
        if "scene" in f.sources:
            lists.append(self.scenes_semantic(q, ids, per_list))
        if "ocr" in f.sources:
            lists.append(self.ocr_search(q, ids, per_list))
        if "image" in f.sources:
            lists.append(self.images(q, ids, per_list // 2))
        fused = rrf(lists)[: max(top_k * 3, 20)]
        if rerank and fused:
            resp = self.inference.rerank(q, [h.text for h in fused], top_n=len(fused))
            for r in resp.results:
                fused[r.index].rerank = float(r.score)
            fused.sort(key=lambda h: (h.rerank if h.rerank is not None else -1e9, h.fused), reverse=True)
        return fused[:top_k]

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _seg_hit(r: dict, kind: str, score: float) -> Hit:
        return Hit(
            "transcript",
            r["video_id"],
            r["start_s"],
            r["end_s"],
            r["text"],
            r["segment_id"],
            language=r["language"],
            model=r["asr_model"],
            scores={kind: float(score)},
        )

    @staticmethod
    def _ocr_hit(r: dict, kind: str, score: float) -> Hit:
        return Hit(
            "ocr",
            r["video_id"],
            r["start_s"],
            r["end_s"],
            r["ocr_text"],
            r["scene_id"],
            r["keyframe_uri"],
            model=r["ocr_model"],
            scores={kind: float(score)},
        )


def _term_score(text_norm: str | None, terms: list[str]) -> float:
    if not text_norm:
        return 0.0
    words = set(re.findall(r"\w+", text_norm))
    exact = sum(1 for t in terms if t in words)
    partial = sum(1 for t in terms if t not in words and t in text_norm)
    return exact + 0.5 * partial


def rrf(lists: list[list[Hit]], k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank). Robust to incomparable score scales."""
    merged: dict[tuple[str, str], Hit] = {}
    for hits in lists:
        for rank, h in enumerate(hits, start=1):
            cur = merged.get(h.key)
            if cur is None:
                merged[h.key] = cur = h
            else:
                cur.scores.update(h.scores)
            cur.fused += 1.0 / (k + rank)
    return sorted(merged.values(), key=lambda h: h.fused, reverse=True)
