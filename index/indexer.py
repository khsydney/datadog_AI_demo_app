# index/indexer.py
"""
Indexer for the RAG store. Can be used either:

  - As a CLI / Job:           python -m index.indexer
  - In-process from the API:  from index.indexer import reindex_directory

The collection schema uses Milvus's `enable_dynamic_field=True`, so any extra
metadata coming out of LangChain loaders (page, page_label, creator, etc.)
becomes a dynamic field automatically and matches the schema produced by the
original Job-based indexer.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List

from pymilvus import MilvusClient, DataType

from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    UnstructuredHTMLLoader,
    UnstructuredMarkdownLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings

EMB_MODEL     = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
MILVUS_URI    = os.getenv("MILVUS_URI", "http://milvus:19530")
COLL          = os.getenv("MILVUS_COLLECTION", "rag_chunks")
DIM           = int(os.getenv("EMBEDDING_DIM", "1024"))   # BGE-m3 = 1024
CHUNK_SIZE    = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
DEFAULT_DIR   = os.getenv("DOC_STORE_DIR", "/data/docs")

_LOADERS = {
    ".pdf":  PyPDFLoader,
    ".txt":  TextLoader,
    ".md":   UnstructuredMarkdownLoader,
    ".html": UnstructuredHTMLLoader,
    ".htm":  UnstructuredHTMLLoader,
}


# ─────────────────────────── Collection bootstrap ──────────────────────────
def _ensure_collection(client: MilvusClient) -> None:
    if client.has_collection(COLL):
        return
    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field("pk",     DataType.INT64,        is_primary=True)
    schema.add_field("text",   DataType.VARCHAR,      max_length=8192)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=DIM)
    schema.add_field("source", DataType.VARCHAR,      max_length=512)

    idx = client.prepare_index_params()
    idx.add_index(field_name="vector", metric_type="IP", index_type="AUTOINDEX")
    client.create_collection(collection_name=COLL, schema=schema, index_params=idx)


# ─────────────────────────── Main entrypoint ───────────────────────────────
def reindex_directory(
    docs_dir: str | None = None,
    *,
    embedding_fn: Callable[[List[str]], List[List[float]]] | None = None,
    purge_collection: bool = False,
) -> dict:
    """
    Load every supported file under `docs_dir`, chunk, embed, upsert into
    Milvus. Returns a summary dict.

    `embedding_fn`: optional pre-loaded embedder (e.g. `emb.embed_documents`
    from rag_pipeline) so we don't pay the BGE-m3 cold-start cost twice.

    `purge_collection`: if True, drop the collection before reindexing.
    Default False — additive upserts (good for "add new doc, reindex" UX).
    """
    docs_dir = docs_dir or DEFAULT_DIR
    root = Path(docs_dir)
    if not root.exists():
        return {"files": 0, "chunks": 0, "skipped": [], "error": f"{docs_dir} not found"}

    # 1. Load everything we can
    docs = []
    files_seen = 0
    skipped: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        loader_cls = _LOADERS.get(p.suffix.lower())
        if not loader_cls:
            skipped.append(f"{p.name} (unsupported ext)")
            continue
        files_seen += 1
        try:
            for d in loader_cls(str(p)).load():
                d.metadata["source"] = p.name
                docs.append(d)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{p.name}: {e}")

    if not docs:
        return {"files": files_seen, "chunks": 0, "skipped": skipped}

    # 2. Chunk
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks = splitter.split_documents(docs)

    # 3. Embed
    if embedding_fn is None:
        emb = HuggingFaceEmbeddings(model_name=EMB_MODEL)
        embedding_fn = emb.embed_documents
    vectors = embedding_fn([c.page_content for c in chunks])

    # 4. Upsert
    client = MilvusClient(uri=MILVUS_URI)
    if purge_collection and client.has_collection(COLL):
        client.drop_collection(COLL)
    _ensure_collection(client)

    rows = [
        {
            "text":   c.page_content,
            "vector": v,
            "source": c.metadata.get("source", ""),
            # dynamic fields — page, page_label, creator, producer, etc.
            **{k: v2 for k, v2 in c.metadata.items() if k != "source"},
        }
        for c, v in zip(chunks, vectors)
    ]
    client.insert(collection_name=COLL, data=rows)

    return {"files": files_seen, "chunks": len(chunks), "skipped": skipped}


if __name__ == "__main__":
    summary = reindex_directory()
    print(f"Index built and {summary['chunks']} chunks loaded from {summary['files']} files.")
    if summary.get("skipped"):
        print("Skipped:", summary["skipped"])
    if summary.get("error"):
        print("Error:", summary["error"])
