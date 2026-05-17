# streamlit_app.py — full replacement (upload now POSTs to the API)
"""
Streamlit UI for chat-llm-rag (Datadog edition).

Changes vs. previous version:
  • Upload no longer writes to the *UI pod's* local filesystem.
    It POSTs multipart to rag-api → /docs/upload, which persists onto a PVC
    shared with the API container.
  • "Rebuild Index" no longer shells out to `python -m index.indexer`.
    It POSTs to rag-api → /docs/reindex, which reindexes in-process.
  • Adds a "Docs in store" list view so you can see what the server actually has.
"""
from __future__ import annotations

import os
import uuid

import httpx
import streamlit as st


# ── Page setup ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Chat RAG (Datadog)", layout="wide")
st.title("Chat LLM with RAG – Datadog")

if "sid" not in st.session_state:
    st.session_state.sid = f"ui-{uuid.uuid4()}"


# ── Sidebar ────────────────────────────────────────────────────────────────
default_api = os.getenv("API_BASE", "http://rag-api:8000")
api_base = st.sidebar.text_input("Backend base URL", value=default_api).rstrip("/")
st.sidebar.caption("FastAPI server exposing /chat, /docs/upload, /docs/reindex.")

col1, col2 = st.sidebar.columns(2)
with col1:
    if st.button("Test backend"):
        try:
            r = httpx.get(api_base + "/health", timeout=5)
            st.sidebar.success(f"OK {r.status_code}")
        except Exception as e:  # noqa: BLE001
            st.sidebar.error(f"{e}")
with col2:
    if st.button("Clear chat"):
        st.session_state.messages = [{"role": "assistant", "content": "Hi! Ask me anything."}]
        st.rerun()


# ── Docs section (server-side, via API) ────────────────────────────────────
st.sidebar.divider()
st.sidebar.subheader("Docs (ingest)")

uploaded = st.sidebar.file_uploader(
    "Upload PDFs / MD / TXT / HTML",
    type=["pdf", "md", "txt", "html", "htm"],
    accept_multiple_files=True,
    key="uploader",
)

if uploaded:
    files_part = [
        ("files", (up.name, up.getvalue(), up.type or "application/octet-stream"))
        for up in uploaded
    ]
    try:
        r = httpx.post(api_base + "/docs/upload", files=files_part, timeout=60)
        r.raise_for_status()
        data = r.json()
        st.sidebar.success(f"Uploaded {len(data['saved'])} file(s) → {data['dir']}")
    except Exception as e:  # noqa: BLE001
        st.sidebar.error(f"Upload failed: {e}")

if st.sidebar.button("Rebuild Index"):
    with st.spinner("Reindexing on the server…"):
        try:
            r = httpx.post(api_base + "/docs/reindex", timeout=600)
            r.raise_for_status()
            data = r.json()
            st.sidebar.success(
                f"Indexed {data.get('chunks', 0)} chunks "
                f"from {data.get('files', 0)} files."
            )
            if data.get("skipped"):
                st.sidebar.caption(f"Skipped: {data['skipped']}")
            if data.get("error"):
                st.sidebar.error(f"Error: {data['error']}")
        except Exception as e:  # noqa: BLE001
            st.sidebar.error(f"Reindex failed: {e}")

# Live list of what the server has
with st.sidebar.expander("Docs in store", expanded=False):
    try:
        r = httpx.get(api_base + "/docs/list", timeout=10)
        r.raise_for_status()
        files = r.json().get("docs", [])
        if not files:
            st.caption("(empty)")
        else:
            for f in files:
                cols = st.columns([5, 1])
                cols[0].write(f)
                if cols[1].button("🗑", key=f"del-{f}"):
                    try:
                        d = httpx.delete(api_base + f"/docs/{f}", timeout=10)
                        d.raise_for_status()
                        st.toast(f"Deleted {f}")
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Delete failed: {e}")
    except Exception as e:  # noqa: BLE001
        st.caption(f"(API unreachable: {e})")

st.sidebar.divider()
st.sidebar.caption("Backend pod must be running (Deployment: rag-api).")


# ── Chat state ─────────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant",
         "content": "Hi! Ask me anything. I'll search your docs with RAG and cite sources."}
    ]

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# ── Chat input → /chat (streamed) ──────────────────────────────────────────
def _stream_chat(question: str, session_id: str):
    payload = {"question": question, "session_id": session_id}
    with httpx.stream(
        "POST", api_base + "/chat", json=payload, timeout=None
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_text():
            if chunk:
                yield chunk


if prompt := st.chat_input("Ask about your documents…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        buf = ""
        try:
            for chunk in _stream_chat(prompt, st.session_state.sid):
                buf += chunk
                placeholder.markdown(buf + "▌")
            placeholder.markdown(buf)
        except Exception as e:  # noqa: BLE001
            buf = f"Backend error: {e}"
            placeholder.error(buf)
    st.session_state.messages.append({"role": "assistant", "content": buf})
