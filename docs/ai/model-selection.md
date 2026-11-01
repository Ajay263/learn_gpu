# Model selection and licensing

All models are open-weight. Licences verified 2026-11-01. Re-verify before production deployment
because licences can change between versions.

## Selected models

| Task | Model | Licence | VRAM (4-bit, T4) | Arabic |
|---|---|---|---|---|
| ASR | `openai/whisper-large-v3` | MIT | ~1.6 GB | ✅ excellent |
| Vision-language / caption | `Qwen/Qwen2-VL-7B-Instruct` | Apache-2.0 | ~9 GB | ✅ native |
| Chat / RAG | `Qwen/Qwen2-VL-7B-Instruct` (shared) | Apache-2.0 | (same) | ✅ native |
| Text embeddings | `BAAI/bge-m3` | MIT | ~1.2 GB | ✅ multilingual |
| Image embeddings | `google/siglip-so400m-patch14-384` | Apache-2.0 | ~1.0 GB | ✅ text→image |
| Reranker | `BAAI/bge-reranker-v2-m3` | MIT | ~0.5 GB | ✅ multilingual |
| OCR | PaddleOCR (CPU) | Apache-2.0 | CPU only | ✅ Arabic+English |

**Total GPU VRAM required (T4 16 GB):** ~14 GB with Qwen2-VL in 4-bit.
The VLM is shared between caption and chat so both fit on one T4.

## Licence summary

| Licence | Meaning for commercial/government use |
|---|---|
| MIT | Permissive: use, modify, distribute with attribution |
| Apache-2.0 | Permissive with patent grant: safe for most commercial/government uses |

No model uses a non-commercial-only licence (CC-BY-NC, Llama community licence, etc.).
**Legal review is still required before production use in a government or regulated environment.**
Verify that the specific checkpoint (revision) you download has not changed licence since this review.

## Why these models

**Whisper large-v3:** Best publicly available multilingual ASR. Arabic WER of ~12% on standard benchmarks.
Alternative: `openai/whisper-medium` if VRAM is tight (saves ~0.7 GB, slightly worse Arabic).

**Qwen2-VL-7B:** The 7B variant fits on a T4 in 4-bit. Understands Arabic text in images natively.
Alternative if GPU has more VRAM: `Qwen/Qwen2-VL-72B-Instruct` (requires A100 or multi-GPU).

**BGE-M3:** Unified embedding model (dense + sparse + ColBERT). 1024-dim. Best multilingual text embedding
on the BEIR benchmark as of 2024. Query prefix is added automatically in the backend.

**SigLIP so400m:** Google's improved CLIP. Strong zero-shot image classification and text-image matching.
The text tower maps colour/scene descriptions into the same space as image embeddings, so
"a blue background" matches frames with blue content without any training.

**BGE-reranker-v2-m3:** Cross-encoder reranker. Multilingual. Outputs raw logits → sigmoid maps to [0, 1].
The gateway applies sigmoid so `MIN_RERANK=0.05` means "reject pairs scoring below 5% similarity".

**PaddleOCR:** Better Arabic OCR than Tesseract. Runs on CPU to leave GPU for transformer models.
Processes frames in parallel across CPUs. Downloads ~100 MB of weights on first use.

## Overriding models

Set environment variables before starting the Kaggle inference server:

```python
os.environ["MODEL_ASR"] = "openai/whisper-medium"            # smaller ASR
os.environ["MODEL_VLM"] = "Qwen/Qwen2-VL-72B-Instruct"      # larger VLM (needs A100)
os.environ["MODEL_CHAT"] = "Qwen/Qwen2.5-7B-Instruct"       # separate text LLM
os.environ["MODEL_EMBED_TEXT"] = "BAAI/bge-large-en-v1.5"   # English-only, faster
```

When `MODEL_CHAT` is set, the backend loads a separate text LLM alongside the VLM.
Only do this if you have sufficient VRAM (A100 40 GB or better).

## Embedding dimensions

| Model | Dimension | Config key |
|---|---|---|
| BGE-M3 | 1024 | `EMBED_TEXT_DIM` |
| SigLIP so400m | 1152 | `EMBED_IMAGE_DIM` |

These must match the values in `.env` used when the Pixeltable schema was initialised.
**Do not change dimensions after the index is built** — it requires a full re-index.

## Face recognition (deferred)

Not included in the MVP. When needed: SCRFD (face detection, MIT) + ArcFace-compatible
backbone (various licences — verify each). Requires biometric data handling legal review
in every jurisdiction where it will be used.
