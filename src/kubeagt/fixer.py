"""The approval-gated fix loop.

Follows the flow chart in docs/design.html: collect and check, ask the AI or take the next rule fix,
server dry run, ask the human, apply through KubeWriter, verify. Every outcome is recorded in the
session history (fed to the AI) and the audit log. After a rejected dry run, n, skip, or a repeated
patch nothing changed, so the loop goes straight back to "Any errors left?"; after an apply attempt
(ok or failed) it re-collects and re-runs the rules once.
"""
from __future__ import annotations

import json
import textwrap
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Set

from .agent import Budget, Investigator, Proposal
from .audit import NULL_AUDIT, AuditLog, check_result
from . import logcheck
from .bundle import Bundle, KIND_FILE
from .findings import Finding, Fix
from .health import health, wait_healthy
from . import policy
from .patching import PatchError, apply_patch, spec_diff
from .redact import redact_obj, redact_text
from .rules import MIN_MEMORY_MIB, run_rules

RULE = "=" * 78
ANSWERS = {"y": "yes", "yes": "yes", "n": "no", "no": "no", "s": "skip", "skip": "skip", "q": "quit", "quit": "quit"}


PROMPT = "Apply this fix? [y]es / [n]o / [s]kip / [q]uit: "
MAX_INVALID = 5


def tty_ask(prompt: str) -> str:
    """Read one raw line from the terminal. Raises EOFError on Ctrl-D or closed stdin."""
    return input(prompt)


