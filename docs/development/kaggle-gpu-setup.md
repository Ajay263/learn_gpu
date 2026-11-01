# Kaggle GPU inference setup

The inference gateway runs on Kaggle's free GPU (T4, 16 GB VRAM) during development.
All model weights download to `/root/.cache/` on the Kaggle session disk — nothing is stored permanently.

## Prerequisites

1. A Kaggle account with GPU quota enabled (free, requires phone verification)
2. `INFERENCE_API_KEY` set as a Kaggle secret (same value as in your Codespaces `.env`)
3. This repo accessible as a Kaggle dataset or cloned from GitHub in the notebook

## First-time setup (15–25 min)

1. Open `notebooks/kaggle_inference_server.ipynb` in Kaggle
2. Set the accelerator to **GPU T4 × 2** (Notebook settings → Accelerator)
3. Add a Kaggle secret named `INFERENCE_API_KEY` with the value from `.env`
4. Update **Cell 3** with your GitHub username: `https://github.com/YOUR_USERNAME/learn_gpu.git`
5. Run all cells in order

Cell 4 downloads ~14 GB of weights. This is slow the first time but fast if Kaggle caches the session.

## After the notebook starts

The tunnel URL is printed in Cell 6 output, e.g.:
```
TUNNEL URL: https://abc-def.trycloudflare.com
```

Run this in your Codespaces terminal:
```bash
make set-llm-url URL=https://abc-def.trycloudflare.com
```

That command:
- Updates `AI_INFERENCE_BASE_URL` in `.env`
- Restarts the API and worker containers
- Prints confirmation

Verify the connection:
```bash
make check-llm
```

Expected: `livez OK` and a JSON health object showing `"backend": "gpu"`.

## Subsequent sessions

When a Kaggle session ends (~12 h or manually), the tunnel dies. The worker's retry queue handles this
automatically — inference stages retry at 30 s → 5 min → 30 min intervals, with up to 8 attempts.

When you start a new Kaggle session:
1. Re-run the notebook from Cell 5 (weights should still be cached; skip Cell 4 if so)
2. Copy the new tunnel URL
3. `make set-llm-url URL=https://new-url.trycloudflare.com`

## Model loading is lazy

The gateway starts in under 5 seconds. Models are loaded on the first request for each capability.
The first call to embed, ASR, caption, etc. takes 30–120 s while the model loads into VRAM.
Subsequent calls are fast. The `/v1/health` endpoint shows `"detail": "not yet loaded (lazy)"` for
capabilities that have not been called yet — this is normal, not an error.

## Security

- `INFERENCE_API_KEY` is never printed by the notebook (read from Kaggle secrets)
- The Cloudflare tunnel is HTTPS-only; HTTP connections are rejected
- The gateway requires the API key header on all `/v1/*` routes
- `/livez` is unauthenticated (liveness probe only, reveals nothing about models)
- Never share the tunnel URL publicly — it accepts any request with the right API key

## GPU memory layout (T4 16 GB)

| Model | VRAM |
|---|---|
| Qwen2-VL-7B (4-bit, caption + chat) | ~9 GB |
| BGE-M3 (text embed) | ~1.2 GB |
| SigLIP so400m (image embed) | ~1.0 GB |
| BGE-reranker-v2-m3 | ~0.5 GB |
| Whisper large-v3 | ~1.6 GB |
| **Total** | **~14.3 GB** |

With T4 × 2 (32 GB total) `device_map="auto"` spreads Qwen across both GPUs.
With a single T4 (16 GB) it may require offloading some layers to CPU (slower, still functional).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `livez FAILED` in Codespaces | Tunnel URL changed (new Kaggle session) | `make set-llm-url URL=<new>` |
| `health check failed` | Wrong API key | Check `INFERENCE_API_KEY` in `.env` and Kaggle secrets match |
| `503` on inference calls | Model failed to load | Check Kaggle notebook logs; OOM → reduce batch size or use smaller model |
| Worker stages stay `retrying` | Inference down for > 4 h | New Kaggle session + `make set-llm-url`; or `make set-llm-url URL=http://inference-fake:9100` to fall back to fake |
| Slow first response | Lazy model load (normal) | Wait up to 2 min; subsequent calls are fast |
| OOM error in VLM | T4 VRAM full | Enable `MODEL_CHAT` env override to use a smaller separate LLM, or use T4 × 2 |
