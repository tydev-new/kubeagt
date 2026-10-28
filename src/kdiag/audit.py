"""Session audit log. Every kubectl command and every LLM call is recorded as one JSON line."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional


class AuditLog:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        rec.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


def default_session_dir() -> Path:
    base = Path(os.environ.get("KDIAG_HOME", Path.home() / ".kdiag")) / "sessions"
    d = base / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


NULL_AUDIT = AuditLog(None)
