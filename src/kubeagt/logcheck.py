"""Verify: log check. Runs after every applied fix (docs/design.html, "Verify: log check").

Before the apply kubeagt keeps a baseline of the target's pod logs. After the rollout it watches the
pods for a short window (default 30s), reads their logs (and the previous container's logs after a
restart), and keeps only lines that were not in the baseline. Then:

  fail  clear evidence: fatal, panic, a stack trace, a "missing" or "not set" message, or a container
        exit or restart since the fix
  warn  new error-level lines; or no log output at all (no_signal); or the logs could not be fetched
  ok    otherwise

Lines are already redacted by the kubectl layer and are redacted again here before they become evidence.
"""
from __future__ import annotations

import math
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from .audit import cap_evidence, check_result
from .redact import redact_text
from .rules import selector_matches

DEFAULT_WINDOW = 30.0
POLL = 5.0
TAIL = 500                 # lines per container per fetch
MAX_PODS = 20
MAX_TEXT = 200_000         # chars per fetch kept for analysis
NO_SIGNAL = "no log output; cannot confirm app health from logs"
WORKLOAD_KINDS = {"deployment", "statefulset", "daemonset"}
LOG_KINDS = WORKLOAD_KINDS | {"service"}

FAIL_RE = re.compile(
    r"\b(fatal|panic|panicked)\b|traceback \(most recent call last\)|exception in thread|goroutine \d+ \[|"
    r"^\s+at [\w$.<>/]+\(.*\)\s*$|segmentation fault|\b(missing|not set)\b", re.I)
ERROR_RE = re.compile(r"\b(error|err|critical|crit)\b|level=error|\"level\"\s*:\s*\"error\"|\[error\]|^E\d{4} ", re.I)
CRASH_WAITING = {"CrashLoopBackOff", "Error", "RunContainerError", "StartError"}


def normalize(line: str) -> str:
    """Make lines comparable across pods and restarts: drop numbers, hex ids, and spacing."""
    s = re.sub(r"[0-9a-f]{8,}", "#", line.strip().lower())
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", s))


class KubeLogSource:
    """Reads pods and logs through the read-only kubectl path."""

    def __init__(self, kc):
        self.kc = kc

    def selector(self, kind: str, name: str) -> Optional[dict]:
        o = self.kc.get_object(kind.lower(), name)
        if not o:
            return None
        sel = (o.get("spec") or {}).get("selector")
        return sel or None

    def pods(self) -> List[dict]:
        return list(self.kc.get_json("pods").get("items", []))

    def logs(self, pod: str, container: str, previous: bool = False, tail: int = TAIL) -> Tuple[bool, str, str]:
        return self.kc.fetch_logs(pod, container, previous, tail)


def _matching(src, selector: dict) -> List[dict]:
    pods = [p for p in src.pods() if not (p.get("metadata") or {}).get("deletionTimestamp")
            and selector_matches(selector, (p.get("metadata") or {}).get("labels") or {})]
    return sorted(pods, key=lambda p: p["metadata"]["name"])[:MAX_PODS]


def _statuses(p: dict) -> List[dict]:
    return list((p.get("status") or {}).get("containerStatuses") or [])


def _containers(p: dict) -> List[str]:
    return [c["name"] for c in (p.get("spec") or {}).get("containers") or [] if c.get("name")]


def _lines(text: str) -> List[str]:
    return [l for l in (text or "")[-MAX_TEXT:].splitlines() if l.strip()]


def capture_baseline(src, kind: str, name: str) -> dict:
    """Logs and restart counts of the target's pods before the fix. Never raises."""
    base = {"lines": set(), "restarts": {}, "pods": set(), "selector": None, "error": ""}
    if src is None or kind.lower() not in LOG_KINDS:
        return base
    try:
        sel = src.selector(kind, name)
        base["selector"] = sel
        if not sel:
            return base
        for p in _matching(src, sel):
            pn = p["metadata"]["name"]
            base["pods"].add(pn)
            for cs in _statuses(p):
                base["restarts"][(pn, cs.get("name"))] = int(cs.get("restartCount") or 0)
            for c in _containers(p):
                for prev in (False, True):
                    ok, text, _ = src.logs(pn, c, prev)
                    if ok:
                        base["lines"].update(normalize(l) for l in _lines(text))
    except Exception as e:  # a baseline problem must never block the fix; the check reports it later
        base["error"] = redact_text(str(e))[:300]
    return base


