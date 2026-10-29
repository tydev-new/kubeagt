"""kubeagt command line: collect, check, fix (alias run), eval."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .agent import Budget
from .audit import AuditLog, default_session_dir
from .bundle import Bundle, collect
from .health import HEALTH_KINDS
from .kube import KubectlError, KubeTarget, KubeWriter, ReadOnlyKubectl
from .llm import DEFAULT_BASE_URL, DEFAULT_KEY_ENV, DEFAULT_MODEL, DEFAULT_PROVIDER, PRESETS, RELAY_NAME, LLMConfig, LLMError, make_client
from .rules import MIN_MEMORY_MIB, run_rules


def _common(p: argparse.ArgumentParser, ns: bool = True) -> None:
    p.add_argument("--kubeconfig", help="path to kubeconfig (default: kubectl's own default)")
    p.add_argument("--context", help="kube context to use (default: current context)")
    if ns:
        p.add_argument("-n", "--namespace", default="default", help="namespace to diagnose (default: default)")


def _llm_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("LLM provider (any OpenAI-compatible chat completions API; default OpenRouter)")
    g.add_argument("--llm", default="auto", choices=["auto", "none", "mock", "relay", "live", "openai"],
                   help="auto: use the provider if its API key env var is set, else rules only (default). "
                        "none: rules only. mock: keyless test provider, makes no LLM API calls. "
                        "relay: write each request to the relay dir and wait for a human/agent-written response. "
                        "live: require the provider (openai is an alias).")
    g.add_argument("--relay-dir", help="relay mode: directory for req-N.json / resp-N.json (default $KUBEAGT_RELAY_DIR or $KUBEAGT_HOME/relay)")
    g.add_argument("--relay-timeout", type=float, default=900.0, help="relay mode: seconds to wait for each response (default 900)")
    g.add_argument("--llm-provider", choices=sorted(PRESETS),
                   help=f"preset for base URL, key env var, default model and prices "
                        f"(default $KUBEAGT_LLM_PROVIDER or {DEFAULT_PROVIDER}: {DEFAULT_BASE_URL}, key ${DEFAULT_KEY_ENV})")
    g.add_argument("--llm-base-url", help="override the preset base URL ($KUBEAGT_LLM_BASE_URL)")
    g.add_argument("--llm-model", help=f"override the preset model ($KUBEAGT_LLM_MODEL; OpenRouter default {DEFAULT_MODEL}, "
                                       "check https://openrouter.ai/models for current slugs)")
    g.add_argument("--llm-key-env", help="name of the env var holding the API key (preset default, e.g. OPENROUTER_API_KEY)")
    g.add_argument("--max-steps", type=int, default=12, help="max steps per investigation: one per AI turn, or one per kubectl call the turn runs (default 12)")
    g.add_argument("--max-tokens", type=int, default=200_000, help="token cap for the whole session (default 200000)")
    g.add_argument("--max-cost", type=float, default=0.50, help="estimated spend cap in USD for the session (default 0.50)")


def _cfg(a) -> LLMConfig:
    return LLMConfig.from_env(provider=getattr(a, "llm_provider", None), base_url=a.llm_base_url, model=a.llm_model, api_key_env=a.llm_key_env)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kubeagt", description="Kubernetes diagnostics with approval-gated fixes.")
    p.add_argument("--version", action="version", version=f"kubeagt {__version__}")
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("collect", help="write a redacted snapshot bundle of a namespace (dir or .zip)")
    _common(c)
    c.add_argument("-o", "--output", help="output path; ends with .zip for a zip (default kubeagt-bundle-<ns>-<time>.zip)")
    c.add_argument("--log-tail", type=int, default=50)

    k = sub.add_parser("check", help="run deterministic checks, live or against a bundle (no changes, no LLM)")
    _common(k)
    k.add_argument("--bundle", help="analyze an offline bundle instead of a live cluster")
    k.add_argument("--json", action="store_true", help="print findings as JSON")
    k.add_argument("--min-memory", type=float, default=MIN_MEMORY_MIB, help="flag memory below this many MiB (default 32)")

    for name in ("fix", "run"):
        f = sub.add_parser(name, help="diagnose and propose fixes one at a time, asking before each change"
                           + (" (alias of fix)" if name == "run" else ""))
        _common(f)
        _llm_args(f)
        f.add_argument("--yes", action="store_true", help="approve every fix without asking. For CI and evals only.")
        f.add_argument("--max-fixes", type=int, default=10,
                       help="stop after this many applied fixes (default 10); the session also stops after "
                            "3x this many proposals (at least 10)")
        f.add_argument("--verify-timeout", type=int, default=120, help="seconds to wait for rollout/health after a fix")
        f.add_argument("--log-watch-seconds", type=float, default=30.0,
                       help="seconds to watch the target's pod logs after a rollout (log check in verify; default 30)")
        f.add_argument("--min-memory", type=float, default=MIN_MEMORY_MIB)
        f.add_argument("--session-dir", help="where to write audit.jsonl and report.json (default ~/.kubeagt/sessions/<time>)")

    e = sub.add_parser("eval", help="run scenario evals on a test cluster and write a scorecard")
    _common(e, ns=False)
    _llm_args(e)
    e.set_defaults(llm="none")
    e.add_argument("--scenarios", help="scenarios directory (default: bundled scenarios)")
    e.add_argument("--only", nargs="*", help="scenario names (or prefixes like 04) to run")
    e.add_argument("--out", default="eval-results", help="output directory (default ./eval-results)")
    e.add_argument("--keep", action="store_true", help="keep scenario namespaces after the run")
    e.add_argument("--verify-timeout", type=int, default=120)
    e.add_argument("--log-watch-seconds", type=float, default=30.0, help="log check watch window per applied fix")
    e.add_argument("--list", action="store_true", help="list scenarios and exit")
    return p


def _target(a) -> KubeTarget:
    return KubeTarget(a.namespace, a.context, os.path.expanduser(a.kubeconfig) if a.kubeconfig else None)


def cmd_collect(a) -> int:
    kc = ReadOnlyKubectl(_target(a))
    if not kc.namespace_exists():
        print(f"namespace {a.namespace} not found (context {kc.current_context()})", file=sys.stderr)
        return 2
    b = collect(kc, log_tail=a.log_tail, progress=lambda s: print("  " + s, file=sys.stderr))
    out = Path(a.output or f"kubeagt-bundle-{a.namespace}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
    path = b.save(out)
    print(f"wrote {path}  (redacted; Secret values were never read)")
    return 0


def print_findings(findings, as_json: bool) -> None:
    if as_json:
        print(json.dumps([f.to_dict() for f in findings], indent=1, default=str))
        return
    if not findings:
        print("No problems found.")
        return
    for f in findings:
        tag = f"[{f.severity}]" if f.root_cause else "[symptom]"
        print(f"{tag} {f.target}{(' container ' + f.container) if f.container else ''}: {f.title}  ({f.rule})")
        if f.root_cause:
            print(f"    {f.diagnosis}")
            for e in f.evidence[:4]:
                print(f"    - {e}")
            if f.fix:
                print(f"    suggested fix: {f.fix.summary}")
        else:
            print(f"    explained by {f.symptom_of}")


def cmd_check(a) -> int:
    if a.bundle:
        b = Bundle.load(a.bundle)
        where = f"bundle {a.bundle} (namespace {b.namespace})"
    else:
        kc = ReadOnlyKubectl(_target(a))
        if not kc.namespace_exists():
            print(f"namespace {a.namespace} not found (context {kc.current_context()})", file=sys.stderr)
            return 2
        where = f"namespace {a.namespace}, context {kc.current_context()}"
        b = collect(kc)
    findings = run_rules(b, a.min_memory)
    if not a.json:
        print(f"kubeagt check: {where}")
    print_findings(findings, a.json)
    return 1 if any(f.root_cause for f in findings) else 0


def cmd_fix(a) -> int:
    from .fixer import FixSession
    if a.yes:
        print("WARNING: --yes approves every fix without asking. Use it only for CI and evals.", file=sys.stderr)
    t = _target(a)
    sess_dir = Path(a.session_dir) if a.session_dir else default_session_dir()
    audit = AuditLog(sess_dir / "audit.jsonl", namespace=a.namespace)
    kc = ReadOnlyKubectl(t, audit)
    writer = KubeWriter(t, audit)
    if not kc.namespace_exists():
        print(f"namespace {a.namespace} not found (context {kc.current_context()})", file=sys.stderr)
        return 2
    try:
        cfg = _cfg(a)
        llm = make_client(a.llm, cfg, a.relay_dir, a.relay_timeout)
    except LLMError as e:
        print(f"LLM setup failed: {e}", file=sys.stderr)
        return 2
    if llm is None:
        why = "disabled" if a.llm == "none" else f"{cfg.api_key_env} not set"
        print(f"LLM: off ({why}); using deterministic rules only.")
    elif a.llm == "mock":
        print(f"LLM: {llm.name} (offline, no LLM API calls)")
    elif a.llm == "relay":
        print(f"LLM: {llm.name}; relay dir {llm.dir}")
    else:
        print(f"LLM: {llm.name}")
    audit.write("session_start", namespace=a.namespace, context=kc.current_context(), llm=getattr(llm, "name", None),
                yes=a.yes, version=__version__)
    sess = FixSession(a.namespace, kc.current_context(), lambda: collect(kc), writer, kc, audit, llm=llm, llm_cfg=cfg,
                      budget=Budget(max_steps=a.max_steps, max_tokens=a.max_tokens, max_cost_usd=a.max_cost),
                      yes=a.yes, max_fixes=a.max_fixes, verify_timeout=a.verify_timeout, min_memory_mib=a.min_memory,
                      log_watch_seconds=a.log_watch_seconds,
                      health_snapshot=lambda: collect(kc, kinds=HEALTH_KINDS, with_logs=False, with_secrets=False))
    rep = sess.run()
    (sess_dir / "report.json").write_text(json.dumps(rep.to_dict(), indent=1, default=str))
    print(f"\nAudit log: {sess_dir / 'audit.jsonl'}")
    return 0 if rep.healthy else 1


def cmd_eval(a) -> int:
    from .evals import EvalOptions, default_scenarios_dir, list_scenarios, run_scenario, write_scorecard
    root = Path(a.scenarios) if a.scenarios else default_scenarios_dir()
    scen = list_scenarios(root)
    if a.only:
        scen = [s for s in scen if any(s.name == o or s.name.startswith(o) for o in a.only)]
    if a.list:
        for s in scen:
            print(s.name)
        return 0
    if not (a.context or a.kubeconfig):
        print("kubeagt eval creates and deletes kubeagt-eval-* namespaces. Pass --context and/or --kubeconfig "
              "explicitly so it never runs against your current context by accident.", file=sys.stderr)
        return 2
    kc = ReadOnlyKubectl(KubeTarget("default", a.context, os.path.expanduser(a.kubeconfig) if a.kubeconfig else None))
    ctx = kc.current_context()
    try:
        cfg = _cfg(a)
    except LLMError as e:
        print(f"LLM setup failed: {e}", file=sys.stderr)
        return 2
    opt = EvalOptions(os.path.expanduser(a.kubeconfig) if a.kubeconfig else None, a.context, a.llm, cfg, a.keep,
                      a.verify_timeout, a.max_steps, a.relay_dir, a.relay_timeout, log_watch_seconds=a.log_watch_seconds)
    llm_desc = (a.llm if a.llm == "none" else f"{a.llm} (offline, no LLM API calls)" if a.llm == "mock"
                else RELAY_NAME if a.llm == "relay"
                else f"{a.llm} ({cfg.provider}, {cfg.model})") if a.llm != "auto" else ("auto (" + ("key set" if cfg.api_key() else "no key, rules only") + ")")
    print(f"kubeagt eval: {len(scen)} scenario(s) on context {ctx}, LLM mode {llm_desc}")
    from .evals import Harness
    try:
        opt.node_baseline = Harness(KubeTarget("", a.context, opt.kubeconfig), print).node_labels()
    except Exception as e:
        print(f"could not read node labels for the preflight baseline: {e}", file=sys.stderr)
        return 2
    out = Path(a.out)
    results = []
    for s in scen:
        try:
            results.append(run_scenario(s, opt, out, print))
        except Exception as e:  # keep going; report the error in the scorecard
            print(f"    ERROR: {e}")
            results.append({"scenario": s.name, "error": str(e), "pass": False})
    meta = {"date": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "context": ctx, "llm": llm_desc, "version": __version__}
    p = write_scorecard(results, out, meta)
    print("\n" + p.read_text())
    return 0 if all(r.get("pass") for r in results) else 1


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0
    try:
        return {"collect": cmd_collect, "check": cmd_check, "fix": cmd_fix, "run": cmd_fix, "eval": cmd_eval}[a.cmd](a)
    except KubectlError as e:
        print(f"kubectl error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; no further changes made", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
