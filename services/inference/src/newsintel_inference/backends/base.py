"""Backend interface. The gateway is model-agnostic; a backend owns model loading and execution."""

from abc import ABC, abstractmethod

from PIL import Image

from newsintel_inference.contract import (
    AsrResponse,
    CapabilityStatus,
    CaptionResponse,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    OcrResponse,
    RerankResponse,
)


class CapabilityUnavailable(RuntimeError):
    """Model not loaded / not configured. Gateway maps this to HTTP 503 (client retries later)."""


class Backend(ABC):
    name: str
    embed_text_dim: int
    embed_image_dim: int

    @abstractmethod
    def status(self) -> dict[str, CapabilityStatus]: ...

    @abstractmethod
    def asr(self, audio: bytes, filename: str, language: str | None) -> AsrResponse: ...

    @abstractmethod
    def caption(self, images: list[Image.Image], prompt: str, max_tokens: int) -> CaptionResponse: ...

    @abstractmethod
    def ocr(self, images: list[Image.Image], languages: list[str]) -> OcrResponse: ...

    @abstractmethod
    def embed_text(self, texts: list[str], purpose: str) -> EmbedResponse: ...

    @abstractmethod
    def embed_image(self, images: list[Image.Image]) -> EmbedResponse: ...

    @abstractmethod
    def embed_image_query(self, texts: list[str]) -> EmbedResponse: ...

    @abstractmethod
    def rerank(self, query: str, documents: list[str], top_n: int | None) -> RerankResponse: ...

    @abstractmethod
    def chat(self, request: ChatRequest) -> ChatResponse: ...
