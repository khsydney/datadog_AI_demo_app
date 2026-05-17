# index/embed.py
import os
from dotenv import load_dotenv
from langchain_huggingface import HuggingFaceEmbeddings  # ✅ new package

load_dotenv()

def make_embeddings():
    """
    Examples:
      EMBEDDING_MODEL=all-MiniLM-L6-v2         (384-dim)
      EMBEDDING_MODEL=bge-small-en-v1.5        (384-dim)
      EMBEDDING_MODEL=bge-base-en-v1.5         (768-dim)
      EMBEDDING_MODEL=bge-large-en-v1.5        (1024-dim)
    """
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

    return HuggingFaceEmbeddings(
        model_name=model_name,
        encode_kwargs={"normalize_embeddings": True},  # optional
    )
