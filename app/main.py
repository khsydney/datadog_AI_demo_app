# app/main.py
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

# ── Datadog APM + LLM Observability ────────────────────────────────────────
from ddtrace.llmobs import LLMObs
LLMObs.enable(
    ml_app=os.getenv("DD_LLMOBS_ML_APP", "chat-llm-rag"),
    integrations_enabled=True,
)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger

from app.rag_pipeline import stream_generate as rag_stream
from app.rag_pipeline import emb
from app.security_demo import router as security_router
from app.ai_guard import evaluate_prompt
from index.indexer import reindex_directory

# ── Structured JSON logging so the Datadog Agent ingests rich fields ───────
def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    fmt = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s "
        "%(dd.trace_id)s %(dd.span_id)s %(dd.service)s %(dd.env)s %(dd.version)s",
        rename_fields={"levelname": "level", "asctime": "@timestamp"},
    )
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))


_configure_logging()
log = logging.getLogger("rag.api")

app = FastAPI(title="Chat-LLM-RAG (Datadog)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(security_router)

DOC_STORE_DIR = os.getenv("DOC_STORE_DIR", "/data/docs")
ALLOWED_EXTS  = {".pdf", ".md", ".txt", ".html", ".htm"}


class Ask(BaseModel):
    question: Optional[str] = None
    q: Optional[str] = None
    session_id: Optional[str] = None

    def text(self) -> str:
        return (self.question or self.q or "").strip()

    def sid(self) -> str:
        return (self.session_id or "default").strip() or "default"


def _safe_path(filename: str) -> Path:
    name = os.path.basename(filename)
    if not name or name in (".", ".."):
        raise HTTPException(400, "bad filename")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(415, f"extension {ext!r} not allowed; allowed: {sorted(ALLOWED_EXTS)}")
    return Path(DOC_STORE_DIR) / name


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat")
async def chat(req: Ask, request: Request):
    prompt = req.text()
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'question'")
    sid = req.sid()

    verdict = evaluate_prompt(prompt)
    if verdict.blocked:
        log.warning("prompt blocked", extra={
            "ai_guard.action": verdict.action,
            "ai_guard.tags":   verdict.tags,
            "ai_guard.source": verdict.source,
            "usr.id":          sid,
        })
        return StreamingResponse(
            iter([f"[blocked by AI Guard: {', '.join(verdict.tags) or verdict.reason}]"]),
            media_type="text/plain",
        )

    async def token_stream():
        try:
            async for chunk in rag_stream(prompt, session_id=sid):
                yield chunk if isinstance(chunk, str) else str(chunk)
        except Exception as e:  # noqa: BLE001
            log.exception("stream error")
            yield f"\n[stream-error] {e}\n"

    return StreamingResponse(token_stream(), media_type="text/plain")


@app.post("/docs/upload")
async def upload_docs(files: List[UploadFile] = File(...)) -> dict:
    """Receive multipart files and persist to the shared docs PVC.
    Does NOT auto-reindex — call /docs/reindex after."""
    Path(DOC_STORE_DIR).mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        if not f.filename:
            continue
        dst = _safe_path(f.filename)
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dst.name)
    try:
        LLMObs.annotate(tags={"docs.upload.count": str(len(saved))})
    except Exception:  # noqa: BLE001
        pass
    return {"saved": saved, "dir": DOC_STORE_DIR}


@app.post("/docs/reindex")
async def reindex_docs(purge: bool = False) -> dict:
    """Re-embed everything in DOC_STORE_DIR. purge=true drops the collection first."""
    import asyncio
    import functools
    loop = asyncio.get_event_loop()
    fn = functools.partial(
        reindex_directory,
        DOC_STORE_DIR,
        embedding_fn=emb.embed_documents,
        purge_collection=purge,
    )
    summary = await loop.run_in_executor(None, fn)
    try:
        LLMObs.annotate(tags={
            "reindex.files":  str(summary.get("files", 0)),
            "reindex.chunks": str(summary.get("chunks", 0)),
        })
    except Exception:  # noqa: BLE001
        pass
    return summary


@app.get("/docs/list")
async def list_docs() -> dict:
    root = Path(DOC_STORE_DIR)
    if not root.exists():
        return {"docs": [], "dir": DOC_STORE_DIR}
    return {
        "docs": sorted(p.name for p in root.iterdir() if p.is_file()),
        "dir":  DOC_STORE_DIR,
    }


@app.delete("/docs/{filename}")
async def delete_doc(filename: str) -> dict:
    dst = _safe_path(filename)
    if not dst.exists():
        raise HTTPException(404, f"{dst.name} not found")
    dst.unlink()
    return {"deleted": dst.name}
