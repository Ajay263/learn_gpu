"""Deterministic CPU backend implementing the full contract without any model weights.

Purpose: run the entire pipeline (ingest → process → index → search → answer) in Codespaces and CI.
It is NOT a model. Outputs are designed so that tests are meaningful:

* text embeddings = L2-normalised feature-hashed bag of words → texts sharing words are similar;
* image embeddings = downsampled colour layout → visually similar frames are similar;
* image-query embeddings map colour words ("red", "blue", …) into the same space;
* captions name the dominant colour; rerank scores token overlap;
* ASR returns lines from FAKE_ASR_SCRIPT (a text file, one line per segment) or a placeholder.
"""

import json
import os
import re
import wave
import zlib
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from newsintel_inference.backends.base import Backend
from newsintel_inference.contract import (
    CAPABILITIES,
    AsrResponse,
    AsrSegment,
    CapabilityStatus,
    CaptionResponse,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    ModelRef,
    OcrResponse,
    OcrResult,
    RerankResponse,
    RerankResult,
)

_TOKEN = re.compile(r"\w+", re.UNICODE)
_COLOURS = {
    "red": (220, 30, 30),
    "green": (30, 180, 60),
    "blue": (30, 60, 220),
    "yellow": (230, 220, 40),
    "white": (245, 245, 245),
    "black": (10, 10, 10),
    "grey": (128, 128, 128),
}
_SEGMENT_S = 10.0


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def _normalise(v: np.ndarray) -> list[float]:
    n = float(np.linalg.norm(v))
    return (v / n if n > 0 else v).astype(np.float32).tolist()


def _dominant_colour(img: Image.Image) -> str:
    rgb = np.asarray(img.convert("RGB").resize((16, 16)), dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return min(_COLOURS, key=lambda c: float(np.sum((np.array(_COLOURS[c]) - rgb) ** 2)))


def _wav_duration(audio: bytes) -> float | None:
    try:
        with wave.open(BytesIO(audio)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return None


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, embed_text_dim: int = 1024, embed_image_dim: int = 768):
        self.embed_text_dim = embed_text_dim
        self.embed_image_dim = embed_image_dim
        script = os.environ.get("FAKE_ASR_SCRIPT")
        self._script = Path(script).read_text(encoding="utf-8").splitlines() if script else []

    def _ref(self, capability: str) -> ModelRef:
        return ModelRef(name=f"fake/{capability}", revision="deterministic-v1")

    def status(self) -> dict[str, CapabilityStatus]:
        return {c: CapabilityStatus(available=True, model=self._ref(c)) for c in CAPABILITIES}

    def asr(self, audio: bytes, filename: str, language: str | None) -> AsrResponse:
        duration = _wav_duration(audio) or _SEGMENT_S
        n = max(1, int(np.ceil(duration / _SEGMENT_S)))
        lines = self._script or [f"placeholder transcript segment {i}" for i in range(n)]
        segments = [
            AsrSegment(
                start=i * _SEGMENT_S,
                end=min(duration, (i + 1) * _SEGMENT_S),
                text=lines[i % len(lines)],
                confidence=0.9,
            )
            for i in range(n)
        ]
        detected = language or ("ar" if any("؀" <= ch <= "ۿ" for s in segments for ch in s.text) else "en")
        return AsrResponse(language=detected, duration_s=duration, segments=segments, model=self._ref("asr"))

    def caption(self, images: list[Image.Image], prompt: str, max_tokens: int) -> CaptionResponse:
        caps = [f"A frame dominated by the colour {_dominant_colour(im)}." for im in images]
        return CaptionResponse(captions=caps, model=self._ref("caption"))

    def ocr(self, images: list[Image.Image], languages: list[str]) -> OcrResponse:
        return OcrResponse(results=[OcrResult(text="", lines=[]) for _ in images], model=self._ref("ocr"))

    def _hash_text(self, text: str, dim: int) -> list[float]:
        v = np.zeros(dim, dtype=np.float32)
        for tok in _tokens(text):
            h = zlib.crc32(tok.encode("utf-8"))
            v[h % dim] += 1.0 if (h >> 31) == 0 else -1.0
        return _normalise(v)

    def embed_text(self, texts: list[str], purpose: str) -> EmbedResponse:
        vecs = [self._hash_text(t, self.embed_text_dim) for t in texts]
        return EmbedResponse(vectors=vecs, dim=self.embed_text_dim, model=self._ref("embed_text"))

    def _colour_vector(self, rgb: np.ndarray) -> list[float]:
        # Tile a centred colour signature across the dimension so dims are configurable.
        base = (rgb / 255.0) - 0.5
        v = np.resize(base.astype(np.float32), self.embed_image_dim)
        return _normalise(v)

    def embed_image(self, images: list[Image.Image]) -> EmbedResponse:
        vecs = [
            self._colour_vector(np.asarray(im.convert("RGB").resize((1, 1)), dtype=np.float32).reshape(3))
            for im in images
        ]
        return EmbedResponse(vectors=vecs, dim=self.embed_image_dim, model=self._ref("embed_image"))

    def embed_image_query(self, texts: list[str]) -> EmbedResponse:
        vecs = []
        for t in texts:
            hits = [_COLOURS[c] for c in _COLOURS if c in _tokens(t)]
            rgb = np.mean(np.array(hits, dtype=np.float32), axis=0) if hits else np.array([128.0, 128.0, 128.0])
            vecs.append(self._colour_vector(rgb))
        return EmbedResponse(vectors=vecs, dim=self.embed_image_dim, model=self._ref("embed_image"))

    def rerank(self, query: str, documents: list[str], top_n: int | None) -> RerankResponse:
        q = set(_tokens(query))
        scores = [len(q & set(_tokens(d))) / (len(q) or 1) for d in documents]
        order = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)[: top_n or len(documents)]
        return RerankResponse(
            results=[RerankResult(index=i, score=scores[i]) for i in order], model=self._ref("rerank")
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        """Echo-style grounded answer: cites the first evidence id found in the prompt (e.g. [E1])."""
        prompt = "\n".join(m.content for m in request.messages)
        ids = re.findall(r"\[(E\d+)\]", prompt)
        if request.response_format == "json":
            if ids:
                body = {
                    "answer": f"According to the evidence [{ids[0]}], the topic was discussed.",
                    "citations": [ids[0]],
                    "sufficient_evidence": True,
                }
            else:
                body = {
                    "answer": "The information could not be verified from the indexed videos.",
                    "citations": [],
                    "sufficient_evidence": False,
                }
            content = json.dumps(body)
        else:
            content = f"Fake answer citing [{ids[0]}]." if ids else "The information could not be verified."
        return ChatResponse(content=content, finish_reason="stop", model=self._ref("chat"))
