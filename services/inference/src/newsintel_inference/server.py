"""Inference gateway: one authenticated HTTP surface in front of all models.

Run:  INFERENCE_API_KEY=... INFERENCE_BACKEND=fake newsintel-inference --port 9100

Environment:
  INFERENCE_API_KEY      required; clients send it in the X-API-Key header
  INFERENCE_BACKEND      fake | gpu   (default fake)
  EMBED_TEXT_DIM / EMBED_IMAGE_DIM   only used by the fake backend
"""

import argparse
import base64
import binascii
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from io import BytesIO

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from newsintel_inference.backends.base import Backend, CapabilityUnavailable
from newsintel_inference.contract import (
    API_KEY_HEADER,
    AsrResponse,
    CaptionRequest,
    CaptionResponse,
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

log = logging.getLogger("newsintel_inference")

MAX_IMAGE_B64 = 12 * 1024 * 1024  # per image, base64 characters
MAX_AUDIO_BYTES = 256 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard


def load_backend() -> Backend:
    kind = os.environ.get("INFERENCE_BACKEND", "fake")
    if kind == "fake":
        from newsintel_inference.backends.fake import FakeBackend

        return FakeBackend(
            embed_text_dim=int(os.environ.get("EMBED_TEXT_DIM", "1024")),
            embed_image_dim=int(os.environ.get("EMBED_IMAGE_DIM", "768")),
        )
    if kind == "gpu":
        from newsintel_inference.backends.gpu import GpuBackend

        return GpuBackend.from_env()
    raise RuntimeError(f"unknown INFERENCE_BACKEND={kind!r}")


def create_app(backend: Backend | None = None, api_key: str | None = None) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("INFERENCE_API_KEY", "")
    if len(key) < 16:
        raise RuntimeError("INFERENCE_API_KEY must be set (>= 16 chars); the gateway never runs unauthenticated")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.backend = backend or load_backend()
        log.info("inference backend ready: %s", app.state.backend.name)
        yield

    app = FastAPI(
        title="News Intelligence Inference Gateway",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def require_key(request: Request) -> None:
        supplied = request.headers.get(API_KEY_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), key.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    def be(request: Request) -> Backend:
        return request.app.state.backend

    @app.exception_handler(CapabilityUnavailable)
    async def _unavailable(_: Request, exc: CapabilityUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "30"})

    @app.middleware("http")
    async def timing(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Elapsed-Ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
        return response

    @app.get("/livez")
    def livez() -> dict:
        """Unauthenticated liveness probe. Reveals nothing about models."""
        return {"ok": True}

    auth = [Depends(require_key)]

    @app.get("/v1/health", dependencies=auth)
    def health(request: Request) -> HealthResponse:
        b = be(request)
        caps = b.status()
        status = "ok" if all(c.available for c in caps.values()) else "degraded"
        return HealthResponse(
            status=status,
            backend=b.name,
            capabilities=caps,
            embed_text_dim=b.embed_text_dim,
            embed_image_dim=b.embed_image_dim,
        )

    @app.post("/v1/asr", dependencies=auth)
    def asr(request: Request, file: UploadFile = File(...), language: str | None = Form(default=None)) -> AsrResponse:
        data = file.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too large; chunk it client-side")
        if language not in (None, "", "auto", "ar", "en"):
            raise HTTPException(422, "language must be auto, ar or en")
        lang = None if language in (None, "", "auto") else language
        return be(request).asr(data, file.filename or "audio.wav", lang)

    @app.post("/v1/vision/caption", dependencies=auth)
    def caption(request: Request, body: CaptionRequest) -> CaptionResponse:
        return be(request).caption(_decode_images(body.images), body.prompt, body.max_tokens)

    @app.post("/v1/ocr", dependencies=auth)
    def ocr(request: Request, body: OcrRequest) -> OcrResponse:
        return be(request).ocr(_decode_images(body.images), body.languages)

    @app.post("/v1/embed/text", dependencies=auth)
    def embed_text(request: Request, body: TextEmbedRequest) -> EmbedResponse:
        return be(request).embed_text(body.texts, body.purpose)

    @app.post("/v1/embed/image", dependencies=auth)
    def embed_image(request: Request, body: ImageEmbedRequest) -> EmbedResponse:
        return be(request).embed_image(_decode_images(body.images))

    @app.post("/v1/embed/image-query", dependencies=auth)
    def embed_image_query(request: Request, body: ImageQueryEmbedRequest) -> EmbedResponse:
        return be(request).embed_image_query(body.texts)

    @app.post("/v1/rerank", dependencies=auth)
    def rerank(request: Request, body: RerankRequest) -> RerankResponse:
        return be(request).rerank(body.query, body.documents, body.top_n)

    @app.post("/v1/chat", dependencies=auth)
    def chat(request: Request, body: ChatRequest) -> ChatResponse:
        return be(request).chat(body)

    return app


def _decode_images(items: list[str]) -> list[Image.Image]:
    out = []
    for i, b64 in enumerate(items):
        if len(b64) > MAX_IMAGE_B64:
            raise HTTPException(413, f"image {i} too large")
        try:
            img = Image.open(BytesIO(base64.b64decode(b64, validate=True)))
            img.load()
        except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            raise HTTPException(422, f"image {i} is not a valid image: {type(e).__name__}") from e
        out.append(img.convert("RGB"))
    return out


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    run()
