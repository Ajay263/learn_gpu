# Search and evidence-grounded answers

Code: `services/app/src/newsintel/search/` — `query.py` (parse), `retrieval.py` (retrieve, fuse, rerank),
`answer.py` (grounded generation), `service.py` (façade used by API and MCP).

## Pipeline

```
question ─► parse_query ─► allowed_videos(user collections)   ← permission filter FIRST
                │                       │
                ▼                       ▼
   transcripts: semantic + keyword ─┐
   scenes: caption semantic ────────┼─► RRF fusion ─► rerank (cross-encoder) ─► top-k evidence
   OCR: semantic + keyword ─────────┤                                              │
   images: text→image vector ───────┘                                              ▼
                                         relevance < MIN_RERANK? ── yes ─► "could not be verified"
                                                    │ no
                                                    ▼
                                LLM (JSON: answer, citations, sufficient_evidence)
                                                    ▼
                    validate citations ⊆ evidence ids; ≥1 citation; else "could not be verified"
```

## Why each piece

| Piece | Reason |
|---|---|
| Permission filter inside every query | Unauthorised rows never become candidates, so they cannot leak via ranking or LLM text |
| Semantic retrieval | Paraphrases, cross-lingual matches (BGE-M3 is multilingual) |
| Keyword retrieval on normalised text | Names, numbers, rare terms; Arabic diacritics/letter variants folded (`textnorm.py`) |
| Image retrieval (SigLIP text tower) | Visual questions that no transcript mentions |
| RRF (k=60) | Merges lists whose scores are not comparable (cosine vs. term counts) |
| Reranker | Precision on the fused top ~30; its score is the evidence-relevance signal |
| `MIN_RERANK` short-circuit | Refuses before the LLM can be tempted to answer from weak evidence |
| Server-side citation checks | The prompt *asks* for grounding; the code *enforces* it |

## Evidence object (returned to UI/agent)

`id, source (transcript|scene|ocr|image), video_id, video_title, start_s, end_s, excerpt, keyframe_uri,
language, model (provenance), relevance (rerank score), retrievers (which retrievers found it)`.
All fields come from the index — never from LLM output.

`quality`: `high` (relevance ≥ 0.5 and corroborated by a second retriever or video), `medium` (≥ 0.2),
`low`, `none`.

## Calibration warning

`MIN_RERANK = 0.05` and the quality thresholds assume a calibrated cross-encoder (bge-reranker-v2-m3
outputs sigmoid scores; unrelated pairs score ≈ 0). With the fake backend they are only approximately
meaningful. **Re-tune on the evaluation set after switching to real models** (`docs/ai/evaluation.md`).
Found during testing: a token-overlap reranker that counts stop-words let an unrelated question pass
the threshold.

## Query parsing (rule-based, MVP)

Extracts spoken-language filters (English/Arabic phrasing), explicit `vid_…` ids, and OCR/visual hints;
strips filler ("show me the videos where…"). A language filter restricts search to transcripts.
Dates/entities are left to semantic retrieval for now (see PROJECT_STATUS known issues).
