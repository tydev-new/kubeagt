"""Scenario evals on a real cluster (kind recommended).

The harness below creates namespaces and applies broken manifests. That is test setup,
not part of the diagnosis path: kdiag's own writes still go only through KubeWriter.
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
from .fixer import FixSession
from .health import HEALTH_KINDS
from .kube import KubeTarget, KubeWriter, ReadOnlyKubectl
from .llm import LLMConfig, make_client
from .rules import run_rules

NS_PREFIX = "kdiag-eval-"


def default_scenarios_dir() -> Path:
    try:
        from . import scenarios as pkg  # installed wheel: kdiag/scenarios
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
        assert ns.startswith(NS_PREFIX), "eval harness only manages kdiag-eval-* namespaces"
        if self._k(["get", "namespace", ns], check=False).returncode == 0:
            self._k(["delete", "namespace", ns, "--wait=true", "--timeout=120s"], timeout=150, check=False)
        self._k(["create", "namespace", ns])

    def delete_namespace(self, ns: str) -> None:
        assert ns.startswith(NS_PREFIX)
        self._k(["delete", "namespace", ns, "--wait=false"], check=False)

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


def run_scenario(sdir: Path, opt: EvalOptions, out_dir: Path, log: Callable[[str], None]) -> Dict[str, Any]:
    spec = yaml.safe_load((sdir / "expected.yaml").read_text())
    name = sdir.name
    ns = (NS_PREFIX + name)[:63]
    h = Harness(KubeTarget("", opt.context, opt.kubeconfig), log)
    log(f"\n### {name}: {spec.get('description', '').strip()}")
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

    llm = make_client(opt.llm_mode, opt.llm_cfg or LLMConfig.from_env())
    sess = FixSession(ns, kc.current_context(), lambda: collect(kc), writer, kc, audit, llm=llm, llm_cfg=opt.llm_cfg,
                      budget=Budget(max_steps=opt.max_steps), yes=True, out=out, verify_timeout=opt.verify_timeout,
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
    res = {
        "scenario": name, "namespace": ns, "expected_findings": expected, "detected": detected,
        "missing": missing, "unexpected": extra, "detection_ok": detection_ok,
        "fixes_proposed": len(rep.fixes), "fixes_applied": len(applied), "fix_checks": checks, "fix_ok": fix_ok,
        "healthy_after": rep.healthy, "expect_healthy": expect_healthy, "healthy_ok": healthy_ok,
        "problems_after": rep.problems, "steps": steps, "llm_tool_steps": rep.tool_steps, "llm_calls": rep.llm_calls,
        "llm_tokens": rep.llm_tokens, "seconds": round(elapsed, 1),
        "pass": detection_ok and fix_ok and healthy_ok,
    }
    log(f"    detected={detected} expected={expected} fix_ok={fix_ok} healthy={rep.healthy} "
        f"steps={steps} time={res['seconds']}s  {'PASS' if res['pass'] else 'FAIL'}")
    if not opt.keep:
        h.delete_namespace(ns)
    return res


def write_scorecard(results: List[Dict[str, Any]], out_dir: Path, meta: Dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scorecard.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1))
    yn = lambda b: "yes" if b else "NO"
    lines = ["# kdiag eval scorecard", "",
             f"- date: {meta.get('date')}", f"- context: {meta.get('context')}", f"- LLM mode: {meta.get('llm')}",
             f"- kdiag version: {meta.get('version')}", "",
             "| Scenario | Detected (expected) | Detection | Correct fix | Healthy after (expected) | Fixes applied | Steps | Time (s) | Result |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['scenario']} | error: {r['error'][:80]} | | | | | | | FAIL |")
            continue
        det = ", ".join(r["detected"]) or "none"
        exp = ", ".join(r["expected_findings"]) or "none"
        lines.append(f"| {r['scenario']} | {det} ({exp}) | {yn(r['detection_ok'])} | {yn(r['fix_ok'])} | "
                     f"{'yes' if r['healthy_after'] else 'no'} ({'yes' if r['expect_healthy'] else 'no'}) | "
                     f"{r['fixes_applied']} | {r['steps']} | {r['seconds']} | {'PASS' if r['pass'] else 'FAIL'} |")
    ok = [r for r in results if r.get("pass")]
    good = [r for r in results if "error" not in r]
    lines += ["", f"**Passed {len(ok)}/{len(results)}.** "
              f"Detection {sum(r['detection_ok'] for r in good)}/{len(good)}, "
              f"correct fix {sum(r['fix_ok'] for r in good)}/{len(good)}, "
              f"health as expected {sum(r['healthy_ok'] for r in good)}/{len(good)}.", "",
              "Steps = fix proposals + LLM read-only tool calls. Time covers diagnosis, all fixes, and verification "
              "(scenario setup and settle time excluded)."]
    p = out_dir / "scorecard.md"
    p.write_text("\n".join(lines) + "\n")
    return p
