"""The LLM investigation loop.

The model gets exactly three tools:
  kubectl      read-only, allowlisted, namespace-scoped (ReadOnlyKubectl)
  propose_fix  returns a proposal to kubeagt; it does NOT apply anything
  finish       ends the investigation without a fix
There is no tool that writes to the cluster. Applying a fix is done by the fixer through
KubeWriter, and only after a human says yes.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .audit import NULL_AUDIT, AuditLog
from .findings import Fix
from .kube import KubeWriter, ToolRefused
from .llm import ChatClient, LLMConfig, LLMError
from .redact import redact_text

SYSTEM_PROMPT = """You are kubeagt, a careful Kubernetes troubleshooting assistant.
You investigate ONE namespace using read-only kubectl commands, then propose exactly ONE fix.

Rules:
- Use the kubectl tool to gather evidence. It only allows read verbs (get, describe, logs, events, top, explain).
  Secrets are never readable. Do not pass -n or --namespace; the namespace is fixed.
- Prefer the smallest change that addresses the root cause. Fix scheduling problems before runtime ones.
- Deterministic rule findings and candidate fixes are provided. They are usually right; verify them against
  the evidence and propose one, or propose a better fix if the evidence shows the candidate is wrong.
- propose_fix only proposes. A human reviews and approves every change. Patch kinds allowed:
  Deployment, StatefulSet, DaemonSet, Service, ConfigMap, PersistentVolumeClaim.
  Prefer patch_type "json" (RFC 6902) with paths like /spec/template/spec/containers/0/readinessProbe/tcpSocket/port.
