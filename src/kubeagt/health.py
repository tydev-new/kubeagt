"""Namespace health: what "fixed" means for verification and evals."""
from __future__ import annotations

import time
from typing import Callable, List, Tuple

from .bundle import Bundle
from .rules import workloads

HEALTH_KINDS = ["pods", "deployments", "statefulsets", "daemonsets", "services", "endpoints"]


def health(b: Bundle) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    for kind, w in workloads(b):
        name = w["metadata"]["name"]
        st = w.get("status", {}) or {}
        if kind == "DaemonSet":
            want, ready, upd = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0), st.get("updatedNumberScheduled", 0)
        else:
            want = (w.get("spec", {}) or {}).get("replicas", 1)
            ready, upd = st.get("readyReplicas", 0) or 0, st.get("updatedReplicas", 0) or 0
        if ready < want or upd < want or (st.get("unavailableReplicas") or 0) > 0:
            problems.append(f"{kind}/{name}: {ready}/{want} ready, {upd} updated")
    for p in b.items("pods"):
        md = p.get("metadata", {})
        if md.get("deletionTimestamp"):
            continue
        st = p.get("status", {}) or {}
        if st.get("phase") == "Succeeded":
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])
        if st.get("phase") != "Running" or not ready:
            problems.append(f"Pod/{md.get('name')}: phase={st.get('phase')} ready={ready}")
    eps = {e["metadata"]["name"]: e for e in b.items("endpoints")}
    for s in b.items("services"):
        spec = s.get("spec", {}) or {}
        if not spec.get("selector") or spec.get("type") == "ExternalName":
            continue
        n = s["metadata"]["name"]
        subsets = (eps.get(n) or {}).get("subsets") or []
        if not any(ss.get("addresses") for ss in subsets):
            problems.append(f"Service/{n}: no ready endpoints")
    return (not problems), problems


def wait_healthy(snapshot: Callable[[], Bundle], timeout: float = 120, interval: float = 5,
                 sleep: Callable[[float], None] = time.sleep) -> Tuple[bool, List[str], float]:
    t0 = time.time()
    while True:
        ok, problems = health(snapshot())
        el = time.time() - t0
        if ok or el >= timeout:
            return ok, problems, el
        sleep(interval)
