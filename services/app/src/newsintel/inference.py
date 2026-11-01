"""Provider-independent client for the inference gateway (contract: newsintel_inference.contract).

The application only knows AI_INFERENCE_BASE_URL + AI_INFERENCE_API_KEY. Whether that is a Kaggle
notebook behind a Cloudflare quick tunnel or an on-prem GPU server is invisible here.
"""

import base64
import io
import threading
import time
from collections.abc import Sequence
from typing import TypeVar

import httpx
from newsintel_inference.contract import (
    API_KEY_HEADER,
    MAX_BATCH,
    AsrResponse,
    CaptionRequest,
    CaptionResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    HealthResponse,
    ImageEmbedRequest,
    ImageQueryEmbedRequest,
    OcrRequest,
    OcrResponse,
    RerankRequest,
    RerankResponse,
    TextEmbedRequest,
)
from PIL import Image
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from newsintel.config import Settings
from newsintel.logging import get_logger
from newsintel.metrics import INFERENCE_CALLS, INFERENCE_LATENCY

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

# 530 = Cloudflare "origin unreachable" (Kaggle session gone but tunnel hostname still resolves).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}


class InferenceError(RuntimeError):
    """Base class."""


class InferenceUnavailable(InferenceError):
    """Transient: endpoint down, overloaded, model not loaded, auth misconfigured. Retry later."""


class InferenceRejected(InferenceError):
    """Permanent for this input (validation error, unsupported media). Do not retry."""


class CircuitBreaker:
    """Fail fast after `threshold` consecutive failures, for `cooldown_s` seconds."""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self._failures, self._opened_at = 0, 0.0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self._failures >= self.threshold and time.monotonic() - self._opened_at < self.cooldown_s:
                raise InferenceUnavailable("circuit open: inference endpoint recently failing")

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures = 0
            else:
                self._failures += 1
                if self._failures >= self.threshold:
                    self._opened_at = time.monotonic()


def encode_image(img: Image.Image, max_px: int = 1024, quality: int = 88) -> str:
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class InferenceClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=settings.ai_inference_base_url,
            headers={API_KEY_HEADER: settings.ai_inference_api_key.get_secret_value()},
            timeout=httpx.Timeout(settings.ai_inference_timeout_s, connect=settings.ai_inference_connect_timeout_s),
            transport=transport,
        )
        self._breaker = CircuitBreaker()

    def close(self) -> None:
        self._http.close()

    # ── transport ─────────────────────────────────────────────────────────────
    @retry(
        retry=retry_if_exception_type(InferenceUnavailable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        reraise=True,
    )
    def _request(self, method: str, path: str, model: type[T], **kwargs) -> T:
        self._breaker.check()
        t0 = time.perf_counter()
        try:
            resp = self._http.request(method, path, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            self._breaker.record(False)
            INFERENCE_CALLS.labels(path, "unavailable").inc()
            raise InferenceUnavailable(f"{path}: {type(e).__name__}: {e}") from e
        finally:
            INFERENCE_LATENCY.labels(path).observe(time.perf_counter() - t0)
        if resp.status_code in RETRYABLE_STATUS or resp.status_code in (401, 403):
            # 401/403 = key mismatch after a Kaggle restart: an operator fix, so wait rather than fail the item.
            self._breaker.record(False)
            INFERENCE_CALLS.labels(path, "unavailable").inc()
            raise InferenceUnavailable(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
        self._breaker.record(True)
        if resp.status_code >= 400:
            INFERENCE_CALLS.labels(path, "rejected").inc()
            raise InferenceRejected(f"{path}: HTTP {resp.status_code}: {resp.text[:500]}")
        INFERENCE_CALLS.labels(path, "ok").inc()
        return model.model_validate(resp.json())

    # ── capabilities ──────────────────────────────────────────────────────────
    def health(self) -> HealthResponse:
        return self._request("GET", "/v1/health", HealthResponse)

    def transcribe(self, wav: bytes, filename: str = "audio.wav", language: str | None = None) -> AsrResponse:
        data = {"language": language} if language else {}
        return self._request("POST", "/v1/asr", AsrResponse, files={"file": (filename, wav, "audio/wav")}, data=data)

    def caption(self, images: Sequence[Image.Image], prompt: str, max_tokens: int = 256) -> CaptionResponse:
        caps: list[str] = []
        resp = None
        for batch in _batches(images, 8):
            body = CaptionRequest(images=[encode_image(i) for i in batch], prompt=prompt, max_tokens=max_tokens)
            resp = self._request("POST", "/v1/vision/caption", CaptionResponse, json=body.model_dump())
            caps.extend(resp.captions)
        assert resp is not None
        return CaptionResponse(captions=caps, model=resp.model)

    def ocr(self, images: Sequence[Image.Image]) -> OcrResponse:
        results, resp = [], None
        for batch in _batches(images, 8):
            body = OcrRequest(images=[encode_image(i, max_px=1600, quality=92) for i in batch])
            resp = self._request("POST", "/v1/ocr", OcrResponse, json=body.model_dump())
            results.extend(resp.results)
        assert resp is not None
        return OcrResponse(results=results, model=resp.model)

    def embed_texts(self, texts: Sequence[str], purpose: str = "document") -> EmbedResponse:
        return self._merge_embeds(
            self._request(
                "POST",
                "/v1/embed/text",
                EmbedResponse,
                json=TextEmbedRequest(texts=list(b), purpose=purpose).model_dump(),
            )  # type: ignore[arg-type]
            for b in _batches(texts, MAX_BATCH)
        )

    def embed_images(self, images: Sequence[Image.Image]) -> EmbedResponse:
        return self._merge_embeds(
            self._request(
                "POST",
                "/v1/embed/image",
                EmbedResponse,
                json=ImageEmbedRequest(images=[encode_image(i, max_px=512) for i in b]).model_dump(),
            )
            for b in _batches(images, 16)
        )

    def embed_image_queries(self, texts: Sequence[str]) -> EmbedResponse:
        return self._merge_embeds(
            self._request(
                "POST", "/v1/embed/image-query", EmbedResponse, json=ImageQueryEmbedRequest(texts=list(b)).model_dump()
            )
            for b in _batches(texts, MAX_BATCH)
        )

    def rerank(self, query: str, documents: Sequence[str], top_n: int | None = None) -> RerankResponse:
        body = RerankRequest(query=query, documents=list(documents), top_n=top_n)
        return self._request("POST", "/v1/rerank", RerankResponse, json=body.model_dump())

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> ChatResponse:
        body = ChatRequest(
            messages=list(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            response_format="json" if json_mode else "text",
        )
        return self._request("POST", "/v1/chat", ChatResponse, json=body.model_dump())

    @staticmethod
    def _merge_embeds(responses) -> EmbedResponse:
        vectors, last = [], None
        for r in responses:
            vectors.extend(r.vectors)
            last = r
        if last is None:
            raise InferenceRejected("no inputs to embed")
        return EmbedResponse(vectors=vectors, dim=last.dim, model=last.model)


def _batches(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
