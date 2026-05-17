# index/loader.py
from pathlib import Path
from typing import List
from langchain_community.document_loaders import PyPDFLoader, UnstructuredFileLoader

SUPPORTED = {".pdf", ".md", ".txt", ".html", ".htm"}

def load_docs(root: str = "data/docs"):
    root_path = Path(root)
    if not root_path.exists():
        print(f"[loader] root not found: {root_path.resolve()}")
        return []

    docs = []
    seen = 0
    for p in root_path.rglob("*"):
        if p.is_dir():
            continue
        seen += 1
        ext = p.suffix.lower()
        if ext not in SUPPORTED:
            print(f"[loader] skip (unsupported): {p}")
            continue
        try:
            if ext == ".pdf":
                # Try fast text extraction first
                loaded = PyPDFLoader(str(p)).load()
            else:
                loaded = UnstructuredFileLoader(str(p)).load()
            docs.extend(loaded)
            print(f"[loader] loaded {len(loaded):>3} chunks from {p}")
        except Exception as e:
            print(f"[loader] ERROR loading {p}: {e}")
    print(f"[loader] scanned files: {seen}, total chunks: {len(docs)}")
    return docs
