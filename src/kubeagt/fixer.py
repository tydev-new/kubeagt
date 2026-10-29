"""The approval-gated fix loop.

collect, check, propose ONE fix, ask the human, apply through KubeWriter, verify, repeat.
"""
from __future__ import annotations

import json
import textwrap
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Set

from .agent import Budget, Investigator, Proposal
from .audit import NULL_AUDIT, AuditLog
from .bundle import Bundle, KIND_FILE
from .findings import Finding, Fix
from .health import health, wait_healthy
from .patching import PatchError, apply_patch, spec_diff
from .redact import redact_obj, redact_text
from .rules import MIN_MEMORY_MIB, run_rules

RULE = "=" * 78
ANSWERS = {"y": "yes", "yes": "yes", "n": "no", "no": "no", "s": "skip", "skip": "skip", "q": "quit", "quit": "quit"}


def tty_ask(prompt: str) -> str:
    while True:
        try:
            a = input(prompt).strip().lower()
        except EOFError:
            return "quit"
        if a in ANSWERS:
            return ANSWERS[a]
        print("  please answer y, n, s, or q")


@dataclass
class FixRecord:
    n: int
    source: str
    rule: Optional[str]
    target: str
    summary: str
    patch_type: str
    patch: object
    decision: str
    applied: bool = False
    verified: bool = False
    status: str = ""


@dataclass
class SessionReport:
    namespace: str
    context: str
    initial_findings: List[dict] = field(default_factory=list)
    fixes: List[FixRecord] = field(default_factory=list)
    tool_steps: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0
    healthy: bool = False
    problems: List[str] = field(default_factory=list)
    remaining_findings: List[dict] = field(default_factory=list)
    seconds: float = 0.0
    stopped: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _w(text: str, indent: str = "", first: str = "") -> str:
    return textwrap.fill(text, width=100, initial_indent=first or indent, subsequent_indent=indent)


