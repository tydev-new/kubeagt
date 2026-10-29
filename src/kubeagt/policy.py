"""Anti-pattern policy filter (skeleton).

A policy check looks at one proposed fix before the server dry run and before the human is asked,
and either allows it or blocks it with a reason. The planned checks are listed in docs/policy-plan.md.
The registry is empty for now, so every proposal is allowed; the call site and its audit event
("policy_check") are in place so checks can be added without touching the fix loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class PolicyContext:
    namespace: str
    context: str
    history: List[str] = field(default_factory=list)


@dataclass
class PolicyResult:
    allow: bool
    reason: str = ""
    check: str = ""
    level: str = "block"        # "block" stops the proposal; "warn" is shown to the human but allowed

    @classmethod
    def allowed(cls) -> "PolicyResult":
        return cls(True)


class PolicyCheck(Protocol):
    name: str

    def check(self, proposal: Any, live_object: Optional[dict], context: PolicyContext) -> PolicyResult:
        ...


REGISTRY: List[PolicyCheck] = []    # planned checks, none implemented yet


def evaluate(proposal: Any, live_object: Optional[dict], context: PolicyContext,
             checks: Optional[List[PolicyCheck]] = None) -> Dict[str, Any]:
    """Run every registered check. Returns {"allow", "reason", "checks_run", "warnings"}.
    The first blocking result wins; warnings are collected."""
    run = REGISTRY if checks is None else checks
    warnings: List[str] = []
    for c in run:
        r = c.check(proposal, live_object, context)
        if r.allow:
            continue
        if r.level == "warn":
            warnings.append(f"{c.name}: {r.reason}")
            continue
        return {"allow": False, "reason": f"{c.name}: {r.reason}", "checks_run": [x.name for x in run], "warnings": warnings}
    return {"allow": True, "reason": "", "checks_run": [x.name for x in run], "warnings": warnings}
