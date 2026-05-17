# app/security_demo.py
"""
Endpoints that intentionally produce security-relevant signal so Datadog
Cloud SIEM can detect on them. They emit structured JSON logs which the
Datadog Agent forwards via the K8s log tailer.

Detection rules to create in the Datadog UI (Security → Cloud SIEM →
Detection Rules):

  1. Brute-force login attempt
     Query:   @evt.name:user.login.failed
     Group by: @usr.id, @network.client.ip
     Threshold: > 5 in 5m   (Severity: Medium)

  2. Unauthorised admin access
     Query:   @evt.name:admin.access.unauthorized
     Group by: @network.client.ip
     Threshold: > 3 in 10m  (Severity: High)

  3. LLM prompt-injection burst
     Query:   @evt.name:ai_guard.prompt @ai_guard.action:(DENY OR ABORT)
     Group by: @network.client.ip
     Threshold: > 3 in 5m   (Severity: High)

  4. Suspicious file access
     Query:   @evt.name:file.download @file.path:(*etc/passwd* OR *.ssh*)
     Threshold: any         (Severity: Critical)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/demo/security", tags=["security-demo"])

# Dedicated security logger — the Agent picks it up off stdout in K8s.
sec_log = logging.getLogger("security.app")


def _client_ip(req: Request) -> str:
    # Honour the K8s-typical X-Forwarded-For chain if present.
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def _emit(event: dict) -> None:
    event.setdefault("@timestamp", datetime.now(timezone.utc).isoformat())
    sec_log.info(json.dumps(event))


# ───────────────────────────── /login ──────────────────────────────────────
class LoginPayload(BaseModel):
    username: str
    password: str


@router.post("/login")
def fake_login(payload: LoginPayload, req: Request):
    """Always fails. Generates @evt.name:user.login.failed events."""
    _emit({
        "evt.name":             "user.login.failed",
        "evt.outcome":          "failure",
        "usr.id":               payload.username,
        "network.client.ip":    _client_ip(req),
        "http.user_agent":      req.headers.get("user-agent", ""),
        "service":              "rag-api",
        "auth.method":          "password",
        "reason":               "invalid_credentials",
    })
    raise HTTPException(status_code=401, detail="invalid credentials")


# ───────────────────────────── /admin/users ────────────────────────────────
ADMIN_TOKEN = "demo-admin-token-do-not-use-in-prod"


@router.get("/admin/users")
def list_users(req: Request, x_admin_token: Optional[str] = Header(default=None)):
    """Requires X-Admin-Token header. Wrong/missing token => SIEM event."""
    if x_admin_token != ADMIN_TOKEN:
        _emit({
            "evt.name":         "admin.access.unauthorized",
            "evt.outcome":      "failure",
            "network.client.ip": _client_ip(req),
            "http.user_agent":  req.headers.get("user-agent", ""),
            "service":          "rag-api",
            "resource":         "/demo/security/admin/users",
            "token_provided":   bool(x_admin_token),
        })
        raise HTTPException(status_code=403, detail="forbidden")
    return {"users": ["alice", "bob", "carol"]}


# ───────────────────────────── /files/download ─────────────────────────────
SUSPICIOUS_PATTERNS = ("/etc/passwd", "/etc/shadow", "/.ssh/", ".aws/credentials", "private.key")


@router.get("/files/download")
def download_file(path: str, req: Request):
    """Logs every download attempt; flags suspicious paths."""
    suspicious = any(p in path for p in SUSPICIOUS_PATTERNS)
    _emit({
        "evt.name":          "file.download",
        "evt.outcome":       "success" if not suspicious else "suspicious",
        "network.client.ip": _client_ip(req),
        "file.path":         path,
        "file.suspicious":   suspicious,
        "service":           "rag-api",
    })
    if suspicious:
        raise HTTPException(status_code=403, detail="path not allowed")
    return {"ok": True, "path": path}


# ───────────────────────────── /demo/iam_misconfig ─────────────────────────
@router.get("/iam-misconfig")
def iam_misconfig(req: Request):
    """
    Emits a CIEM-style log entry as if the service had detected an
    overly-permissive IAM policy in its own configuration.
    Useful for stitching Cloud SIEM signals together with Datadog
    Cloud Security's CIEM findings in dashboards.
    """
    _emit({
        "evt.name":          "iam.policy.overly_permissive",
        "evt.outcome":       "alert",
        "network.client.ip": _client_ip(req),
        "service":           "rag-api",
        "iam.principal":     "arn:aws:iam::123456789012:role/eks-demo-app",
        "iam.actions":       ["s3:*", "iam:*", "ec2:*"],
        "iam.resource":      "*",
        "compliance.framework": "CIS-AWS-1.5",
    })
    return {"detected": "overly_permissive_iam_policy"}

# Add to app/security_demo.py — DO NOT use in production
import sqlite3
@router.get("/sqli-demo")
def sqli_demo(name: str, req: Request):
    """Intentionally vulnerable — demos IAST detection."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE users (name TEXT, email TEXT)")
    cur.execute("INSERT INTO users VALUES ('alice', 'a@x.com')")
    # ❌ tainted concatenation — IAST will flag this
    cur.execute(f"SELECT * FROM users WHERE name = '{name}'")
    return {"rows": cur.fetchall()}