class FixSession:
    def __init__(self, namespace: str, context: str, snapshot: Callable[[], Bundle], writer, kubectl,
                 audit: AuditLog = NULL_AUDIT, llm=None, llm_cfg=None, budget: Optional[Budget] = None,
                 ask: Callable[[str], str] = tty_ask, yes: bool = False, out: Callable[[str], None] = print,
                 max_fixes: int = 10, verify_timeout: float = 120, settle_timeout: float = 15,
                 min_memory_mib: float = MIN_MEMORY_MIB, dry_run_validate: bool = True,
                 health_snapshot: Optional[Callable[[], Bundle]] = None, sleep: Callable[[float], None] = time.sleep):
        self.ns, self.context = namespace, context
        self.snapshot, self.writer, self.kc = snapshot, writer, kubectl
        self.health_snapshot = health_snapshot or snapshot
        self.audit, self.llm, self.llm_cfg = audit, llm, llm_cfg
        self.budget = budget or Budget()
        self.ask, self.yes, self.out = ask, yes, out
        self.max_fixes, self.verify_timeout, self.settle_timeout = max_fixes, verify_timeout, settle_timeout
        self.min_memory_mib = min_memory_mib
        self.dry_run_validate = dry_run_validate
        self.sleep = sleep
        self.handled: Set[str] = set()      # fix fingerprints already decided or attempted
        self.skipped: Set[str] = set()      # finding keys the user skipped
        self.history: List[str] = []        # fed back to the LLM
        self.llm_idle_state: Optional[frozenset] = None

    # ------------------------------------------------------------ helpers
    def _check(self) -> tuple:
        b = self.snapshot()
        return b, run_rules(b, self.min_memory_mib)

    def _validate(self, fix: Fix) -> Optional[str]:
        if not self.dry_run_validate:
            return None
        cp = self.writer.patch(fix.kind, fix.name, fix.patch_type, fix.patch, dry_run=True)
        return None if cp.returncode == 0 else redact_text((cp.stderr or cp.stdout or "").strip())[:400]

    def _after(self, bundle: Bundle, fix: Fix):
        before = bundle.get(KIND_FILE.get(fix.kind, fix.kind.lower() + "s"), fix.name)
        after, err = None, None
        if self.dry_run_validate:
            cp = self.writer.patch(fix.kind, fix.name, fix.patch_type, fix.patch, dry_run=True)
            if cp.returncode != 0:
                return before, None, redact_text((cp.stderr or cp.stdout).strip())[:400]
            try:
                after = redact_obj(json.loads(cp.stdout))
            except json.JSONDecodeError:
                after = None
        if after is None and before is not None:
            try:
                after = apply_patch(before, fix.patch_type, fix.patch)
            except (PatchError, KeyError, IndexError, ValueError) as e:
                err = f"patch does not apply: {e}"
        return before, after, err

    def _llm_task(self, findings: List[Finding], candidates: List[Finding]) -> str:
        fdesc = [{"rule": f.rule, "target": f.target, "container": f.container, "root_cause": f.root_cause,
                  "title": f.title, "evidence": f.evidence[:6]} for f in findings[:15]]
        hints = [{"rule": f.rule, "kind": f.fix.kind, "name": f.fix.name, "patch_type": f.fix.patch_type, "patch": f.fix.patch,
                  "summary": f.fix.summary, "diagnosis": f.diagnosis, "evidence": f.evidence[:6]} for f in candidates]
        parts = [f"Namespace: {self.ns}",
                 "Deterministic findings (JSON):", json.dumps(fdesc, indent=1),
                 "<candidate_fixes>", json.dumps(hints), "</candidate_fixes>"]
        if self.history:
            parts += ["Session history so far (most recent last):"] + [f"- {h}" for h in self.history[-10:]]
        parts.append("Investigate as needed, then call propose_fix with ONE fix not already tried, or finish.")
        return "\n".join(parts)

    # ------------------------------------------------------------ main loop
    def run(self) -> SessionReport:
        t0 = time.time()
        rep = SessionReport(self.ns, self.context)
        bundle, findings = self._check()
        rep.initial_findings = [f.to_dict() for f in findings]
        self._print_findings(findings)
        n = 0
        while n < self.max_fixes:
            roots = [f for f in findings if f.root_cause]
            cands = [f for f in roots if f.fix and f.fix.fingerprint() not in self.handled and f.key() not in self.skipped]
            proposal: Optional[Proposal] = None
            finding: Optional[Finding] = None
            state = frozenset(f.key() for f in roots if f.key() not in self.skipped)
            if self.llm is not None and state and state != self.llm_idle_state:
                self.out(f"\nInvestigating with LLM ({getattr(self.llm, 'name', 'llm')}), read-only tools only...")
                inv = Investigator(self.llm, self.kc, self.audit, self.budget, self.llm_cfg, validate=self._validate, log=self.out)
                res = inv.investigate(self._llm_task(findings, cands))
                rep.tool_steps += res.tool_steps
                if res.proposal and res.proposal.fix.fingerprint() not in self.handled:
                    proposal = res.proposal
                    finding = self._match(roots, proposal)
                else:
                    self.llm_idle_state = state
                    if res.summary:
                        self.out(_w("LLM: " + res.summary, "     "))
                    if res.stopped and res.stopped != "finished":
                        self.out(f"LLM investigation stopped: {res.stopped}")
            if proposal is None and cands:
                finding = cands[0]
                proposal = Proposal(finding.fix, finding.diagnosis, finding.evidence, finding.reasoning, finding.rule)
            if proposal is None:
                break
            n += 1
            fix = proposal.fix
            self.handled.add(fix.fingerprint())
            before, after, err = self._after(bundle, fix)
            rec = FixRecord(n, fix.source, proposal.rule or (finding.rule if finding else None), f"{fix.kind}/{fix.name}",
                            fix.summary, fix.patch_type, fix.patch, decision="")
            if err:
                rec.decision, rec.status = "invalid", f"patch rejected by dry run: {err}"
                self.out(f"\nSkipping a proposed fix for {rec.target}: {rec.status}")
                self.history.append(f"fix '{fix.summary}' on {rec.target} was rejected by server dry run: {err}")
                self.audit.write("fix_invalid", **_rec_dict(rec))
                rep.fixes.append(rec)
                continue
            self._print_proposal(n, proposal, finding, before, after, len(cands))
            decision = "yes" if self.yes else ANSWERS.get(
                str(self.ask("Apply this fix? [y]es / [n]o / [s]kip / [q]uit: ")).strip().lower(), "quit")
            if self.yes:
                self.out("Apply this fix? [y]es / [n]o / [s]kip / [q]uit: y   (auto-approved by --yes)")
            rec.decision = decision
            self.audit.write("decision", **_rec_dict(rec))
            if decision == "quit":
                rep.fixes.append(rec)
                rep.stopped = "user quit"
                break
            if decision == "skip":
                if finding:
                    self.skipped.add(finding.key())
                self.history.append(f"user skipped the issue on {rec.target} ({rec.rule}); leave it alone")
                rep.fixes.append(rec)
                continue
            if decision == "no":
                self.history.append(f"user rejected fix '{fix.summary}' on {rec.target}; propose an alternative only if clearly better")
                rep.fixes.append(rec)
                continue
            self._apply_and_verify(rec, fix, finding, cands)
            rep.fixes.append(rec)
            bundle, findings = self._check()
        else:
            rep.stopped = f"fix cap reached ({self.max_fixes})"

        applied = any(r.applied for r in rep.fixes)
        if applied:
            self.out(f"\nWaiting up to {int(self.verify_timeout)}s for the namespace to become healthy...")
            ok, problems, _ = wait_healthy(self.health_snapshot, self.verify_timeout, 5, self.sleep)
            bundle, findings = self._check()
        else:
            ok, problems = health(bundle)
        rep.healthy, rep.problems = ok, problems
        rep.remaining_findings = [f.to_dict() for f in findings if f.root_cause]
        rep.llm_calls = self.budget.llm_calls
        rep.llm_tokens = self.budget.prompt_tokens + self.budget.completion_tokens
        rep.llm_cost_usd = round(self.budget.cost_usd, 5)
        rep.seconds = round(time.time() - t0, 1)
        self._print_summary(rep)
        self.audit.write("session_end", healthy=ok, problems=problems, fixes=[_rec_dict(r) for r in rep.fixes], seconds=rep.seconds)
        return rep

    def _match(self, roots: List[Finding], p: Proposal) -> Optional[Finding]:
        target = (p.fix.kind.lower(), p.fix.name)
        for f in roots:
            if (f.kind.lower(), f.name) == target and p.rule and f.rule == p.rule:
                return f
        for f in roots:
            if f.fix and f.fix.fingerprint() == p.fix.fingerprint():
                return f
        for f in roots:
            if (f.kind.lower(), f.name) == target:
                return f
        return None

    def _apply_and_verify(self, rec: FixRecord, fix: Fix, finding: Optional[Finding], cands: List[Finding]) -> None:
        cp = self.writer.patch(fix.kind, fix.name, fix.patch_type, fix.patch)
        if cp.returncode != 0:
            rec.status = "apply failed: " + redact_text((cp.stderr or cp.stdout).strip())[:300]
            self.out(f"  {rec.status}")
            self.history.append(f"applying '{fix.summary}' failed: {rec.status}")
            self.audit.write("fix_apply_failed", **_rec_dict(rec))
            return
        rec.applied = True
        self.out(f"  applied: {(cp.stdout or '').strip()}")
        self.audit.write("fix_applied", **_rec_dict(rec))
        pending_same = [f for f in cands if f.target == rec.target and f.fix and f.fix.fingerprint() != fix.fingerprint()]
        timeout = self.settle_timeout if pending_same else self.verify_timeout
        self.out(f"  verifying: waiting up to {int(timeout)}s for rollout of {rec.target}...")
        rs = self.writer.rollout_status(fix.kind, fix.name, timeout=int(timeout))
        b, nf = self._check()
        still = bool(finding) and any(f.key() == finding.key() for f in nf)
        target_issues = [f for f in nf if f.target == rec.target and f.root_cause]
        if still:
            rec.status = f"not resolved: rule {finding.rule} still fires on {rec.target}"
        elif rs.returncode == 0:
            rec.verified = True
            rec.status = "verified: rollout complete" + ("" if not target_issues else f"; {len(target_issues)} other issue(s) remain")
        elif pending_same or target_issues:
            rec.verified = True
            rec.status = ("spec issue resolved; rollout is still blocked by other issue(s): "
                          + ", ".join(sorted({f.rule for f in target_issues}) or {"pending"}))
        else:
            rec.status = "applied, but rollout did not complete: " + redact_text((rs.stderr or rs.stdout or "").strip())[:200]
        self.out(f"  {rec.status}")
        if not rec.verified:
            new_state = "; ".join(f"{f.rule} on {f.target}: {f.title}" for f in nf if f.root_cause) or "no rule findings"
            self.history.append(f"applied '{fix.summary}' on {rec.target} but verification failed ({rec.status}). New state: {new_state}")
        else:
            self.history.append(f"applied and verified '{fix.summary}' on {rec.target}")
        self.audit.write("fix_verified" if rec.verified else "fix_verify_failed", **_rec_dict(rec))

    # ------------------------------------------------------------ output
    def _print_findings(self, findings: List[Finding]) -> None:
        roots = [f for f in findings if f.root_cause]
        sym = [f for f in findings if not f.root_cause]
        self.out(f"kubeagt: namespace {self.ns}, context {self.context}")
        if not findings:
            self.out("No problems found.")
            return
        self.out(f"Found {len(roots)} problem(s)" + (f" and {len(sym)} related symptom(s)" if sym else "") + ":")
        for f in roots:
            self.out(f"  [{f.severity}] {f.target}{(' container ' + f.container) if f.container else ''}: {f.title}"
                     + ("" if f.fix else "  (no automatic fix)"))
        for f in sym:
            self.out(f"  [symptom] {f.target}: {f.title}")

    def _print_proposal(self, n: int, p: Proposal, finding: Optional[Finding], before, after, n_cands: int) -> None:
        fix = p.fix
        src = (f"deterministic rule {p.rule}" if fix.source == "rule" else f"LLM ({getattr(self.llm, 'name', 'llm')})"
               + (f", addresses rule {p.rule}" if p.rule else ""))
        o = self.out
        o("")
        o(RULE)
        o(f"Proposed fix {n}   source: {src}   confidence: {fix.confidence}")
        o(f"Target:    {fix.kind}/{fix.name}   namespace: {self.ns}   context: {self.context}")
        if finding:
            o(f"Problem:   {finding.title}")
        o(_w(p.diagnosis, "           ", "Diagnosis: "))
        if p.evidence:
            o("Evidence:")
            for e in p.evidence[:8]:
                o(_w(e, "      ", "  - "))
        o(f"Change:    {fix.summary}")
        d = spec_diff(before or {}, after or {}, f"{fix.kind}/{fix.name}") if after is not None else "(no preview available)"
        for line in d.splitlines():
            o("    " + line)
        o(_w(p.reasoning, "           ", "Why:       "))
        o(f"Command:   {fix.kubectl_preview(self.ns)}")
        o(RULE)

    def _print_summary(self, rep: SessionReport) -> None:
        o = self.out
        o("")
        o("Summary")
        if not rep.fixes:
            o("  no fixes proposed")
        for r in rep.fixes:
            o(f"  {r.n}. {r.target}: {r.summary}  [{r.decision}{', applied' if r.applied else ''}]  {r.status}")
        o(f"  namespace healthy: {'yes' if rep.healthy else 'no'}")
        for p in rep.problems[:8]:
            o(f"    - {p}")
        left = [f for f in rep.remaining_findings]
        if left:
            o("  open findings:")
            for f in left[:8]:
                o(f"    - {f['target']}: {f['title']}")
        if rep.llm_calls:
            o(f"  LLM: {rep.llm_calls} call(s), {rep.llm_tokens} tokens, est. ${rep.llm_cost_usd:.4f}, {rep.tool_steps} read-only tool call(s)")


def _rec_dict(r: FixRecord) -> dict:
    d = asdict(r)
    d["patch"] = json.loads(redact_text(json.dumps(r.patch)))
    return d
