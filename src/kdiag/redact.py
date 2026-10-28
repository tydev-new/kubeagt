"""Redaction applied to everything that leaves the cluster (bundles, tool output sent to an LLM)."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"
SENSITIVE_NAME = re.compile(r"(pass(word)?|passwd|secret|token|api[_-]?key|private[_-]?key|credential|auth)", re.I)

_PATTERNS = [
    # key=value or key: value where key looks sensitive
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[A-Za-z0-9_.-]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"), r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\b(?:sk|xai|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), REDACTED),  # JWT
]


def redact_text(text: str) -> str:
    if not text:
        return text
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Redact a Kubernetes object tree in place-safe manner (returns a new tree)."""
    if isinstance(obj, dict):
        kind = obj.get("kind")
        out = {}
        for k, v in obj.items():
            if kind == "Secret" and k in ("data", "stringData"):
                out[k] = {key: REDACTED for key in (v or {})}
                continue
            if kind == "ConfigMap" and k in ("data", "binaryData"):
                out[k] = {key: (redact_text(val) if isinstance(val, str) else REDACTED) for key, val in (v or {}).items()}
                continue
            if k == "annotations" and isinstance(v, dict):
                v = {ak: ("<removed>" if ak == "kubectl.kubernetes.io/last-applied-configuration" else redact_text(str(av))) for ak, av in v.items()}
                out[k] = v
                continue
            if k == "env" and isinstance(v, list):
                out[k] = [_redact_env(e) for e in v]
                continue
            out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def _redact_env(e: Any) -> Any:
    if isinstance(e, dict) and "value" in e and SENSITIVE_NAME.search(str(e.get("name", ""))):
        e = dict(e)
        e["value"] = REDACTED
        return e
    return redact_obj(e)
