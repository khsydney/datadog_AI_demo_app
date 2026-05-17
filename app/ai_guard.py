# app/ai_guard.py
"""
Datadog AI Guard wrapper with a local heuristic fallback.

Datadog AI Guard is a Preview product that ships inside `ddtrace`
(ddtrace.appsec.ai_guard). It needs:

  * DD_AI_GUARD_ENABLED=true
  * DD_API_KEY + DD_APP_KEY (the app key needs the `ai_guard_evaluate` scope)
  * Feature flag turned on for your Datadog org (request access via support)
  * The Datadog Agent reachable from this process

If any of those are missing — for example, while waiting for Datadog to
flip the Preview feature flag on your org — we fall back to a small local
heuristic guard so the demo still produces "blocked" verdicts and the
LLM Observability traces still get tagged with the guard outcome.

Public surface used here, in order of preference (the SDK has shifted
across recent ddtrace releases — we try all three so the wrapper keeps
working when users upgrade ddtrace):

    1. ddtrace.appsec.ai_guard.new_ai_guard_client()      # ≥4.7 ish
    2. ddtrace.appsec.ai_guard.AIGuardClient()            # earlier
    3. ddtrace.appsec.ai_guard.evaluate(messages=...)     # module-level fallback

Response schema (per https://docs.datadoghq.com/security/ai_guard/):

    action ∈ {"ALLOW", "DENY", "ABORT"}
    reason: str
    tags: list[str]                # e.g. ["prompt_injection", "jailbreak"]
    tag_probs: dict[str, float]    # ddtrace ≥ 4.8

ABORT verdicts raise AIGuardAbortError on the SDK side. We catch it and
synthesise a deterministic verdict so callers don't have to know about
that exception class.

Site support: AI Guard runs on US1, US3, US5, EU, AP1 and AP2 — the only
commercial-tier exclusion is `app.ddog-gov.com` (US-FED / GovCloud).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("rag.aiguard")

# A handful of patterns that catch obvious jailbreak / prompt-injection /
# data-exfil attempts. This is intentionally simple — the real product
# (Datadog AI Guard) is far more capable. This is only a safety net for
# when AI Guard isn't reachable (Preview flag not yet flipped, network
# blip, transient API error, …).
_INJECTION_PATTERNS = [
    r"ignore (?:all )?(?:previous|prior|above) (?:instructions|prompts)",
    r"disregard (?:the )?(?:system|previous) (?:prompt|instructions)",
    r"you are now (?:in )?(?:dan|developer|jailbreak|god) mode",
    r"reveal (?:your|the) system prompt",
    r"print (?:your|the) (?:hidden )?instructions",
    r"act as if (?:there are )?no (?:rules|restrictions|guardrails)",
    r"forget (?:your|all) (?:rules|guidelines|safety)",
]
_EXFIL_PATTERNS = [
    r"(?i)(?:aws|gcp|azure)[_-]?(?:access[_-]?)?(?:secret|key|token)",
    r"(?i)password\s*[:=]",
    r"(?i)api[_-]?key\s*[:=]",
    r"-----BEGIN (?:RSA|EC|OPENSSH|DSA|PRIVATE) (?:PRIVATE )?KEY-----",
    r"\b(?:\d[ -]*?){13,16}\b",   # 13-16 digit credit-card-shaped numbers
]


@dataclass
class GuardVerdict:
    action: str                       # "ALLOW", "DENY", "ABORT"
    reason: str
    tags: list[str]
    source: str                       # "datadog-ai-guard" | "local-heuristic"
    tag_probs: dict = field(default_factory=dict)
    raw: Optional[dict] = None

    @property
    def blocked(self) -> bool:
        return self.action in ("DENY", "ABORT")


# ───────────────────────────── Datadog AI Guard ─────────────────────────────
_ai_guard_client = None
_ai_guard_evaluate_fn = None        # module-level evaluate() fallback
_AIGuardAbortError = None            # cached exception class (if available)
_ai_guard_init_attempted = False


def _try_init_ai_guard():
    """Lazily try to construct the Datadog AI Guard client.

    Three import paths are tried (factory → class → module function)
    because the public API has shifted across recent ddtrace releases.
    """
    global _ai_guard_client, _ai_guard_evaluate_fn, _AIGuardAbortError
    global _ai_guard_init_attempted
    if _ai_guard_init_attempted:
        return _ai_guard_client or _ai_guard_evaluate_fn
    _ai_guard_init_attempted = True

    if os.getenv("DD_AI_GUARD_ENABLED", "").lower() not in ("1", "true", "yes"):
        log.info("AI Guard not enabled (DD_AI_GUARD_ENABLED unset); using fallback.")
        return None
    if not os.getenv("DD_APP_KEY"):
        log.warning(
            "DD_APP_KEY missing — AI Guard requires an Application Key with "
            "the ai_guard_evaluate scope. Falling back to local heuristic guard."
        )
        return None

    # Capture the abort exception class up-front (best-effort).
    try:
        from ddtrace.appsec.ai_guard import AIGuardAbortError  # type: ignore
        _AIGuardAbortError = AIGuardAbortError
    except Exception:  # noqa: BLE001
        _AIGuardAbortError = None

    # Path 1: factory function (newer)
    try:
        from ddtrace.appsec.ai_guard import new_ai_guard_client  # type: ignore
        _ai_guard_client = new_ai_guard_client()
        log.info("Datadog AI Guard initialised via new_ai_guard_client().")
        return _ai_guard_client
    except Exception as exc:  # noqa: BLE001
        log.debug("new_ai_guard_client unavailable (%s)", exc)

    # Path 2: client class (earlier)
    try:
        from ddtrace.appsec.ai_guard import AIGuardClient  # type: ignore
        _ai_guard_client = AIGuardClient()
        log.info("Datadog AI Guard initialised via AIGuardClient().")
        return _ai_guard_client
    except Exception as exc:  # noqa: BLE001
        log.debug("AIGuardClient unavailable (%s)", exc)

    # Path 3: module-level evaluate()
    try:
        from ddtrace.appsec import ai_guard as _mod  # type: ignore
        if hasattr(_mod, "evaluate"):
            _ai_guard_evaluate_fn = _mod.evaluate
            log.info("Datadog AI Guard initialised via module-level evaluate().")
            return _ai_guard_evaluate_fn
    except Exception as exc:  # noqa: BLE001
        log.debug("module-level evaluate unavailable (%s)", exc)

    log.warning("Could not initialise Datadog AI Guard; using local heuristic fallback.")
    return None


def _datadog_evaluate(messages: list[dict]) -> Optional[GuardVerdict]:
    handle = _try_init_ai_guard()
    if handle is None:
        return None

    try:
        if _ai_guard_client is not None:
            result = _ai_guard_client.evaluate(messages=messages)
        else:
            result = _ai_guard_evaluate_fn(messages=messages)
    except Exception as exc:  # noqa: BLE001
        # If ddtrace itself raised AIGuardAbortError, that *is* the
        # verdict — the SDK is telling us the conversation must abort.
        if _AIGuardAbortError is not None and isinstance(exc, _AIGuardAbortError):
            return GuardVerdict(
                action="ABORT",
                reason=str(exc) or "AIGuardAbortError",
                tags=list(getattr(exc, "tags", []) or []),
                tag_probs=dict(getattr(exc, "tag_probs", {}) or {}),
                source="datadog-ai-guard",
            )
        # Any other error (network blip, auth, etc.) — degrade to the
        # local heuristic so the demo never breaks.
        log.warning("AI Guard evaluate() failed (%s); using heuristic fallback.", exc)
        return None

    action = str(getattr(result, "action", "ALLOW") or "ALLOW").upper()
    reason = str(getattr(result, "reason", "") or "")
    tags = list(getattr(result, "tags", []) or [])
    tag_probs = dict(getattr(result, "tag_probs", {}) or {})
    raw = getattr(result, "__dict__", None) or (result if isinstance(result, dict) else None)
    return GuardVerdict(
        action=action,
        reason=reason,
        tags=tags,
        tag_probs=tag_probs,
        source="datadog-ai-guard",
        raw=raw,
    )


# ───────────────────────────── Local fallback ───────────────────────────────
def _local_heuristic(text: str) -> GuardVerdict:
    matched_tags: list[str] = []
    for pat in _INJECTION_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            matched_tags.append("prompt_injection")
            break
    for pat in _EXFIL_PATTERNS:
        if re.search(pat, text):
            matched_tags.append("data_exfiltration")
            break
    if any(t in text.lower() for t in (
        "rm -rf", "drop table", ":(){:|:&};:", "shutdown -h", "format c:"
    )):
        matched_tags.append("destructive_tool_call")
    if matched_tags:
        return GuardVerdict(
            action="DENY",
            reason=f"local-heuristic matched: {', '.join(matched_tags)}",
            tags=matched_tags,
            source="local-heuristic",
        )
    return GuardVerdict(
        action="ALLOW",
        reason="",
        tags=[],
        source="local-heuristic",
    )


# ───────────────────────────── Public API ───────────────────────────────────
def evaluate_prompt(user_prompt: str, system_prompt: str = "") -> GuardVerdict:
    """Evaluate a user prompt before sending to the LLM.

    Latency note: on the critical path of /chat. AI Guard adds roughly
    300-800 ms before the first token is streamed back. The heuristic
    fallback is sub-millisecond.
    """
    messages = [
        {"role": "system", "content": system_prompt} if system_prompt else None,
        {"role": "user",   "content": user_prompt},
    ]
    messages = [m for m in messages if m]
    verdict = _datadog_evaluate(messages)
    if verdict is None:
        verdict = _local_heuristic(user_prompt)
    _emit_security_log("ai_guard.prompt", user_prompt, verdict)
    return verdict


def evaluate_response(assistant_text: str) -> GuardVerdict:
    """Evaluate an assistant response after streaming finishes.

    This runs *after* the user has already received the tokens, so it
    doesn't add to perceived latency — but a DENY/ABORT verdict here
    flags the workflow span and emits a SIEM log for post-hoc review.
    """
    messages = [{"role": "assistant", "content": assistant_text}]
    verdict = _datadog_evaluate(messages)
    if verdict is None:
        verdict = _local_heuristic(assistant_text)
    _emit_security_log("ai_guard.response", assistant_text, verdict)
    return verdict


# ───────────────────────────── Logging hook ─────────────────────────────────
# Emit a structured log so Cloud SIEM can run detections on guard events
# (e.g. spike of prompt-injection attempts, repeated DENY verdicts per IP).
_sec_log = logging.getLogger("security.ai_guard")


def _emit_security_log(evt: str, content: str, v: GuardVerdict) -> None:
    truncated = (content or "")[:280]
    _sec_log.info(
        json.dumps({
            "evt.name":               evt,
            "evt.outcome":            v.action.lower(),
            "ai_guard.action":        v.action,
            "ai_guard.tags":          v.tags,
            "ai_guard.tag_probs":     v.tag_probs,
            "ai_guard.source":        v.source,
            "ai_guard.reason":        v.reason,
            "ai_guard.content_sample": truncated,
        })
    )