- If nothing safe can be proposed, call finish with a short explanation of what a human should do.
- Keep diagnosis and reasoning short and concrete. Cite evidence lines you actually saw."""

TOOLS = [
    {"type": "function", "function": {
        "name": "kubectl",
        "description": "Run a read-only kubectl command in the target namespace. Example args: [\"describe\", \"pod\", \"redis-abc\"].",
        "parameters": {"type": "object", "properties": {
            "args": {"type": "array", "items": {"type": "string"}, "description": "kubectl arguments without the leading 'kubectl'"}},
            "required": ["args"]}}},
    {"type": "function", "function": {
        "name": "propose_fix",
        "description": "Propose exactly one change for the human to approve. Does not apply anything.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string"}, "name": {"type": "string"},
            "patch_type": {"type": "string", "enum": ["json", "merge", "strategic"]},
            "patch": {"description": "JSON patch array, or merge/strategic patch object"},
            "change": {"type": "string", "description": "one short line describing the change, e.g. 'set readinessProbe port to 6379'"},
            "diagnosis": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string"},
            "rule": {"type": "string", "description": "id of the deterministic finding this addresses, if any"}},
            "required": ["kind", "name", "patch_type", "patch", "diagnosis", "reasoning"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End the investigation without proposing a fix.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


@dataclass
class ToolLimits:
    """Inner tool-error loop limits for one investigation. Configurable in code (Investigator(limits=...))."""
    max_calls_per_turn: int = 5    # tool calls beyond this in one assistant turn are not executed
    nudge_after_errors: int = 3    # consecutive failed kubectl results before kubeagt tells the AI to change approach
    stop_after_errors: int = 5     # consecutive failed kubectl results before the investigation stops


def tool_failed(out: str) -> bool:
    """A kubectl tool result counts as an error when it was refused, raised, or kubectl exited non-zero."""
    return out.startswith(("REFUSED", "ERROR", "[exit "))


@dataclass
class Budget:
    max_steps: int = 12            # assistant turns plus executed kubectl calls (see Investigator.investigate)
    max_tokens: int = 200_000
    max_cost_usd: float = 0.50
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0

    def add(self, usage: dict, cfg: Optional[LLMConfig]) -> None:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += p
        self.completion_tokens += c
        self.llm_calls += 1
        reported = usage.get("cost")  # OpenRouter reports the actual charge in USD
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            self.cost_usd += float(reported)
        elif cfg:
            self.cost_usd += p / 1e6 * cfg.price_in_per_mtok + c / 1e6 * cfg.price_out_per_mtok

    def exhausted(self) -> Optional[str]:
        if self.prompt_tokens + self.completion_tokens >= self.max_tokens:
            return f"token cap reached ({self.prompt_tokens + self.completion_tokens} >= {self.max_tokens})"
        if self.cost_usd >= self.max_cost_usd:
            return f"spend cap reached (${self.cost_usd:.4f} >= ${self.max_cost_usd:.2f})"
        return None


@dataclass
class Proposal:
    fix: Fix
    diagnosis: str
    evidence: List[str]
    reasoning: str
    rule: Optional[str] = None


@dataclass
class AgentResult:
    proposal: Optional[Proposal] = None
    summary: str = ""
    tool_steps: int = 0
    stopped: str = ""
    transcript: List[dict] = field(default_factory=list)


class Investigator:
    def __init__(self, llm: ChatClient, kubectl, audit: AuditLog = NULL_AUDIT, budget: Optional[Budget] = None,
                 cfg: Optional[LLMConfig] = None, validate: Optional[Callable[[Fix], Optional[str]]] = None,
                 log: Callable[[str], None] = lambda s: None, limits: Optional[ToolLimits] = None):
        self.llm = llm
        self.limits = limits or ToolLimits()
        self.kc = kubectl
        self.audit = audit
        self.budget = budget or Budget()
        self.cfg = cfg
        self.validate = validate
        self.log = log

    def _kubectl(self, args) -> str:
        if not isinstance(args, list) or not all(isinstance(a, (str, int)) for a in args):
            return "REFUSED: args must be a list of strings"
        if any(str(a) in ("-n", "--namespace", "-A", "--all-namespaces") or str(a).startswith(("--namespace=", "-n=")) for a in args):
            return "REFUSED: the namespace is fixed; do not pass -n/--namespace/-A"
        try:
            return self.kc.run([str(a) for a in args], path="llm-tool")
        except ToolRefused as e:
            return f"REFUSED: {e}"
        except Exception as e:  # kubectl failure is information for the model, not a crash
            return f"ERROR: {e}"

    def investigate(self, task: str) -> AgentResult:
        """Ask the AI until it proposes a fix or finishes, within the caps.

        Step budget: each assistant turn uses one step, except that a turn which executes kubectl calls
        uses one step per executed call, so batching several calls in one turn cannot bypass max_steps.
        Inner tool-error loop: consecutive failed kubectl results are counted (a success resets the count);
        after limits.nudge_after_errors the AI is told to change approach or finish, after
        limits.stop_after_errors the investigation stops. An exact repeat of a call that just failed is
        refused without running it and counts as an error."""
        res = AgentResult()
        messages: List[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
        nudged = False
        lim = self.limits
        used = 0                 # steps used
        errors = 0               # consecutive failed kubectl results
        failed: List[list] = []  # argv of the failed calls in the current error streak
        step = 0
        while True:
            if used >= self.budget.max_steps:
                res.stopped = f"step cap reached ({self.budget.max_steps})"
                break
            why = self.budget.exhausted()
            if why:
                res.stopped = why
                break
            t0 = time.time()
            try:
                resp = self.llm.chat(messages, TOOLS)
            except LLMError as e:
                res.stopped = f"LLM error: {e}"
                self.audit.write("llm_call", actor="ai", outcome="fail", reason=redact_text(str(e))[:300],
                                 step=step, model=getattr(self.llm, "name", "?"), error=redact_text(str(e))[:2000])
                break
            self.budget.add(resp.usage, self.cfg)
            msg = dict(resp.message or {})
            msg.setdefault("role", "assistant")
            calls = msg.get("tool_calls") or []
            fin = getattr(resp, "finish_reason", None)
            truncated = fin == "length"
            self.audit.write("llm_call", actor="ai", outcome="warn" if truncated else "ok",
                             reason="response cut off at the output token limit (finish_reason length)" if truncated else "",
                             step=step, model=getattr(self.llm, "name", "?"), secs=round(time.time() - t0, 2),
                             usage=resp.usage, finish_reason=fin, content=redact_text(msg.get("content") or "")[:2000],
                             tool_calls=[{"name": c.get("function", {}).get("name"),
                                          "arguments": redact_text(c.get("function", {}).get("arguments", ""))[:2000]} for c in calls])
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            step += 1
            if not calls:
                used += 1
                if nudged:
                    res.summary = (msg.get("content") or "").strip()
                    res.stopped = "model answered without a tool call" + (" (response cut off at the token limit)" if truncated else "")
                    break
                messages.append({"role": "user", "content": ("Your last answer was cut off. " if truncated else "")
                                 + "Call propose_fix with exactly one fix, or finish."})
                nudged = True
                continue
            executed = 0
            stop = ""
            for i, c in enumerate(calls):
                fn = c.get("function", {}) or {}
                name = fn.get("name")
                if i >= lim.max_calls_per_turn:
                    out = f"ERROR: too many tool calls in one turn (max {lim.max_calls_per_turn}); this call was not executed"
                    messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
                    continue
                if stop:
                    messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": f"ERROR: not executed; {stop}"})
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = None
                if not isinstance(args, dict):
                    out = "ERROR: arguments were not a JSON object"
                elif name == "kubectl":
                    argv = args.get("args")
                    key = [str(a) for a in argv] if isinstance(argv, list) else argv
                    if key in failed:
                        out = "REFUSED: this exact kubectl call just failed; not run again. Change the arguments, try a different command, or call finish."
                    elif used + executed >= self.budget.max_steps:
                        out = f"ERROR: step budget used up ({self.budget.max_steps}); this call was not executed"
                        stop = out[len("ERROR: "):]
                    else:
                        executed += 1
                        res.tool_steps += 1
                        self.log(f"  [llm] kubectl {' '.join(str(a) for a in (argv if isinstance(argv, list) else []))}")
                        out = self._kubectl(argv)
                    if tool_failed(out):
                        errors += 1
                        if key not in failed:
                            failed.append(key)
                        if errors >= lim.stop_after_errors and not stop:
                            stop = f"tool error limit: {errors} failed kubectl calls in a row"
                    else:
                        errors, failed = 0, []
                elif name == "propose_fix":
                    prop, err = self._proposal(args)
                    if prop:
                        res.proposal = prop
                        res.transcript = messages
                        return res
                    out = f"INVALID: {err}. Fix the proposal and call propose_fix again, or finish."
                elif name == "finish":
                    res.summary = str(args.get("summary", ""))
                    res.stopped = "finished"
                    res.transcript = messages
                    return res
                else:
                    out = f"ERROR: unknown tool {name}"
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
            used += max(1, executed)
            if stop.startswith("tool error limit"):
                res.stopped = stop
                self.audit.write("llm_task", actor="kubeagt", outcome="fail", reason=stop,
                                 consecutive_errors=errors, limit=lim.stop_after_errors,
                                 last_errors=[redact_text(m["content"])[:300] for m in messages[-errors:] if m.get("role") == "tool"])
                break
            if stop:
                res.stopped = f"step cap reached ({self.budget.max_steps})"
                break
            if errors >= lim.nudge_after_errors:
                messages.append({"role": "user", "content":
                                 f"The last {errors} kubectl calls failed. Change your approach (a different command or resource), "
                                 f"or call finish. The investigation stops after {lim.stop_after_errors} failed calls in a row."})
        res.transcript = messages
        return res

    def _proposal(self, a: dict):
        try:
            patch = a.get("patch")
            if isinstance(patch, str):
                patch = json.loads(patch)
            k, n, pt, patch = KubeWriter.normalize(a.get("kind", ""), a.get("name", ""), a.get("patch_type", "json"), patch)
        except (ToolRefused, ValueError) as e:
            return None, str(e)
        missing = [f for f in ("diagnosis", "reasoning") if not str(a.get(f) or "").strip()]
        if missing:
            return None, f"required field(s) empty: {', '.join(missing)}"
        kind = {"deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet", "service": "Service",
                "configmap": "ConfigMap", "persistentvolumeclaim": "PersistentVolumeClaim"}[k]
        change = str(a.get("change") or "").strip()
        if not change:
            diag = str(a.get("diagnosis", "")).strip()
            change = diag.split(". ")[0].rstrip(".")
        if len(change) > 100:
            change = change[:97].rstrip() + "..."
        fix = Fix(kind, n, pt, patch, summary=change, source="llm", confidence="medium")
        if self.validate:
            err = self.validate(fix)
            if err:
                return None, f"server-side dry run rejected the patch: {err}"
        ev = a.get("evidence") or []
        if isinstance(ev, str):
            ev = [ev]
        return Proposal(fix, str(a.get("diagnosis", "")), [str(x) for x in ev], str(a.get("reasoning", "")), a.get("rule")), None
