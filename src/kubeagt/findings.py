"""Data types shared by rules, the LLM agent, and the fixer."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional


@dataclass
class Fix:
    """One proposed change. It is only data: nothing here can touch a cluster."""
    kind: str
    name: str
    patch_type: str  # json | merge | strategic
    patch: Any
    summary: str
    source: str = "rule"  # rule | llm
    confidence: str = "high"  # high | medium | low

    def fingerprint(self) -> str:
        raw = json.dumps([self.kind.lower(), self.name, self.patch_type, self.patch], sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def kubectl_preview(self, namespace: str) -> str:
        return f"kubectl patch {self.kind.lower()} {self.name} -n {namespace} --type {self.patch_type} -p '{json.dumps(self.patch)}'"


@dataclass
class Finding:
    rule: str
    severity: str  # critical | warning | info
    kind: str
    name: str
    title: str
    diagnosis: str
    evidence: List[str] = field(default_factory=list)
    reasoning: str = ""
    fix: Optional[Fix] = None
    container: Optional[str] = None
    root_cause: bool = True
    symptom_of: Optional[str] = None

    @property
    def target(self) -> str:
        return f"{self.kind}/{self.name}"

    def key(self) -> str:
        return f"{self.rule}:{self.kind}/{self.name}:{self.container or ''}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target"] = self.target
        return d