def scripted_ask(answers: List[str]) -> Callable[[str], str]:
    """Feed a fixed answer sequence to the prompt, as if typed. When the list runs out it behaves
    like Ctrl-D (EOFError). Used by the human-response evals and tests; not exposed on the CLI."""
    queue = list(answers)

    def ask(prompt: str) -> str:
        if not queue:
            raise EOFError
        return str(queue.pop(0))
    return ask


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
    verified: bool = False          # true for verified and verified_with_warnings
    status: str = ""
    verify_result: str = ""         # verified | verified_with_warnings | failed (empty when not applied)


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
                 health_snapshot: Optional[Callable[[], Bundle]] = None, sleep: Callable[[float], None] = time.sleep,
                 max_proposals: Optional[int] = None, policy_checks: Optional[list] = None,
                 log_source=None, log_watch_seconds: float = logcheck.DEFAULT_WINDOW):
        self.ns, self.context = namespace, context
        self.snapshot, self.writer, self.kc = snapshot, writer, kubectl
        self.health_snapshot = health_snapshot or snapshot
        self.audit, self.llm, self.llm_cfg = audit, llm, llm_cfg
        if audit.path and audit.namespace is None:
            audit.namespace = namespace
        self.budget = budget or Budget()
        self.ask, self.yes, self.out = ask, yes, out
        # max_fixes caps applied fixes (apply attempts). max_proposals is a separate guard so a model that keeps
        # proposing new patches that are rejected, declined, or repeated cannot loop forever.
        self.max_fixes, self.verify_timeout, self.settle_timeout = max_fixes, verify_timeout, settle_timeout
        self.max_proposals = max_proposals if max_proposals is not None else max(3 * max_fixes, 10)
        self.min_memory_mib = min_memory_mib
        self.dry_run_validate = dry_run_validate
        self.sleep = sleep
        self.policy_checks = policy_checks          # None means the default registry (empty for now)
        # Log check source: the real read-only kubectl by default. Test doubles without logs get none (check skipped).
        self.log_source = log_source if log_source is not None else (
            logcheck.KubeLogSource(kubectl) if hasattr(kubectl, "fetch_logs") else None)
        self.log_watch_seconds = log_watch_seconds
        self.handled: Set[str] = set()      # fix fingerprints already decided or attempted
        self.skipped: Set[str] = set()      # finding keys the user skipped
        self.history: List[str] = []        # session history, fed back to the LLM
        self.dry_run_cache: dict = {}       # fix fingerprint -> CompletedProcess of its one server dry run
        self.llm_idle_state: Optional[frozenset] = None
        self._cur_n: Optional[int] = None    # proposal being prompted, for prompt events
        self._cur_t: Optional[str] = None

    def _ev(self, event: str, rec: "FixRecord", actor: str = "kubeagt", outcome: str = "n/a", reason: str = "",
            check: Optional[dict] = None, **extra) -> None:
        """Write one event about a proposal: the envelope plus the fix record (and a check result) as detail."""
        d = _rec_dict(rec)
        d.pop("n", None)
        d.pop("target", None)
        if check is not None:
            outcome, reason = check["outcome"], check["reason"]
            d.update(check)
        d.update(extra)
        self.audit.emit(event, d, fix_n=rec.n, target=rec.target, actor=actor, outcome=outcome, reason=reason)

    # ------------------------------------------------------------ helpers
    def _check(self, final: bool = False) -> tuple:
        b = self.snapshot()
        found = run_rules(b, self.min_memory_mib)
        roots = sorted(f.key() for f in found if f.root_cause)
        self.audit.write("rule_check", actor="rules", outcome="fail" if roots else "ok",
                         reason=f"{len(roots)} root cause(s)" if roots else "", final=final, root_causes=roots)
        return b, found

    def _prompt(self) -> str:
        """Ask y/n/s/q. Invalid input re-prompts (up to MAX_INVALID times); EOF or Ctrl-D means quit."""
        for _ in range(MAX_INVALID + 1):
            try:
                raw = self.ask(PROMPT)
            except EOFError:
                self.out("  end of input: treating as quit, nothing applied")
                self.audit.write("prompt", fix_n=self._cur_n, target=self._cur_t, actor="user", outcome="n/a",
                                 reason="end of input", eof=True)
                return "quit"
            a = str(raw).strip().lower()
            if a in ANSWERS:
                return ANSWERS[a]
            self.out("  please answer y, n, s, or q")
            self.audit.write("prompt", fix_n=self._cur_n, target=self._cur_t, actor="user", outcome="warn",
                             reason="invalid answer", answer=redact_text(str(raw))[:40])
        self.out("  too many invalid answers: treating as quit")
        return "quit"

    def _dry_run(self, fix: Fix):
        """One server-side dry run per proposal; later callers reuse the result."""
        fp = fix.fingerprint()
        if fp not in self.dry_run_cache:
            self.dry_run_cache[fp] = self.writer.patch(fix.kind, fix.name, fix.patch_type, fix.patch, dry_run=True)
        return self.dry_run_cache[fp]

    def _validate(self, fix: Fix) -> Optional[str]:
        if not self.dry_run_validate:
            return None
        cp = self._dry_run(fix)
        return None if cp.returncode == 0 else redact_text((cp.stderr or cp.stdout or "").strip())[:400]

    def _after(self, bundle: Bundle, fix: Fix):
        before = bundle.get(KIND_FILE.get(fix.kind, fix.kind.lower() + "s"), fix.name)
        after, err = None, None
        if self.dry_run_validate:
            cp = self._dry_run(fix)
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

    def _open(self, findings: List[Finding]) -> List[Finding]:
        """Findings minus the ones the user skipped (and symptoms of skipped ones)."""
        return [f for f in findings if f.key() not in self.skipped and (f.symptom_of or "") not in self.skipped]

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
        bundle, findings = self._check()                       # Collect snapshot and run rule checks
        rep.initial_findings = [f.to_dict() for f in findings]
        self._print_findings(findings)
        n = 0          # proposals (numbering and the proposal guard)
        applies = 0    # apply attempts, capped by --max-fixes
        while True:
            # Any errors left? Root causes the user has not skipped. Caps also stop the session here.
            roots = [f for f in self._open(findings) if f.root_cause]
            if not roots:
                break
            if applies >= self.max_fixes:
                rep.stopped = f"fix cap reached ({self.max_fixes} applied)"
                break
            if n >= self.max_proposals:
                rep.stopped = f"proposal cap reached ({self.max_proposals} proposals)"
                break
            cands = [f for f in roots if f.fix and f.fix.fingerprint() not in self.handled]
            proposal: Optional[Proposal] = None
            finding: Optional[Finding] = None
            state = frozenset(f.key() for f in roots)
            # Ask the AI? yes when configured and the open errors changed since it last had nothing new.
            if self.llm is not None and state != self.llm_idle_state:
                self.out(f"\nInvestigating with LLM ({getattr(self.llm, 'name', 'llm')}), read-only tools only...")
                self.audit.write("llm_task", findings=sorted(f.key() for f in self._open(findings) if f.root_cause),
                                 candidates=sorted(f.key() for f in cands))
                inv = Investigator(self.llm, self.kc, self.audit, self.budget, self.llm_cfg, log=self.out)
                res = inv.investigate(self._llm_task(self._open(findings), cands))
                rep.tool_steps += res.tool_steps
                if res.proposal and res.proposal.fix.fingerprint() in self.handled:
                    # The AI repeated a patch already tried: record it, then back to Any errors left.
                    n += 1
                    fix = res.proposal.fix
                    rec = FixRecord(n, fix.source, res.proposal.rule, f"{fix.kind}/{fix.name}", fix.summary,
                                    fix.patch_type, fix.patch, decision="repeat",
                                    status="rejected: repeats a patch already tried in this session")
                    self.out(f"\nThe LLM repeated a fix already tried on {rec.target}; recorded and not shown again.")
                    self.history.append(f"fix '{fix.summary}' on {rec.target} was rejected because it repeats a patch "
                                        "already tried; propose something different or finish")
                    self._ev("proposal", rec, outcome="block", reason=rec.status)
                    rep.fixes.append(rec)
                    self.llm_idle_state = state
                    continue
                if res.proposal:
                    proposal = res.proposal
                    finding = self._match(roots, proposal)
                else:
                    self.llm_idle_state = state
                    if res.summary:
                        self.out(_w("LLM: " + res.summary, "     "))
                    if res.stopped and res.stopped != "finished":
                        self.out(f"LLM investigation stopped: {res.stopped}")
            if proposal is None and cands:                     # next rule fix
                finding = cands[0]
                proposal = Proposal(finding.fix, finding.diagnosis, finding.evidence, finding.reasoning, finding.rule)
            if proposal is None:
                rep.stopped = rep.stopped or "no fix available for the remaining errors"
                break
            n += 1
            fix = proposal.fix
            self.handled.add(fix.fingerprint())
            rec = FixRecord(n, fix.source, proposal.rule or (finding.rule if finding else None), f"{fix.kind}/{fix.name}",
                            fix.summary, fix.patch_type, fix.patch, decision="")
            # Policy filter (skeleton): runs before the dry run and the human. The registry is empty, so it always allows.
            live = bundle.get(KIND_FILE.get(fix.kind, fix.kind.lower() + "s"), fix.name)
            pol = policy.evaluate(proposal, live, policy.PolicyContext(self.ns, self.context, list(self.history)),
                                  self.policy_checks)
            self._ev("proposal", rec, actor="ai" if fix.source == "llm" else "rules", outcome="ok")
            self._ev("policy_check", rec, check=check_result(
                "policy_check", "block" if not pol["allow"] else "warn" if pol.get("warnings") else "ok", pol.get("reason") or "",
                pol.get("warnings") or [], 0, allow=pol["allow"], checks_run=pol.get("checks_run", [])))
            if not pol["allow"]:
                rec.decision, rec.status = "blocked", f"blocked by policy: {pol['reason']}"
                self.out(f"\nNot showing a proposed fix for {rec.target}: {rec.status}")
                self.history.append(f"fix '{fix.summary}' on {rec.target} was blocked by policy: {pol['reason']}")
                rep.fixes.append(rec)
                continue                                       # no rule re-run
            t_dr = time.time()
            before, after, err = self._after(bundle, fix)      # Server dry run (once per proposal)
            dr_outcome = "fail" if err else ("ok" if self.dry_run_validate else "skip")
            self._ev("dry_run", rec, check=check_result(
                "dry_run", dr_outcome, err or ("" if self.dry_run_validate else "dry run disabled"),
                [err] if err else [], time.time() - t_dr))
            if err:
                rec.decision, rec.status = "invalid", f"patch rejected by dry run: {err}"
                self.out(f"\nSkipping a proposed fix for {rec.target}: {rec.status}")
                self.history.append(f"fix '{fix.summary}' on {rec.target} was rejected by server dry run: {err}")
                rep.fixes.append(rec)
                continue                                       # no rule re-run
            self._print_proposal(n, proposal, finding, before, after, len(cands))
            self._cur_n, self._cur_t = n, rec.target
            decision = "yes" if self.yes else self._prompt()
            if self.yes:
                self.out(PROMPT + "y   (auto-approved by --yes)")
            rec.decision = decision
            self._ev("decision", rec, actor="kubeagt" if self.yes else "user",
                     outcome={"yes": "ok", "no": "fail", "skip": "skip"}.get(decision, "n/a"),
                     reason="auto-approved by --yes" if self.yes else "", answer=decision)
            if decision == "quit":
                rep.fixes.append(rec)
                rep.stopped = "user quit"
                break
            if decision == "skip":
                if finding:
                    self.skipped.add(finding.key())
                self.history.append(f"user skipped the issue on {rec.target} ({rec.rule}); leave it alone")
                rep.fixes.append(rec)
                continue                                       # no rule re-run
            if decision == "no":
                self.history.append(f"user rejected fix '{fix.summary}' on {rec.target}; propose an alternative only if clearly better")
                rep.fixes.append(rec)
                continue                                       # no rule re-run
            applies += 1
            bundle, findings = self._apply_and_verify(rec, fix, finding, cands)   # one re-collect per apply
            rep.fixes.append(rec)

        applied = any(r.applied for r in rep.fixes)
        if applied:
            self.out(f"\nWaiting up to {int(self.verify_timeout)}s for the namespace to become healthy...")
            ok, problems, _ = wait_healthy(self.health_snapshot, self.verify_timeout, 5, self.sleep)
            bundle, findings = self._check(final=True)            # end-of-session report, not a loop re-run
        else:
            ok, problems = health(bundle)
        rep.healthy, rep.problems = ok, problems
        rep.remaining_findings = [f.to_dict() for f in findings if f.root_cause]
        rep.llm_calls = self.budget.llm_calls
        rep.llm_tokens = self.budget.prompt_tokens + self.budget.completion_tokens
        rep.llm_cost_usd = round(self.budget.cost_usd, 5)
        rep.seconds = round(time.time() - t0, 1)
        self._print_summary(rep)
        self.audit.write("session_end", outcome="ok" if ok else "fail", reason=rep.stopped or "", healthy=ok, problems=problems, stopped=rep.stopped,
                         skipped=sorted(self.skipped), open_findings=[f.key() for f in findings if f.root_cause], fixes=[_rec_dict(r) for r in rep.fixes], seconds=rep.seconds)
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

    def _apply_and_verify(self, rec: FixRecord, fix: Fix, finding: Optional[Finding], cands: List[Finding]) -> tuple:
        """Apply, verify, record. Returns the one fresh (bundle, findings) taken after the apply attempt.

        Verify runs three checks and records them in one fix_verify event: rollout_check, log_check
        (new log lines since the baseline taken just before the apply), and rule_recheck."""
        base = logcheck.capture_baseline(self.log_source, fix.kind, fix.name)
        cp = self.writer.patch(fix.kind, fix.name, fix.patch_type, fix.patch)
        if cp.returncode != 0:
            rec.status = "apply failed: " + redact_text((cp.stderr or cp.stdout).strip())[:300]
            self.out(f"  {rec.status}")
            self.history.append(f"applying '{fix.summary}' on {rec.target} failed: {rec.status}")
            self._ev("fix_apply", rec, outcome="fail", reason=rec.status)
            return self._check()
        rec.applied = True
        self.out(f"  applied: {(cp.stdout or '').strip()}")
        self._ev("fix_apply", rec, outcome="ok")
        pending_same = [f for f in cands if f.target == rec.target and f.fix and f.fix.fingerprint() != fix.fingerprint()]
        timeout = self.settle_timeout if pending_same else self.verify_timeout
        self.out(f"  verifying: waiting up to {int(timeout)}s for rollout of {rec.target}...")
        t = time.time()
        rs = self.writer.rollout_status(fix.kind, fix.name, timeout=int(timeout))
        roll = check_result("rollout_check", "ok" if rs.returncode == 0 else "fail",
                            "" if rs.returncode == 0 else "rollout did not complete",
                            [] if rs.returncode == 0 else [redact_text((rs.stderr or rs.stdout or "").strip())[:300]],
                            time.time() - t)
        # Log check: watch the full window only after a completed rollout; otherwise just read what is there.
        window = self.log_watch_seconds if rs.returncode == 0 else 0
        if window and self.log_source is not None:
            self.out(f"  verifying: watching logs of {rec.target} for {int(window)}s...")
        logs = logcheck.run_log_check(self.log_source, fix.kind, fix.name, base, window, self.sleep)
        self._ev("log_check", rec, check=logs)
        t = time.time()
        b, nf = self._check()
        still = bool(finding) and any(f.key() == finding.key() for f in nf)
        target_issues = [f for f in nf if f.target == rec.target and f.root_cause]
        rule = check_result("rule_recheck", "fail" if still else "ok",
                            f"rule {finding.rule} still fires on {rec.target}" if still else "",
                            [f"{f.rule} on {f.target}: {f.title}" for f in target_issues], time.time() - t)
        checks = [roll, logs, rule]
        if any(c["outcome"] == "fail" for c in checks):
            result = "failed"
        elif logs["outcome"] == "warn":
            result = "verified_with_warnings"
        else:
            result = "verified"
        rec.verify_result = result
        rec.verified = result != "failed"
        # Human-readable status (kept compatible with earlier wording).
        if still:
            rec.status = f"verify failed: rule {finding.rule} still fires on {rec.target}"
        elif rs.returncode != 0:
            rec.status = "verify failed: rollout did not complete"
            if target_issues:
                rec.status += "; blocked by other issue(s): " + ", ".join(sorted({f.rule for f in target_issues}))
            else:
                rec.status += ": " + redact_text((rs.stderr or rs.stdout or "").strip())[:200]
            if logs["outcome"] == "fail":
                rec.status += f"; log check: {logs['reason']}"
        elif logs["outcome"] == "fail":
            rec.status = f"verify failed: log check: {logs['reason']}"
        else:
            rec.status = ("verified with warnings: rollout complete; log check: " + logs["reason"]
                          if result == "verified_with_warnings" else "verified: rollout complete")
            if target_issues:
                rec.status += f"; {len(target_issues)} other issue(s) remain"
        self.out(f"  verify: {result.replace('_', ' ')}")
        for c in checks:
            self.out(f"    {c['check']}: {c['outcome']}" + (f": {c['reason']}" if c["reason"] else ""))
            if c["check"] == "log_check" and c["outcome"] in ("warn", "fail"):
                for e in c["evidence"][:5]:
                    self.out(f"      | {e}")
        log_note = ""
        if logs["outcome"] in ("warn", "fail"):
            log_note = f" Log check {logs['outcome']}: {logs['reason']}" + (
                f"; lines: {logcheck.summarize(logs['evidence'])}" if logs["evidence"] else "")
        if not rec.verified:
            new_state = "; ".join(f"{f.rule} on {f.target}: {f.title}" for f in nf if f.root_cause) or "no rule findings"
            self.history.append(f"applied '{fix.summary}' on {rec.target} but verification failed ({rec.status}).{log_note} "
                                f"New state: {new_state}")
        elif result == "verified_with_warnings":
            self.history.append(f"applied '{fix.summary}' on {rec.target}: verified with warnings.{log_note}")
        else:
            self.history.append(f"applied and verified '{fix.summary}' on {rec.target}")
        self._ev("fix_verify", rec, outcome={"verified": "ok", "verified_with_warnings": "warn"}.get(result, "fail"),
                 reason=rec.status, result=result, checks=checks)
        return b, nf

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
            if r.verify_result:
                o(f"     verify result: {r.verify_result}")
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
