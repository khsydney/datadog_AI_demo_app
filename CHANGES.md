# Session Notes — chat-llm-rag-dd

_Last updated: 2026-05-18_

---

## Current State

| Component | Value |
|---|---|
| RAG API image | `432030471883.dkr.ecr.us-east-1.amazonaws.com/chat-llm-rag-api:2.1.2` |
| Chat model | `gpt-3.5-turbo` |
| Embedding model | `text-embedding-3-large` (OpenAI API, 3072-dim) |
| Reranker | None — top-K similarity from Milvus |
| Milvus rows | 1514 (two reindex runs; deduplicate if needed) |
| ECR tags | `2.1.2` only |
| Datadog site | `ap2.datadoghq.com` |
| AI Guard | Local heuristic fallback (cloud returning 403 — org Preview flag needed) |
| Prompt tracking | Working — `rag-qa-prompt` v1.0.0 visible in Datadog Prompts tab |
| Hallucination eval | Not yet enabled (template unavailable on ap2; custom LLM Judge needed) |

---

## Changes Made

### 1. Embedding architecture swap: local BGE-m3 → OpenAI API

**Why:** BGE-m3 (1024-dim) ran on CPU inside the API container — slow, memory-heavy, and added 4 GB+ to the image via torch/transformers. `text-embedding-3-large` supports Korean + English equivalently, runs on OpenAI's infrastructure, and needs zero GPU/CPU budget inside the container.

**Files changed:**
- `app/rag_pipeline.py` — replaced `HuggingFaceEmbeddings` with `OpenAIEmbeddings(model=EMB_MODEL)`
- `index/indexer.py` — same swap; `DIM` changed from `1024` → `3072`
- `requirements.txt` — removed `langchain-huggingface`, `sentence-transformers`, `transformers` (no torch)
- `docker/Dockerfile.api` — removed multi-stage build (no CUDA/torch layer needed)
- `k8s/00-namespace.yaml` ConfigMap — `EMBEDDING_MODEL: "text-embedding-3-large"`, `EMBEDDING_DIM: "3072"`

**Breaking change:** Milvus collection dimension changed 1024 → 3072. Required `purge_collection=True` on reindex to drop and recreate the collection.

---

### 2. Reranker removed

**Why:** CrossEncoder (BGE-reranker-v2-m3) added latency and another large model download. For this demo scale (top-10 chunks), Milvus inner-product similarity scores are sufficient ranking signal.

**Change:** `_rerank_impl` simplified to `return docs[:RERANK_K]`. `RERANK_TOP_K` still configurable (default 10) in case a real reranker is wired back later.

---

### 3. Chat model: gpt-4o-mini → gpt-3.5-turbo

**Why:** Two reasons — (1) hit 429 rate limits on gpt-4o-mini during rapid testing + reindex; (2) gpt-3.5-turbo is more susceptible to prompt injection, which makes for a better live demo of AI Guard blocking.

**File:** `k8s/00-namespace.yaml` ConfigMap — `CHAT_MODEL: "gpt-3.5-turbo"`

---

### 4. Datadog prompt tracking (Prompt type)

**Why:** Datadog's hallucination detection and managed evaluations require the LLM call to carry a typed `Prompt` object (not a plain dict) with `rag_context_variables` and `rag_query_variables` set so the platform knows which variables are the retrieved context vs. the user query.

**Key change in `app/rag_pipeline.py`:**
```python
from ddtrace.llmobs.types import Prompt

with LLMObs.annotation_context(
    prompt=Prompt(
        id="rag-qa-prompt",
        template=(
            "You are a document Q&A assistant. "
            "Answer using the context below.\n\nContext:\n{{context}}"
        ),
        version="1.0.0",
        variables={"context": context, "question": question},
        rag_context_variables=["context"],
        rag_query_variables=["question"],
    )
):
    async for chunk in llm_chain_stream_with_mem.astream(...):
        ...
```

**Result:** `rag-qa-prompt` v1.0.0 now visible in Datadog → LLM Observability → Prompts. Evaluations (failure-to-answer, prompt-injection, sentiment) running against each span.

---

### 5. AI Guard integration

`app/ai_guard.py` wraps `ddtrace.appsec.ai_guard`. It tries three import paths for compatibility across ddtrace versions:
1. `new_ai_guard_client()` (ddtrace ≥ 4.7)
2. `AIGuardClient()` constructor
3. Module-level `evaluate()`

Falls back to local regex heuristics when cloud returns 403. The heuristic catches direct injection phrases (`ignore previous instructions`, `disregard your prompt`, etc.) but misses rephrased variants.

**Current blocker:** Cloud AI Guard returns HTTP 403. `DD_APP_KEY` has the `ai_guard_evaluate` scope added, but the org's Preview feature flag for AI Guard is likely not enabled. Contact Datadog account team to enable.

---

### 6. Milvus fixes

- **Dynamic fields:** Old collection was created without `enable_dynamic_field=True`, causing `DataNotMatchException: unexpected field 'author'` when PDFs with author metadata were indexed. Fix: `reindex_directory(purge_collection=True)` drops and recreates.
- **Dimension mismatch:** After embedding swap, reindex with `purge_collection=True` required to recreate collection at 3072-dim.
- **Double-indexed data:** Two reindex runs without purge resulted in 1514 rows instead of ~757. No functional impact (duplicates scored equally and deduplicated naturally by top-K), but worth a purge + single reindex to clean up.

---

### 7. Docker image simplification

Removed multi-stage build. New single-stage `Dockerfile.api`:
- Base: `python:3.11-slim`
- System deps: `libmagic1`, `poppler-utils`, `curl`
- No torch, no CUDA, no local model weights
- Build time: ~3 min (was ~12+ min with torch)
- Image size: ~800 MB (was ~4+ GB)

---

### 8. ECR cleanup

Deleted ~30 dangling untagged images (build layer artifacts from previous tags). Some failed first-pass deletion because they were referenced by manifest lists — fixed by deleting the parent tag (`1.0.0`) first, then retrying orphaned layer digests. Only `2.1.2` remains tagged.

---

## Pending / Known Issues

| Item | Status | Notes |
|---|---|---|
| AI Guard cloud 403 | Open | Org Preview flag needed — contact Datadog account team |
| Hallucination evaluation | Open | Template not available on ap2; create custom LLM Judge at LLM Obs → Evaluations → LLM-as-a-Judge |
| Milvus duplicate rows | Open | Run `reindex_directory(purge_collection=True)` once to clear 1514 → ~757 |
| Streamlit upload UX | Deferred | Per-file progress bar discussed but explicitly kept as manual Rebuild Index button |

---

## Architecture Reference

```
Browser
  └── Streamlit UI  (port 8501)
        └── HTTP → FastAPI RAG API  (port 8000)
                    ├── OpenAI Embeddings  (text-embedding-3-large, API call)
                    ├── Milvus  (vector store, port 19530)
                    ├── Redis  (chat history, port 6379)
                    ├── OpenAI Chat  (gpt-3.5-turbo, API call)
                    └── Datadog Agent (DaemonSet, traces/logs/metrics → ap2.datadoghq.com)
                          └── LLM Observability
                                ├── Prompt tracking (rag-qa-prompt v1.0.0)
                                ├── Managed evals (failure-to-answer, prompt-injection, sentiment)
                                └── Custom evals (answer.length, retrieval.docs_returned, ai_guard.blocked)
```
