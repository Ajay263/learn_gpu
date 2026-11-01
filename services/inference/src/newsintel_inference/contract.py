"""Inference API contract, version 1.

Shared by the gateway (server side) and the application's InferenceClient (client side), so the two
cannot drift. Keep this module dependency-light: pydantic only.

Images travel as base64-encoded JPEG/PNG. Audio travels as a multipart file (16 kHz mono WAV preferred).
Every response names the model that produced it (`ModelRef`) — the app stores this for provenance.
"""

from typing import Literal

from pydantic import BaseModel, Field

API_VERSION = "v1"
API_KEY_HEADER = "X-API-Key"

Capability = Literal["asr", "caption", "ocr", "embed_text", "embed_image", "rerank", "chat"]
CAPABILITIES: tuple[Capability, ...] = ("asr", "caption", "ocr", "embed_text", "embed_image", "rerank", "chat")

MAX_BATCH = 64


class ModelRef(BaseModel):
    name: str = Field(description="Model identifier, e.g. 'BAAI/bge-m3'")
    revision: str = Field(default="unknown", description="Weights revision / commit hash")


class CapabilityStatus(BaseModel):
    available: bool
    model: ModelRef | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    api_version: str = API_VERSION
    backend: str
    capabilities: dict[str, CapabilityStatus]
    embed_text_dim: int
    embed_image_dim: int


# ── ASR ────────────────────────────────────────────────────────────────────────
class AsrSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class AsrResponse(BaseModel):
    language: str = Field(description="ISO 639-1 detected or forced language, e.g. 'ar', 'en'")
    duration_s: float
    segments: list[AsrSegment]
    model: ModelRef


# ── Vision: captions + OCR ─────────────────────────────────────────────────────
class CaptionRequest(BaseModel):
    images: list[str] = Field(min_length=1, max_length=MAX_BATCH, description="base64 images")
    prompt: str = Field(min_length=1, max_length=4000)
    max_tokens: int = Field(default=256, ge=16, le=1024)


class CaptionResponse(BaseModel):
    captions: list[str]
    model: ModelRef


class OcrRequest(BaseModel):
    images: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    languages: list[str] = Field(default_factory=lambda: ["ar", "en"])


class OcrLine(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    bbox: list[float] = Field(description="[x0, y0, x1, y1] in pixels", min_length=4, max_length=4)


class OcrResult(BaseModel):
    text: str
    lines: list[OcrLine]


class OcrResponse(BaseModel):
    results: list[OcrResult]
    model: ModelRef


# ── Embeddings ─────────────────────────────────────────────────────────────────
class TextEmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    purpose: Literal["document", "query"] = "document"


class ImageEmbedRequest(BaseModel):
    images: list[str] = Field(min_length=1, max_length=MAX_BATCH)


class ImageQueryEmbedRequest(BaseModel):
    """Text → vector in the *image* embedding space (CLIP/SigLIP text tower)."""

    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH)


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dim: int
    model: ModelRef


# ── Rerank ─────────────────────────────────────────────────────────────────────
class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    documents: list[str] = Field(min_length=1, max_length=200)
    top_n: int | None = Field(default=None, ge=1)


class RerankResult(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    results: list[RerankResult] = Field(description="Sorted by score, descending")
    model: ModelRef


# ── Chat (OpenAI-compatible subset) ────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.1, ge=0, le=2)
    response_format: Literal["text", "json"] = "text"


class ChatResponse(BaseModel):
    content: str
    finish_reason: str | None = None
    model: ModelRef
