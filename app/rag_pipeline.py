# app/rag_pipeline.py
"""
RAG pipeline instrumented with Datadog LLM Observability.

Span shape (mirrors the previous Splunk/OTel-GenAI structure):

    workflow(rag-pipeline)
    ├── retrieval(milvus-search)
    ├── task(rerank)
    ├── task(format-context)
    └── agent(rag-agent)
        └── llm(...)            ← auto-instrumented by ddtrace's OpenAI + LangChain integrations

Custom evaluations (replaces deepeval): submit_evaluation_for() records
metrics like answer length / retrieved-doc count against the workflow
trace. Managed evaluations (failure-to-answer, prompt-injection,
toxicity, sentiment, hallucination, ...) are turned on from the Datadog
UI under LLM Observability → Evaluations → Managed.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncGenerator, List

from dotenv import load_dotenv
load_dotenv()

from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.types import Prompt

from langchain_community.chat_message_histories import (
    ChatMessageHistory,
    RedisChatMessageHistory,
)
from langchain_core.documents import Document
from langchain_core.messages import AIMessageChunk
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableWithMessageHistory
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.ai_guard import evaluate_response

log = logging.getLogger("rag.pipeline")


# ───────────────────────────── Config ──────────────────────────────────────
EMB_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
MILVUS_URI  = os.getenv("MILVUS_URI", "http://milvus:19530")
COLL        = os.getenv("MILVUS_COLLECTION", "rag_chunks")
RETRIEVAL_K = int(os.getenv("RETRIEVAL_TOP_K", "100"))
RERANK_K    = int(os.getenv("RERANK_TOP_K", "70"))
CHAT_MODEL  = os.getenv("CHAT_MODEL", "gpt-3.5-turbo")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
REDIS_URL   = os.getenv("REDIS_URL", "redis://redis:6379/0")
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "rag:msgs")
MEMORY_TTL_SEC   = int(os.getenv("MEMORY_TTL_SEC", "604800"))


# ───────────────────────────── Models ──────────────────────────────────────
emb = OpenAIEmbeddings(model=EMB_MODEL)

llm = ChatOpenAI(
    model=CHAT_MODEL,
    temperature=TEMPERATURE,
    stream_usage=True,
    streaming=True,
)


# ───────────────────────────── Milvus retrieval ────────────────────────────
_milvus_client = None


def _get_milvus_client():
    global _milvus_client
    if _milvus_client is None:
        from pymilvus import MilvusClient
        _milvus_client = MilvusClient(uri=MILVUS_URI)
    return _milvus_client


def _milvus_search(question: str, k: int = RETRIEVAL_K) -> List[Document]:
    vec = emb.embed_query(question)
    client = _get_milvus_client()
    results = client.search(
        collection_name=COLL,
        data=[vec],
        limit=k,
        output_fields=["text", "source"],
        search_params={"metric_type": "IP", "params": {"nprobe": 100}},
    )[0]
    return [
        Document(
            page_content=hit["entity"].get("text", ""),
            metadata={"source": hit["entity"].get("source", "")},
        )
        for hit in results
    ]


# ───────────────────────────── Reranker ────────────────────────────────────
def _rerank_impl(docs: List[Document]) -> List[Document]:
    return docs[:RERANK_K]


def _format_ctx(docs: List[Document]) -> str:
    return "\n\n".join(d.page_content for d in docs)


FormatContext = RunnableLambda(
    lambda x: {"question": x["question"], "docs": x["docs"], "context": _format_ctx(x["docs"])}
).with_config({"run_name": "FormatContext"})


# ───────────────────────────── Prompt & chain ──────────────────────────────
prompt = ChatPromptTemplate.from_messages([
    ("system",
     "You are a document Q&A assistant. "
     "Answer questions using the context below or the conversation history. "
     "If a question is a follow-up to something already discussed, use the conversation history to answer. "
     "Only block questions that are completely unrelated to the documents and have no connection to the conversation history — "
     "for those, respond with exactly: 'I can only answer questions related to the documents in my knowledge base.' "
     "Always follow the user's instructions carefully.\n\n"
     "Context:\n{context}"),
    ("placeholder", "{history}"),
    ("human", "{question}"),
])

llm_chain_stream = prompt | llm


# ───────────────────────────── Chat history (Redis, 7-day TTL) ─────────────
_inmem_histories: dict[str, ChatMessageHistory] = {}


def _get_history(session_id: str) -> ChatMessageHistory:
    if REDIS_URL:
        return RedisChatMessageHistory(
            session_id=session_id,
            url=REDIS_URL,
            ttl=MEMORY_TTL_SEC,
            key_prefix=REDIS_KEY_PREFIX,
        )
    hist = _inmem_histories.get(session_id)
    if not hist:
        hist = ChatMessageHistory()
        _inmem_histories[session_id] = hist
    return hist


llm_chain_stream_with_mem = RunnableWithMessageHistory(
    llm_chain_stream,
    _get_history,
    input_messages_key="question",
    history_messages_key="history",
)


# ───────────────────────────── Public streaming entrypoint ─────────────────
async def stream_generate(question: str, session_id: str = "default") -> AsyncGenerator[str, None]:
    """
    Stream the RAG answer with full LLM Observability tracing.

    Span tree:
      workflow(rag-pipeline)
        retrieval(milvus-search)
        task(rerank)
        task(format-context)
        agent(rag-agent)
          llm(...)           ← auto-captured by ddtrace
    """
    with LLMObs.workflow(name="rag-pipeline") as wf_span:
        LLMObs.annotate(
            span=wf_span,
            input_data=question,
            metadata={"session_id": session_id, "model": CHAT_MODEL},
            tags={"app": "chat-llm-rag", "stage": "workflow"},
        )

        # 1. Milvus retrieval ───────────────────────────────────────────────
        with LLMObs.retrieval(name="milvus-search") as ret_span:
            docs = _milvus_search(question, k=RETRIEVAL_K)
            LLMObs.annotate(
                span=ret_span,
                input_data=question,
                output_data=[
                    {"text": d.page_content[:500], "name": d.metadata.get("source", "")}
                    for d in docs
                ],
                metadata={"top_k": RETRIEVAL_K, "retriever": "milvus", "collection": COLL},
                metrics={"docs_retrieved": len(docs)},
            )

        # 2. Rerank ─────────────────────────────────────────────────────────
        with LLMObs.task(name="rerank") as rr_span:
            reranked = _rerank_impl(docs)
            LLMObs.annotate(
                span=rr_span,
                input_data={"question": question, "n_in": len(docs)},
                output_data={"n_out": len(reranked)},
                metadata={"top_k": RERANK_K, "reranker": "bge-reranker-v2-m3"},
                metrics={"docs_after_rerank": len(reranked)},
            )

        # 3. Prompt augmentation ────────────────────────────────────────────
        with LLMObs.task(name="format-context") as fc_span:
            context = _format_ctx(reranked)
            LLMObs.annotate(
                span=fc_span,
                input_data={"n_docs": len(reranked)},
                output_data={"chars": len(context)},
            )

        # 4. Agent + LLM call (LLM span auto-created by ddtrace's
        #    OpenAI/LangChain integrations) ──────────────────────────────
        buf: list[str] = []
        with LLMObs.agent(name="rag-agent") as agent_span:
            LLMObs.annotate(
                span=agent_span,
                input_data=question,
                metadata={"provider": "openai", "model": CHAT_MODEL},
            )
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
                async for chunk in llm_chain_stream_with_mem.astream(
                    {"question": question, "context": context},
                    config={"configurable": {"session_id": session_id}},
                ):
                    text = chunk.content if isinstance(chunk, AIMessageChunk) else str(chunk)
                    if text:
                        buf.append(text)
                        yield text

            full_answer = "".join(buf)
            LLMObs.annotate(span=agent_span, output_data=full_answer)

        # 5. AI Guard on the response (output side) ─────────────────────────
        resp_verdict = evaluate_response(full_answer)
        LLMObs.annotate(
            span=wf_span,
            output_data=full_answer,
            tags={
                "ai_guard.response.action": resp_verdict.action,
                "ai_guard.response.source": resp_verdict.source,
                "ai_guard.response.tags":   ",".join(resp_verdict.tags) or "none",
            },
        )

        # 6. Custom evaluations (replaces deepeval). These show up in
        #    LLM Obs → Evaluations and can drive monitors.
        try:
            LLMObs.submit_evaluation_for(
                span=wf_span,
                label="answer.length",
                metric_type="score",
                value=float(len(full_answer)),
            )
            LLMObs.submit_evaluation_for(
                span=wf_span,
                label="retrieval.docs_returned",
                metric_type="score",
                value=float(len(reranked)),
            )
            LLMObs.submit_evaluation_for(
                span=wf_span,
                label="ai_guard.blocked",
                metric_type="categorical",
                value="true" if resp_verdict.blocked else "false",
            )
        except Exception as exc:  # noqa: BLE001
            # Older ddtrace versions used submit_evaluation(); try that.
            try:
                ctx = LLMObs.export_span(span=wf_span)
                LLMObs.submit_evaluation(
                    span_context=ctx,
                    label="answer.length",
                    metric_type="score",
                    value=float(len(full_answer)),
                )
            except Exception:  # noqa: BLE001
                log.debug("submit_evaluation unavailable (%s)", exc)