def _exits(pods: List[dict], base: dict) -> List[str]:
    """Container exits or restarts since the fix."""
    out = []
    for p in pods:
        pn = p["metadata"]["name"]
        for cs in _statuses(p):
            cn = cs.get("name")
            was = base["restarts"].get((pn, cn), 0)
            now = int(cs.get("restartCount") or 0)
            st = cs.get("state") or {}
            if now > was:
                last = (cs.get("lastState") or {}).get("terminated") or {}
                out.append(f"pod {pn} container {cn} restarted {now - was} time(s)"
                           + (f" (last exit code {last.get('exitCode')}, {last.get('reason', '')})" if last else ""))
            elif "terminated" in st:
                t = st["terminated"]
                out.append(f"pod {pn} container {cn} exited (exit code {t.get('exitCode')}, {t.get('reason', '')})")
            elif (st.get("waiting") or {}).get("reason") in CRASH_WAITING:
                out.append(f"pod {pn} container {cn} waiting: {st['waiting'].get('reason')}")
    return out


def run_log_check(src, kind: str, name: str, base: dict, window: float = DEFAULT_WINDOW,
                  sleep: Callable[[float], None] = time.sleep, poll: float = POLL) -> dict:
    """Watch the target's pods for `window` seconds, then judge their new log lines.
    Returns the shared check-result shape plus pods, lines_seen, new_lines, no_signal, window_secs."""
    t0 = time.time()

    def result(outcome, reason, evidence=(), **extra):
        extra.setdefault("no_signal", False)
        return check_result("log_check", outcome, reason, [redact_text(e) for e in evidence], time.time() - t0,
                            window_secs=window, **extra)

    if src is None:
        return result("skip", "no log source available")
    if kind.lower() not in LOG_KINDS:
        return result("skip", f"{kind} has no pods to read logs from")
    try:
        sel = base.get("selector") or src.selector(kind, name)
        if not sel:
            return result("warn", f"could not find the pod selector of {kind}/{name}")
        steps = int(math.ceil(window / poll)) if window > 0 else 0
        pods = _matching(src, sel)
        for _ in range(steps):              # watch: stop early on a clear exit or restart
            sleep(min(poll, window))
            pods = _matching(src, sel)
            if _exits(pods, base):
                break
        exits = _exits(pods, base)
        seen: List[str] = []
        errors: List[str] = []
        fetched = 0
        for p in pods:
            pn = p["metadata"]["name"]
            restarted = {cs.get("name") for cs in _statuses(p)
                         if int(cs.get("restartCount") or 0) > base["restarts"].get((pn, cs.get("name")), 0)}
            for c in _containers(p):
                for prev in ((False, True) if c in restarted else (False,)):
                    ok, text, err = src.logs(pn, c, prev)
                    fetched += 1
                    if ok:
                        seen += [l[:1000] for l in _lines(text)]
                    elif not prev:
                        errors.append(f"{pn}/{c}: {err or 'logs unavailable'}")
    except Exception as e:
        return result("warn", "could not fetch logs: " + redact_text(str(e))[:200])
    new = [l for l in seen if normalize(l) not in base["lines"]]
    new = list(dict.fromkeys(new))      # the same line from the current and previous container counts once
    fatal = [l for l in new if FAIL_RE.search(l)]
    errs = [l for l in new if ERROR_RE.search(l) and l not in fatal]
    info = dict(pods=[p["metadata"]["name"] for p in pods][:10], lines_seen=len(seen), new_lines=len(new),
                fetch_errors=len(errors))
    if exits or fatal:
        why = []
        if exits:
            why.append("container exit or restart since the fix")
        if fatal:
            why.append(f"{len(fatal)} fatal or missing-config log line(s)")
        return result("fail", "; ".join(why), exits + fatal, **info)
    if not pods:
        return result("warn", NO_SIGNAL, ["no pods matched the target's selector"], no_signal=True, **info)
    if not seen and errors and len(errors) >= fetched:
        return result("warn", "could not fetch logs: " + errors[0], errors, **info)
    if not seen:
        return result("warn", NO_SIGNAL, [f"{len(pods)} pod(s) produced no log lines"], no_signal=True, **info)
    if errs:
        return result("warn", f"{len(errs)} new error-level log line(s) after the fix", errs, **info)
    if errors:
        return result("warn", f"logs unavailable for {len(errors)} container(s)", errors, **info)
    return result("ok", "", [], **info)


def summarize(evidence: List[str]) -> str:
    return " | ".join(cap_evidence(evidence, 3, 160))
