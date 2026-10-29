"""Scenario evals on a real cluster (kind recommended).

The harness below creates namespaces and applies broken manifests. That is test setup,
not part of the diagnosis path: kubeagt's own writes still go only through KubeWriter.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .agent import Budget
from .audit import AuditLog
from .bundle import collect
from .fixer import FixSession, scripted_ask
from .health import HEALTH_KINDS
from .kube import KubeTarget, KubeWriter, ReadOnlyKubectl
from .llm import LLMConfig, make_client
from .rules import run_rules

NS_PREFIX = "kubeagt-eval-"


def default_scenarios_dir() -> Path:
    try:
        from . import scenarios as pkg  # installed wheel: kubeagt/scenarios
        return Path(pkg.__file__).parent
    except ImportError:
        return Path(__file__).resolve().parents[2] / "scenarios"


def list_scenarios(root: Path) -> List[Path]:
    return sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "expected.yaml").exists())


class Harness:
    def __init__(self, base: KubeTarget, log: Callable[[str], None]):
        self.base = base
        self.log = log

    def _k(self, args: List[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        cp = subprocess.run(KubeTarget("", self.base.context, self.base.kubeconfig, self.base.kubectl).base() + args,
                            capture_output=True, text=True, timeout=timeout)
        if check and cp.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {cp.stderr.strip()}")
        return cp

    def reset_namespace(self, ns: str) -> None:
        assert ns.startswith(NS_PREFIX), "eval harness only manages kubeagt-eval-* namespaces"
        if self._k(["get", "namespace", ns], check=False).returncode == 0:
            self._k(["delete", "namespace", ns, "--wait=true", "--timeout=120s"], timeout=150, check=False)
        self._k(["create", "namespace", ns])

    def delete_namespace(self, ns: str) -> None:
        assert ns.startswith(NS_PREFIX)
        self._k(["delete", "namespace", ns, "--wait=false"], check=False)

    def eval_namespaces(self) -> List[str]:
        cp = self._k(["get", "namespaces", "-o", "name"])
        names = [l.split("/", 1)[-1] for l in cp.stdout.split()]
        return sorted(n for n in names if n.startswith(NS_PREFIX))

    def node_labels(self) -> Dict[str, Dict[str, str]]:
        cp = self._k(["get", "nodes", "-o", "json"])
        return {n["metadata"]["name"]: dict(n["metadata"].get("labels") or {}) for n in json.loads(cp.stdout)["items"]}

    def preflight(self, baseline: Optional[Dict[str, Dict[str, str]]], wait: int = 120) -> Optional[str]:
        """Pre-case check: no kubeagt-eval-* namespaces left over (deleted ones get time to finish terminating)
        and node labels match the baseline taken at the start of the run. Returns a reason on failure."""
        deadline = time.time() + wait
        stale = self.eval_namespaces()
        while stale and time.time() < deadline:
            time.sleep(3)
            stale = self.eval_namespaces()
        if stale:
            return f"preflight failed: stale namespace(s) still present after {wait}s: {', '.join(stale)}"
        if baseline is not None:
            now = self.node_labels()
            if now != baseline:
                diff = []
                for node in sorted(set(now) | set(baseline)):
                    a, b = baseline.get(node), now.get(node)
                    if a is None or b is None:
                        diff.append(f"node {node} {'added' if a is None else 'removed'}")
                        continue
                    for k in sorted(set(a) | set(b)):
                        if a.get(k) != b.get(k):
                            diff.append(f"{node} label {k}: {a.get(k)!r} now {b.get(k)!r}")
                return "preflight failed: node labels differ from the baseline: " + "; ".join(diff[:6])
        return None

    def apply(self, ns: str, path: Path) -> None:
        self._k(["apply", "-n", ns, "-f", str(path)])

    def wait_available(self, ns: str, timeout: int = 120) -> None:
        self._k(["wait", "-n", ns, "--for=condition=Available", "deployment", "--all", f"--timeout={timeout}s"],
                timeout=timeout + 15, check=False)


_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def get_path(obj: Any, path: str):
    cur = obj
    for name, idx in _SEG.findall(path):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return False, None
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                return False, None
            cur = cur[i]
    return True, cur


def check_expectation(obj: Optional[dict], chk: dict) -> tuple:
    if obj is None:
        return False, f"{chk['path']}: object missing"
    found, val = get_path(obj, chk["path"])
    if chk.get("absent"):
        return (not found or val in (None, {}, [])), f"{chk['path']} absent (got {val!r})"
    if "equals" in chk:
        want = chk["equals"]
        ok = found and (val == want or str(val) == str(want))
        return ok, f"{chk['path']} == {want!r} (got {val!r})"
    return found, f"{chk['path']} present"


@dataclass
class EvalOptions:
    kubeconfig: Optional[str]
    context: Optional[str]
    llm_mode: str = "none"
    llm_cfg: Optional[LLMConfig] = None
    keep: bool = False
    verify_timeout: int = 120
    max_steps: int = 12
    relay_dir: Optional[str] = None
    relay_timeout: float = 900.0
    node_baseline: Optional[Dict[str, Dict[str, str]]] = None


def _events(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def check_human(spec: dict, mode: str, ev: List[dict], rep, before: Dict[str, Any], after: Dict[str, Any]) -> List[dict]:
    """Checks for a human-response case: the decisions recorded, event counts, and the
    flow chart invariants (n and s never re-run rules, n is never re-offered, skipped
    findings never reach the AI, nothing is written after q)."""
    exp = dict(spec.get("expect") or {})
    exp.update((spec.get("expect_by_mode") or {}).get(mode) or {})
    checks: List[dict] = []

    def add(ok, msg):
        checks.append({"ok": bool(ok), "check": msg})

    decisions = [e for e in ev if e["event"] == "decision"]
    got = [e["decision"] for e in decisions]
    if "decisions" in exp:
        add(got == exp["decisions"], f"decisions {exp['decisions']} (got {got})")
    for name, want in (exp.get("events") or {}).items():
        n = sum(1 for e in ev if e["event"] == name)
        add(n == want, f"audit {name} x{want} (got {n})")
    if "stopped" in exp:
        add(exp["stopped"] in (rep.stopped or ""), f"stopped contains {exp['stopped']!r} (got {rep.stopped!r})")
    open_rules = sorted({f["rule"] for f in rep.remaining_findings})
    for r in exp.get("open") or []:
        add(r in open_rules, f"{r} still open at session end (open: {open_rules})")
    for t in spec.get("unchanged") or []:
        add(before.get(t) is not None and before.get(t) == after.get(t), f"{t} spec and generation unchanged")
    # Invariants from the approved flow chart.
    idx = {id(e): i for i, e in enumerate(ev)}
    for d in decisions:
        i = idx[id(d)]
        nxt = next((j for j in range(i + 1, len(ev)) if ev[j]["event"] in ("decision", "session_end")), len(ev))
        if d["decision"] in ("no", "skip"):
            rc = sum(1 for e in ev[i + 1:nxt] if e["event"] == "rule_check" and not e.get("final"))
            add(rc == 0, f"no rule re-run after {d['decision']} on fix {d['n']} (got {rc})")
        if d["decision"] == "skip" and d.get("rule"):
            key = f"{d['rule']}:{d['target']}:"
            leaked = [e for e in ev[i + 1:] if e["event"] == "llm_task" and any(k.startswith(key) for k in e["findings"])]
            add(not leaked, f"skipped {d['rule']} not sent to the AI again (got {len(leaked)} task(s))")
        if d["decision"] == "quit":
            writes = [e for e in ev[i + 1:] if e["event"] == "kubectl" and e.get("path") in ("write", "write-dryrun")
                      or e["event"] in ("fix_applied", "decision")]
            add(not writes, f"nothing proposed or written after quit (got {len(writes)})")
    fps = [json.dumps([d["target"], d["patch_type"], d["patch"]], sort_keys=True) for d in decisions]
    add(len(fps) == len(set(fps)), "no patch offered twice")
    attempts = sum(1 for e in ev if e["event"] in ("fix_applied", "fix_apply_failed"))
    applied = sum(1 for e in ev if e["event"] == "fix_applied")
    rc_loop = sum(1 for e in ev if e["event"] == "rule_check" and not e.get("final"))
    rc_final = sum(1 for e in ev if e["event"] == "rule_check" and e.get("final"))
    add(rc_loop == 1 + attempts, f"loop rule checks = 1 + apply attempts ({1 + attempts}, got {rc_loop})")
    add(rc_final == (1 if applied else 0), f"final report check only after an apply ({1 if applied else 0}, got {rc_final})")
    return checks


def run_scenario(sdir: Path, opt: EvalOptions, out_dir: Path, log: Callable[[str], None]) -> Dict[str, Any]:
    spec = yaml.safe_load((sdir / "expected.yaml").read_text())
    name = sdir.name
    ns = (NS_PREFIX + name)[:63]
    h = Harness(KubeTarget("", opt.context, opt.kubeconfig), log)
    log(f"\n### {name}: {spec.get('description', '').strip()}")
    why = h.preflight(opt.node_baseline)
    if why:
        log(f"    {why}  FAIL")
        return {"scenario": name, "error": why, "pass": False, "human": "answers" in spec}
    h.reset_namespace(ns)
    for st in spec.get("steps", []):
        h.apply(ns, sdir / st["apply"])
        if st.get("wait_ready"):
            h.wait_available(ns)
    time.sleep(float(spec.get("settle_seconds", 15)))

    sess_dir = out_dir / name
    sess_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(sess_dir / "audit.jsonl")
    target = KubeTarget(ns, opt.context, opt.kubeconfig)
    kc = ReadOnlyKubectl(target, audit)
    writer = KubeWriter(target, audit)
    t0 = time.time()
    initial = run_rules(collect(kc))
    detected = sorted({f.rule for f in initial if f.root_cause})
    expected = sorted(spec.get("expected_findings") or [])
    missing = sorted(set(expected) - set(detected))
    extra = sorted(set(detected) - set(expected))
    detection_ok = not missing and (bool(expected) or not detected)

    transcript: List[str] = []

    def out(s: str) -> None:
        transcript.append(s)

    human = "answers" in spec

    def snap_targets() -> Dict[str, Any]:
        res = {}
        for t in spec.get("unchanged") or []:
            kind, oname = t.split("/", 1)
            o = kc.get_object(kind.lower(), oname)
            res[t] = None if o is None else {"spec": o.get("spec"), "generation": o["metadata"].get("generation")}
        return res

    before = snap_targets()
    llm = make_client(opt.llm_mode, opt.llm_cfg or LLMConfig.from_env(), opt.relay_dir, opt.relay_timeout)
    ask_kw = {"ask": scripted_ask(spec["answers"]), "yes": False} if human else {"yes": True}
    sess = FixSession(ns, kc.current_context(), lambda: collect(kc), writer, kc, audit, llm=llm, llm_cfg=opt.llm_cfg,
                      budget=Budget(max_steps=opt.max_steps), out=out, verify_timeout=opt.verify_timeout, **ask_kw,
                      health_snapshot=lambda: collect(kc, kinds=HEALTH_KINDS, with_logs=False, with_secrets=False))
    rep = sess.run()
    elapsed = time.time() - t0
    (sess_dir / "transcript.txt").write_text("\n".join(transcript) + "\n")
    (sess_dir / "report.json").write_text(json.dumps(rep.to_dict(), indent=1, default=str))

    checks = []
    for ef in spec.get("expected_fixes") or []:
        kind, oname = ef["target"].split("/", 1)
        obj = kc.get_object(kind.lower(), oname)
        for chk in ef.get("checks", []):
            ok, msg = check_expectation(obj, chk)
            checks.append({"ok": ok, "check": msg})
    applied = [r for r in rep.fixes if r.applied]
    if spec.get("expected_fixes"):
        fix_ok = bool(checks) and all(c["ok"] for c in checks)
    else:
        fix_ok = not applied
    expect_healthy = bool(spec.get("expect_healthy", True))
    healthy_ok = rep.healthy == expect_healthy
    steps = len(rep.fixes) + rep.tool_steps
    human_checks = check_human(spec, opt.llm_mode, _events(sess_dir / "audit.jsonl"), rep, before, snap_targets()) if human else []
    human_ok = all(c["ok"] for c in human_checks)
    res = {
        "scenario": name, "namespace": ns, "expected_findings": expected, "detected": detected,
        "missing": missing, "unexpected": extra, "detection_ok": detection_ok,
        "fixes_proposed": len(rep.fixes), "fixes_applied": len(applied), "fix_checks": checks, "fix_ok": fix_ok,
        "healthy_after": rep.healthy, "expect_healthy": expect_healthy, "healthy_ok": healthy_ok,
        "problems_after": rep.problems, "steps": steps, "llm_tool_steps": rep.tool_steps, "llm_calls": rep.llm_calls,
        "llm_tokens": rep.llm_tokens, "seconds": round(elapsed, 1),
        "human": human, "answers": spec.get("answers"), "decisions": [r.decision for r in rep.fixes],
        "human_checks": human_checks, "human_ok": human_ok,
        "pass": detection_ok and fix_ok and healthy_ok and human_ok,
    }
    for c in checks + human_checks:
        if not c["ok"]:
            log(f"    check failed: {c['check']}")
    log(f"    detected={detected} expected={expected} fix_ok={fix_ok} healthy={rep.healthy} "
        f"steps={steps} time={res['seconds']}s  {'PASS' if res['pass'] else 'FAIL'}")
    if not opt.keep:
        h.delete_namespace(ns)
    return res


def write_scorecard(results: List[Dict[str, Any]], out_dir: Path, meta: Dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scorecard.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1))
    yn = lambda b: "yes" if b else "NO"
    lines = ["# kubeagt eval scorecard", "",
             f"- date: {meta.get('date')}", f"- context: {meta.get('context')}", f"- LLM mode: {meta.get('llm')}",
             f"- kubeagt version: {meta.get('version')}", "",
             "| Scenario | Detected (expected) | Detection | Correct fix | Healthy after (expected) | Fixes applied | Steps | Time (s) | Result |",
             "|---|---|---|---|---|---|---|---|---|"]
    scen = [r for r in results if not r.get("human")]
    hum = [r for r in results if r.get("human")]
    for r in scen:
        if "error" in r:
            lines.append(f"| {r['scenario']} | error: {r['error'][:80]} | | | | | | | FAIL |")
            continue
        det = ", ".join(r["detected"]) or "none"
        exp = ", ".join(r["expected_findings"]) or "none"
        lines.append(f"| {r['scenario']} | {det} ({exp}) | {yn(r['detection_ok'])} | {yn(r['fix_ok'])} | "
                     f"{'yes' if r['healthy_after'] else 'no'} ({'yes' if r['expect_healthy'] else 'no'}) | "
                     f"{r['fixes_applied']} | {r['steps']} | {r['seconds']} | {'PASS' if r['pass'] else 'FAIL'} |")

    def summary(rs):
        ok = [r for r in rs if r.get("pass")]
        good = [r for r in rs if "error" not in r]
        return (f"**Passed {len(ok)}/{len(rs)}.** Detection {sum(r['detection_ok'] for r in good)}/{len(good)}, "
                f"correct fix {sum(r['fix_ok'] for r in good)}/{len(good)}, "
                f"health as expected {sum(r['healthy_ok'] for r in good)}/{len(good)}.")
    if scen:
        lines += ["", summary(scen)]
    if hum:
        lines += ["", "## Human response cases", "",
                  "Answers are fed to the real fix prompt in order; when they run out the prompt sees end of input (Ctrl-D).", "",
                  "| Case | Answers | Decisions recorded | Fixes applied | Healthy after (expected) | Cluster and audit checks | Time (s) | Result |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in hum:
            if "error" in r:
                lines.append(f"| {r['scenario']} | | error: {r['error'][:80]} | | | | | FAIL |")
                continue
            allc = r["fix_checks"] + r["human_checks"]
            passed = sum(c["ok"] for c in allc)
            ans = ", ".join(repr(a) if a == "" else str(a) for a in r["answers"]) or "(none, end of input)"
            lines.append(f"| {r['scenario']} | {ans} | {', '.join(r['decisions']) or 'none'} | {r['fixes_applied']} | "
                         f"{'yes' if r['healthy_after'] else 'no'} ({'yes' if r['expect_healthy'] else 'no'}) | "
                         f"{passed}/{len(allc)} | {r['seconds']} | {'PASS' if r['pass'] else 'FAIL'} |")
        lines += ["", f"**Human response cases passed {sum(1 for r in hum if r.get('pass'))}/{len(hum)}.**"]
    lines += ["", f"**Overall passed {sum(1 for r in results if r.get('pass'))}/{len(results)}.**", "",
              "Steps = fix proposals + LLM read-only tool calls. Time covers diagnosis, all fixes, and verification "
              "(scenario setup and settle time excluded)."]
    p = out_dir / "scorecard.md"
    p.write_text("\n".join(lines) + "\n")
    return p
