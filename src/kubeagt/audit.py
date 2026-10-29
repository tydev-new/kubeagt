"""Session audit log: one JSON line per event, in the standard envelope (schema_version 1).

Every event carries schema_version, ts, session_id, seq, event, fix_n, target, actor, outcome,
reason, and detail. fix_n and target are null when the event is not about one proposal. See
docs/design.html, section "Audit event schema". read_events() also reads version 0 files (the
format before the envelope) and upgrades each line, so old sessions and eval outputs stay readable.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
ACTORS = ("rules", "ai", "user", "kubeagt")
OUTCOMES = ("ok", "fail", "warn", "block", "skip", "n/a")
ENVELOPE = ("schema_version", "ts", "session_id", "seq", "event", "fix_n", "target", "actor", "outcome", "reason", "detail")
EVIDENCE_MAX_ITEMS = 10
EVIDENCE_MAX_CHARS = 300


def target_obj(kind: Optional[str], name: Optional[str], namespace: Optional[str]) -> Optional[dict]:
    if not kind and not name:
        return None
    return {"kind": kind, "name": name, "namespace": namespace}


def parse_target(t: Any, namespace: Optional[str] = None) -> Optional[dict]:
    """Accept a {kind,name,namespace} dict or a 'Kind/name' string."""
    if t is None or isinstance(t, dict):
        return t
    kind, _, name = str(t).partition("/")
    return target_obj(kind, name, namespace)


def cap_evidence(lines: List[str], items: int = EVIDENCE_MAX_ITEMS, chars: int = EVIDENCE_MAX_CHARS) -> List[str]:
    out = []
    for s in list(lines)[:items]:
        s = str(s)
        out.append(s if len(s) <= chars else s[: chars - 3] + "...")
    return out


def check_result(check: str, outcome: str, reason: str = "", evidence: Optional[List[str]] = None,
                 secs: float = 0.0, **extra: Any) -> dict:
    """The shared check-result shape for policy_check, dry_run, rollout_check, log_check, rule_recheck."""
    assert outcome in OUTCOMES, outcome
    d = {"check": check, "outcome": outcome, "reason": reason, "evidence": cap_evidence(evidence or []),
         "secs": round(float(secs), 2)}
    d.update(extra)
    return d


class AuditLog:
    def __init__(self, path: Optional[Path] = None, session_id: Optional[str] = None, namespace: Optional[str] = None):
        self.path = Path(path) if path else None
        self.session_id = session_id or (self.path.parent.name if self.path else "")
        self.namespace = namespace
        self.seq = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():   # appending to an existing file keeps seq monotonic
                self.seq = sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())

    def write(self, event: str, *, fix_n: Optional[int] = None, target: Any = None, actor: str = "kubeagt",
              outcome: str = "n/a", reason: str = "", **detail: Any) -> None:
        self.emit(event, detail, fix_n=fix_n, target=target, actor=actor, outcome=outcome, reason=reason)

    def emit(self, event: str, detail: Dict[str, Any], fix_n: Optional[int] = None, target: Any = None,
             actor: str = "kubeagt", outcome: str = "n/a", reason: str = "") -> None:
        """Like write(), with detail as a dict (so detail may itself hold keys such as outcome or reason)."""
        if not self.path:
            return
        assert actor in ACTORS and outcome in OUTCOMES, (actor, outcome)
        self.seq += 1
        rec = {"schema_version": SCHEMA_VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "session_id": self.session_id, "seq": self.seq, "event": event, "fix_n": fix_n,
               "target": parse_target(target, self.namespace), "actor": actor, "outcome": outcome,
               "reason": str(reason or ""), "detail": detail}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


# ------------------------------------------------------------------ reading (v1 and v0)
_V0_RENAME = {
    "fix_invalid": ("dry_run", "fail"), "fix_blocked": ("policy_check", "block"), "fix_repeat": ("proposal", "block"),
    "prompt_invalid": ("prompt", "warn"), "prompt_eof": ("prompt", "n/a"), "fix_applied": ("fix_apply", "ok"),
    "fix_apply_failed": ("fix_apply", "fail"), "fix_verified": ("fix_verify", "ok"),
    "fix_verify_failed": ("fix_verify", "fail"), "llm_error": ("llm_call", "fail"),
}
_V0_ACTOR = {"rule_check": "rules", "llm_call": "ai", "llm_error": "ai", "decision": "user", "prompt_invalid": "user",
             "prompt_eof": "user"}
_DECISION_OUTCOME = {"yes": "ok", "no": "fail", "skip": "skip", "quit": "n/a"}


def upgrade_v0(e: Dict[str, Any], session_id: str = "", seq: int = 0) -> Dict[str, Any]:
    """Turn one version 0 line (flat fields, old event names) into the version 1 envelope."""
    if "schema_version" in e:
        return e
    d = {k: v for k, v in e.items() if k not in ("ts", "event")}
    old = e.get("event", "")
    event, outcome = _V0_RENAME.get(old, (old, "n/a"))
    if old == "decision":
        outcome = _DECISION_OUTCOME.get(d.get("decision"), "n/a")
        d["answer"] = d.get("decision")
    elif old == "kubectl":
        outcome = "ok" if d.get("rc") == 0 else "fail"
    elif old == "policy_check":
        outcome = "ok" if d.get("allow", True) else "block"
    elif old == "session_end":
        outcome = "ok" if d.get("healthy") else "fail"
    elif old == "prompt_eof":
        d["eof"] = True
    if event == "fix_verify":
        d["result"] = "verified" if outcome == "ok" else "failed"
    fix_n = d.pop("n", None) if old not in ("llm_call",) else None
    tgt = parse_target(d.get("target"))
    return {"schema_version": 0, "ts": e.get("ts"), "session_id": session_id, "seq": seq, "event": event,
            "fix_n": fix_n, "target": tgt, "actor": _V0_ACTOR.get(old, "kubeagt"), "outcome": outcome,
            "reason": str(d.get("status") or d.get("reason") or d.get("error") or ""), "detail": d, "v0_event": old}


def read_events(path: Path) -> List[Dict[str, Any]]:
    """Read an audit.jsonl of either version; version 0 lines are upgraded to the envelope."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()):
        out.append(upgrade_v0(json.loads(line), path.parent.name, i + 1))
    return out


def tstr(t: Optional[dict]) -> str:
    """'Kind/name' for an envelope target."""
    return f"{t.get('kind')}/{t.get('name')}" if t else ""


def default_session_dir() -> Path:
    base = Path(os.environ.get("KUBEAGT_HOME", Path.home() / ".kubeagt")) / "sessions"
    d = base / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


NULL_AUDIT = AuditLog(None)
