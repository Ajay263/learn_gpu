"""Real GPU backend: all models loaded from HuggingFace/local weights on a CUDA device.

Model selection (all open-weight, verified licences):
  ASR        : openai/whisper-large-v3  (MIT)          — Arabic + English, runs on CPU to save VRAM
  Caption    : Qwen/Qwen2-VL-7B-Instruct (Apache-2.0) — vision-language, Arabic aware
  OCR        : PaddleOCR (Apache-2.0)                  — Arabic+English, CPU-ok
  Text embed : BAAI/bge-m3 (MIT)                       — multilingual, 1024-dim
  Image embed: google/siglip-so400m-patch14-384 (Apache-2.0) — 1152-dim
  Rerank     : BAAI/bge-reranker-v2-m3 (MIT)           — multilingual cross-encoder
  Chat/LLM   : Qwen/Qwen2.5-7B-Instruct (Apache-2.0)  — Arabic + English, instruction tuned

All models are loaded lazily on first call so the gateway starts fast and unavailable capabilities
return 503 rather than crashing startup.

GPU memory budget (T4 / 16 GB typical on Kaggle):
  Qwen2-VL-7B-Instruct (caption)  : ~9 GB (4-bit)
  Qwen2.5-7B-Instruct  (chat)     : ~9 GB (4-bit)
  bge-m3                           : ~1.2 GB
  siglip-so400m                    : ~1.0 GB
  bge-reranker-v2-m3               : ~0.5 GB
  whisper-large-v3                 : CPU only (saves VRAM for VLM); set ASR_DEVICE=cuda to override

Because the T4 has 16 GB we cannot run both VLM and chat LLM simultaneously. The backend shares the
quantised Qwen2-VL for both captioning and chat (VL models accept text-only inputs). If a dedicated
chat LLM is needed, set USE_SEPARATE_LLM=1 and ensure you have enough VRAM (A100 / T4x2).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
from typing import Any

import numpy as np
from PIL import Image

from newsintel_inference.backends.base import Backend, CapabilityUnavailable
from newsintel_inference.contract import (
    AsrResponse,
    AsrSegment,
    CaptionResponse,
    CapabilityStatus,
    ChatRequest,
    ChatResponse,
    EmbedResponse,
    ModelRef,
    OcrResponse,
    OcrResult,
    OcrLine,
    RerankResponse,
    RerankResult,
)

log = logging.getLogger("newsintel_inference.gpu")

# ── model ids (override via env) ───────────────────────────────────────────────
_M_ASR = os.environ.get("MODEL_ASR", "openai/whisper-large-v3")
_M_VLM = os.environ.get("MODEL_VLM", "Qwen/Qwen2-VL-7B-Instruct")
_M_CHAT = os.environ.get("MODEL_CHAT", "")  # empty → reuse VLM for chat
_M_EMBED_TEXT = os.environ.get("MODEL_EMBED_TEXT", "BAAI/bge-m3")
_M_EMBED_IMAGE = os.environ.get("MODEL_EMBED_IMAGE", "google/siglip-so400m-patch14-384")
_M_RERANK = os.environ.get("MODEL_RERANK", "BAAI/bge-reranker-v2-m3")
_M_OCR_LANGS = os.environ.get("OCR_LANGUAGES", "ar,en").split(",")

_EMBED_TEXT_DIM = 1024   # bge-m3 output dimension
_EMBED_IMAGE_DIM = 1152  # siglip-so400m output dimension


def _device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class _Lazy:
    """Thread-safe lazy loader: the first call loads, subsequent calls return cached value."""

    def __init__(self, name: str, loader):
        self._name = name
        self._loader = loader
        self._value = None
        self._error: Exception | None = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._error is not None:
                raise CapabilityUnavailable(f"{self._name} failed to load: {self._error}") from self._error
            if self._value is None:
                try:
                    log.info("Loading %s …", self._name)
                    self._value = self._loader()
                    log.info("Loaded %s", self._name)
                except Exception as e:  # noqa: BLE001
                    self._error = e
                    raise CapabilityUnavailable(f"{self._name} failed to load: {e}") from e
        return self._value

    @property
    def ok(self) -> bool:
        with self._lock:
            return self._value is not None and self._error is None

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._error is not None


# ── loaders ────────────────────────────────────────────────────────────────────

def _load_whisper():
    import whisper  # openai-whisper
    # Load Whisper on CPU by default to preserve all GPU VRAM for the VLM.
    # CPU transcription is ~5–10× real-time for medium-sized audio — acceptable for batch.
    # Set ASR_DEVICE=cuda to override when you have spare VRAM (e.g. A100 / T4x2).
    dev = os.environ.get("ASR_DEVICE", "cpu")
    log.info("Loading Whisper %s on %s", _M_ASR, dev)
    # Strip "openai/" prefix if present (whisper.load_model takes the variant name)
    variant = _M_ASR.split("/")[-1]  # e.g. "whisper-large-v3" -> "large-v3"
    variant = variant.replace("whisper-", "")
    return whisper.load_model(variant, device=dev)


def _load_vlm():
    """Loads Qwen2-VL in 4-bit (bitsandbytes) to fit within T4 VRAM."""
    import torch
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration, BitsAndBytesConfig
    dev = _device()
    log.info("Loading VLM %s on %s (4-bit)", _M_VLM, dev)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16) if dev == "cuda" else None
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        _M_VLM,
        torch_dtype=torch.float16 if dev == "cuda" else torch.float32,
        quantization_config=bnb,
        device_map="auto" if dev == "cuda" else None,
    )
    processor = AutoProcessor.from_pretrained(_M_VLM)
    return model, processor


def _load_separate_chat():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    chat_id = _M_CHAT or "Qwen/Qwen2.5-7B-Instruct"
    dev = _device()
    log.info("Loading separate chat LLM %s on %s (4-bit)", chat_id, dev)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16) if dev == "cuda" else None
    model = AutoModelForCausalLM.from_pretrained(
        chat_id,
        torch_dtype=torch.float16 if dev == "cuda" else torch.float32,
        quantization_config=bnb,
        device_map="auto" if dev == "cuda" else None,
    )
    tok = AutoTokenizer.from_pretrained(chat_id)
    return model, tok


def _load_embed_text():
    from sentence_transformers import SentenceTransformer
    log.info("Loading text embedder %s", _M_EMBED_TEXT)
    return SentenceTransformer(_M_EMBED_TEXT, device=_device())


def _load_embed_image():
    import torch
    from transformers import AutoProcessor, AutoModel
    dev = _device()
    log.info("Loading image embedder %s on %s", _M_EMBED_IMAGE, dev)
    model = AutoModel.from_pretrained(_M_EMBED_IMAGE, torch_dtype=torch.float16 if dev == "cuda" else torch.float32)
    model = model.to(dev)
    proc = AutoProcessor.from_pretrained(_M_EMBED_IMAGE)
    return model, proc


def _load_reranker():
    from sentence_transformers import CrossEncoder
    log.info("Loading reranker %s", _M_RERANK)
    return CrossEncoder(_M_RERANK, max_length=512, device=_device())


def _load_ocr():
    import easyocr
    log.info("Loading EasyOCR for langs: %s", _M_OCR_LANGS)
    # EasyOCR avoids the CUDA-context conflict that kills Kaggle kernels with PaddleOCR 3.x.
    # gpu=False: leave CUDA for the transformer models; EasyOCR on CPU is fast enough for frames.
    langs = [l for l in _M_OCR_LANGS if l in ("ar", "en", "fa")]
    if not langs:
        langs = ["en"]
    return easyocr.Reader(langs, gpu=False)


# ── helper: image → base64 ────────────────────────────────────────────────────

def _pil_to_b64(img: Image.Image, fmt: str = "JPEG") -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format=fmt, quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ── backend ───────────────────────────────────────────────────────────────────

class GpuBackend(Backend):
    name = "gpu"
    embed_text_dim = _EMBED_TEXT_DIM
    embed_image_dim = _EMBED_IMAGE_DIM

    def __init__(self):
        self._asr = _Lazy("whisper", _load_whisper)
        self._vlm = _Lazy("qwen2-vl", _load_vlm)
        self._chat_llm: _Lazy | None = _Lazy("chat-llm", _load_separate_chat) if _M_CHAT else None
        self._embed_text = _Lazy("bge-m3", _load_embed_text)
        self._embed_image = _Lazy("siglip", _load_embed_image)
        self._rerank = _Lazy("bge-reranker", _load_reranker)
        self._ocr = _Lazy("paddleocr", _load_ocr)

    @classmethod
    def from_env(cls) -> "GpuBackend":
        return cls()

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, CapabilityStatus]:
        def _cap(lazy: _Lazy, model_name: str) -> CapabilityStatus:
            if lazy.ok:
                return CapabilityStatus(available=True, model=ModelRef(name=model_name))
            if lazy.failed:
                return CapabilityStatus(available=False, detail="load failed — see gateway logs")
            return CapabilityStatus(available=True, model=ModelRef(name=model_name), detail="not yet loaded (lazy)")
        chat_lazy = self._chat_llm if self._chat_llm else self._vlm
        chat_name = _M_CHAT or _M_VLM
        return {
            "asr": _cap(self._asr, _M_ASR),
            "caption": _cap(self._vlm, _M_VLM),
            "ocr": _cap(self._ocr, "paddleocr"),
            "embed_text": _cap(self._embed_text, _M_EMBED_TEXT),
            "embed_image": _cap(self._embed_image, _M_EMBED_IMAGE),
            "rerank": _cap(self._rerank, _M_RERANK),
            "chat": _cap(chat_lazy, chat_name),
        }

    # ── ASR ───────────────────────────────────────────────────────────────────

    def asr(self, audio: bytes, filename: str, language: str | None) -> AsrResponse:
        import tempfile, os as _os
        model = self._asr.get()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio)
            tmp = f.name
        try:
            options: dict[str, Any] = {"word_timestamps": False, "verbose": False}
            if language:
                options["language"] = language
            result = model.transcribe(tmp, **options)
        finally:
            _os.unlink(tmp)
        segs = [
            AsrSegment(start=s["start"], end=s["end"], text=s["text"].strip())
            for s in result.get("segments", [])
        ]
        return AsrResponse(
            language=result.get("language", language or "unknown"),
            duration_s=float(segs[-1].end) if segs else 0.0,
            segments=segs,
            model=ModelRef(name=_M_ASR),
        )

    # ── Caption (Qwen2-VL) ────────────────────────────────────────────────────

    def caption(self, images: list[Image.Image], prompt: str, max_tokens: int) -> CaptionResponse:
        import torch
        model, processor = self._vlm.get()
        captions = []
        for img in images:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": img},
                {"type": "text", "text": prompt},
            ]}]
            text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = processor(text=[text], images=[img], padding=True, return_tensors="pt")
            dev = next(model.parameters()).device
            inputs = {k: v.to(dev) for k, v in inputs.items()}
            with torch.no_grad():
                out_ids = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
            # Strip the input tokens to get only generated text
            in_len = inputs["input_ids"].shape[1]
            gen = out_ids[:, in_len:]
            caption_text = processor.batch_decode(gen, skip_special_tokens=True)[0].strip()
            captions.append(caption_text)
        return CaptionResponse(captions=captions, model=ModelRef(name=_M_VLM))

    # ── OCR (PaddleOCR) ───────────────────────────────────────────────────────

    def ocr(self, images: list[Image.Image], languages: list[str]) -> OcrResponse:
        import numpy as np
        reader = self._ocr.get()
        results = []
        for img in images:
            arr = np.array(img.convert("RGB"))
            # EasyOCR returns: [(bbox_pts, text, confidence), ...]
            # bbox_pts = [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
            raw = reader.readtext(arr)
            lines: list[OcrLine] = []
            full_texts: list[str] = []
            for bbox_pts, text, conf in raw:
                xs = [p[0] for p in bbox_pts]
                ys = [p[1] for p in bbox_pts]
                lines.append(OcrLine(
                    text=text, confidence=float(conf),
                    bbox=[min(xs), min(ys), max(xs), max(ys)],
                ))
                full_texts.append(text)
            results.append(OcrResult(text="\n".join(full_texts), lines=lines))
        return OcrResponse(results=results, model=ModelRef(name="easyocr"))

    # ── Text embeddings (BGE-M3) ──────────────────────────────────────────────

    def embed_text(self, texts: list[str], purpose: str) -> EmbedResponse:
        model = self._embed_text.get()
        # BGE-M3 recommends a query prefix for retrieval queries
        if purpose == "query":
            texts = [f"Represent this sentence for searching relevant passages: {t}" for t in texts]
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
        return EmbedResponse(
            vectors=vecs.tolist(),
            dim=int(vecs.shape[1]),
            model=ModelRef(name=_M_EMBED_TEXT),
        )

    # ── Image embeddings (SigLIP image tower) ─────────────────────────────────

    def embed_image(self, images: list[Image.Image]) -> EmbedResponse:
        import torch
        model, proc = self._embed_image.get()
        dev = next(model.parameters()).device
        inputs = proc(images=images, return_tensors="pt", padding=True).to(dev)
        with torch.no_grad():
            feats = model.get_image_features(**inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return EmbedResponse(
            vectors=feats.cpu().float().tolist(),
            dim=feats.shape[1],
            model=ModelRef(name=_M_EMBED_IMAGE),
        )

    # ── Image-query embeddings (SigLIP text tower) ────────────────────────────

    def embed_image_query(self, texts: list[str]) -> EmbedResponse:
        import torch
        model, proc = self._embed_image.get()
        dev = next(model.parameters()).device
        inputs = proc(text=texts, return_tensors="pt", padding=True, truncation=True).to(dev)
        with torch.no_grad():
            feats = model.get_text_features(**inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return EmbedResponse(
            vectors=feats.cpu().float().tolist(),
            dim=feats.shape[1],
            model=ModelRef(name=_M_EMBED_IMAGE),
        )

    # ── Reranker (BGE-reranker-v2-m3) ─────────────────────────────────────────

    def rerank(self, query: str, documents: list[str], top_n: int | None) -> RerankResponse:
        cross_enc = self._rerank.get()
        pairs = [(query, doc) for doc in documents]
        scores = cross_enc.predict(pairs, show_progress_bar=False, convert_to_numpy=True)
        # sigmoid → [0,1]; cross-encoder raw logits → sigmoid
        import math
        def _sig(x: float) -> float:
            return 1.0 / (1.0 + math.exp(-x))
        ranked = sorted(
            [RerankResult(index=i, score=float(_sig(s))) for i, s in enumerate(scores)],
            key=lambda r: r.score,
            reverse=True,
        )
        if top_n:
            ranked = ranked[:top_n]
        return RerankResponse(results=ranked, model=ModelRef(name=_M_RERANK))

    # ── Chat / LLM ────────────────────────────────────────────────────────────

    def chat(self, request: ChatRequest) -> ChatResponse:
        import torch
        if self._chat_llm:
            # Separate text-only LLM
            model, tok = self._chat_llm.get()
            dev = next(model.parameters()).device
            prompt = tok.apply_chat_template(
                [m.model_dump() for m in request.messages],
                tokenize=False, add_generation_prompt=True,
            )
            inputs = tok(prompt, return_tensors="pt").to(dev)
            with torch.no_grad():
                out = model.generate(
                    **inputs,
                    max_new_tokens=request.max_tokens,
                    temperature=request.temperature,
                    do_sample=request.temperature > 0,
                    pad_token_id=tok.eos_token_id,
                )
            text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            return ChatResponse(content=text.strip(), finish_reason="stop", model=ModelRef(name=_M_CHAT))
        else:
            # Reuse VLM (text-only path: no image in messages)
            model, processor = self._vlm.get()
            dev = next(model.parameters()).device
            msgs = [{"role": m.role, "content": m.content} for m in request.messages]
            text_input = processor.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
            inputs = processor(text=[text_input], return_tensors="pt")
            inputs = {k: v.to(dev) for k, v in inputs.items()}
            with torch.no_grad():
                out_ids = model.generate(
                    **inputs,
                    max_new_tokens=request.max_tokens,
                    temperature=request.temperature,
                    do_sample=request.temperature > 0,
                )
            in_len = inputs["input_ids"].shape[1]
            gen = out_ids[:, in_len:]
            content = processor.batch_decode(gen, skip_special_tokens=True)[0].strip()
            return ChatResponse(content=content, finish_reason="stop", model=ModelRef(name=_M_VLM))
