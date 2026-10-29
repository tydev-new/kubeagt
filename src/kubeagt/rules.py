"""Deterministic checks. Every rule reads only from a Bundle, so they run the same live or offline.

Rules that can name a safe, specific change attach a Fix. Rules that only describe a
symptom are linked to the root-cause finding on the same workload so the user sees one
problem, not five.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from .bundle import Bundle
from .findings import Finding, Fix
from .patching import escape_pointer
from .quantity import fmt_mib, mib

WORKLOAD_FILES = {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet"}

# Ports that well-known images listen on, used only when a container declares no ports.
WELL_KNOWN_PORTS = {
    "redis": 6379, "postgres": 5432, "mysql": 3306, "mariadb": 3306, "mongo": 27017,
    "memcached": 11211, "nginx": 80, "httpd": 80, "rabbitmq": 5672, "elasticsearch": 9200,
}

MIN_MEMORY_MIB = 32.0
SAFE_REQUEST = "128Mi"
SAFE_LIMIT = "512Mi"

# Lower number is proposed first. Pods must schedule before anything else matters.
PRIORITY = {
    "nodeselector-no-match": 10, "node-affinity-no-match": 11, "image-pull-failure": 20,
    "memory-too-small": 30, "oom-killed": 31, "crash-loop": 40, "container-config-error": 41,
    "probe-port-mismatch": 50, "probe-failing": 51, "service-no-matching-pods": 60,
    "service-target-port-mismatch": 61, "unschedulable": 70, "pvc-pending": 75,
    "service-no-ready-endpoints": 80, "workload-unavailable": 90,
}


# ---------------------------------------------------------------- helpers
def _md(o: dict) -> dict:
    return o.get("metadata", {}) or {}


def _labels(o: dict) -> Dict[str, str]:
    return _md(o).get("labels", {}) or {}


def selector_matches(selector: Optional[dict], labels: Dict[str, str]) -> bool:
    """Label selector match supporting matchLabels and matchExpressions."""
    if not selector:
        return False
    ml = selector.get("matchLabels") if ("matchLabels" in selector or "matchExpressions" in selector) else selector
    for k, v in (ml or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get("matchExpressions", []) or []:
        if not _expr_ok(e, labels):
            return False
    return True


def _expr_ok(e: dict, labels: Dict[str, str]) -> bool:
    k, op, vals = e.get("key"), e.get("operator"), e.get("values") or []
    present = k in labels
    if op == "In":
        return present and labels[k] in vals
    if op == "NotIn":
        return not present or labels[k] not in vals
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    if op in ("Gt", "Lt"):
        try:
            a, b = int(labels.get(k, "")), int(vals[0])
        except (ValueError, IndexError):
            return False
        return a > b if op == "Gt" else a < b
    return False


def workloads(b: Bundle) -> Iterable[Tuple[str, dict]]:
    for f, kind in WORKLOAD_FILES.items():
        for o in b.items(f):
            yield kind, o


def pod_spec(w: dict) -> dict:
    return (w.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}


def pod_owner(b: Bundle, pod: dict) -> Tuple[str, str]:
    for ref in _md(pod).get("ownerReferences", []) or []:
        if ref.get("kind") == "ReplicaSet":
            rs = b.get("replicasets", ref.get("name"))
            for r2 in (_md(rs).get("ownerReferences", []) if rs else []) or []:
                if r2.get("kind") == "Deployment":
                    return "Deployment", r2.get("name")
            # no RS object in the bundle: strip the pod-template-hash suffix
            name = ref.get("name", "")
            h = _labels(pod).get("pod-template-hash")
            if h and name.endswith("-" + h) and b.get("deployments", name[: -len(h) - 1]):
                return "Deployment", name[: -len(h) - 1]
            return "ReplicaSet", name
        if ref.get("kind") in ("StatefulSet", "DaemonSet", "Job"):
            return ref["kind"], ref.get("name")
    for kind, w in workloads(b):
        if selector_matches(w.get("spec", {}).get("selector"), _labels(pod)):
            return kind, _md(w).get("name")
    return "Pod", _md(pod).get("name")


def pods_of(b: Bundle, kind: str, name: str, current_only: bool = False) -> List[dict]:
    return [p for p in b.items("pods") if pod_owner(b, p) == (kind, name) and not (current_only and is_stale_pod(b, p))]


def _revision(o: dict) -> int:
    try:
        return int((_md(o).get("annotations") or {}).get("deployment.kubernetes.io/revision", "0"))
    except ValueError:
        return 0


def is_stale_pod(b: Bundle, pod: dict) -> bool:
    """True if the pod belongs to an older ReplicaSet of its Deployment (it is being replaced)."""
    rs_name = next((r.get("name") for r in _md(pod).get("ownerReferences", []) or [] if r.get("kind") == "ReplicaSet"), None)
    rs = b.get("replicasets", rs_name) if rs_name else None
    if not rs:
        return False
    dep = next((r.get("name") for r in _md(rs).get("ownerReferences", []) or [] if r.get("kind") == "Deployment"), None)
    if not dep:
        return False
    revs = [_revision(r) for r in b.items("replicasets")
            if any(o.get("kind") == "Deployment" and o.get("name") == dep for o in _md(r).get("ownerReferences", []) or [])]
    return bool(revs) and _revision(rs) < max(revs)


def events_for(b: Bundle, names: Iterable[str], reasons: Optional[Iterable[str]] = None) -> List[dict]:
    names = set(names)
    reasons = set(reasons) if reasons else None
    out = []
    for e in b.items("events"):
        io = e.get("involvedObject", e.get("regarding", {})) or {}
        if io.get("name") in names and (reasons is None or e.get("reason") in reasons):
            out.append(e)
    out.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    return out


def _event_line(e: dict) -> str:
    io = e.get("involvedObject", {}) or {}
    cnt = e.get("count") or 1
    return f"event {e.get('reason')} on {io.get('kind')}/{io.get('name')} (x{cnt}): {(e.get('message') or '').strip()[:240]}"


def _image_repo(image: str) -> str:
    base = image.split("@", 1)[0]
    last = base.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]


def _container_statuses(pod: dict) -> List[dict]:
    st = pod.get("status", {}) or {}
    return list(st.get("containerStatuses") or []) + list(st.get("initContainerStatuses") or [])


def _container_index(w: dict, cname: str) -> Optional[int]:
    for i, c in enumerate(pod_spec(w).get("containers", []) or []):
        if c.get("name") == cname:
            return i
    return None


def _wname(w: dict) -> str:
    return _md(w).get("name", "")


# ---------------------------------------------------------------- spec rules
def check_node_selector(b: Bundle) -> List[Finding]:
    nodes = b.items("nodes")
    if not nodes:
        return []
    out = []
    for kind, w in workloads(b):
        sel = pod_spec(w).get("nodeSelector") or {}
        if not sel:
            continue
        if any(all(_labels(n).get(k) == v for k, v in sel.items()) for n in nodes):
            continue
        bad = [k for k, v in sel.items() if not any(_labels(n).get(k) == v for n in nodes)]
        if len(bad) == len(sel):
            patch = [{"op": "remove", "path": "/spec/template/spec/nodeSelector"}]
            what = "remove the nodeSelector"
        else:
            patch = [{"op": "remove", "path": "/spec/template/spec/nodeSelector/" + escape_pointer(k)} for k in bad]
            what = "remove nodeSelector keys " + ", ".join(bad)
        sel_s = ", ".join(f"{k}={v}" for k, v in sel.items())
        node_s = "; ".join(f"{_md(n).get('name')}: " + ", ".join(f"{k}={v}" for k, v in sorted(_labels(n).items()) if not k.startswith("beta.")) for n in nodes[:5])
        pods = pods_of(b, kind, _wname(w))
        ev = [f"{kind}/{_wname(w)} spec.template.spec.nodeSelector = {{{sel_s}}}",
              f"{len(nodes)} node(s) in the cluster, none has label(s) " + ", ".join(f"{k}={sel[k]}" for k in bad),
              f"node labels: {node_s}"]
        ev += [_event_line(e) for e in events_for(b, [_md(p).get("name") for p in pods], ["FailedScheduling"])[:2]]
        out.append(Finding(
            rule="nodeselector-no-match", severity="critical", kind=kind, name=_wname(w),
            title=f"nodeSelector {sel_s} matches no node",
            diagnosis=f"Pods of {kind}/{_wname(w)} can never be scheduled: the nodeSelector requires {sel_s}, and no node carries that label.",
            evidence=ev,
            reasoning=("Removing the selector lets the scheduler place the pod on any node. The alternative is to label a node "
                       f"(kubectl label node <node> {', '.join(f'{k}={sel[k]}' for k in bad)}), which is right if the workload truly needs special hardware; "
                       "kubeagt does not modify nodes, so it proposes the workload-side change."),
            fix=Fix(kind, _wname(w), "json", patch, what, confidence="high"),
        ))
    return out


def check_node_affinity(b: Bundle) -> List[Finding]:
    nodes = b.items("nodes")
    if not nodes:
        return []
    out = []
    for kind, w in workloads(b):
        req = (((pod_spec(w).get("affinity") or {}).get("nodeAffinity") or {})
               .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
        terms = req.get("nodeSelectorTerms") or []
        if not terms:
            continue

        def term_ok(t: dict, n: dict) -> bool:
            return all(_expr_ok(e, _labels(n)) for e in t.get("matchExpressions", []) or [])
        if any(term_ok(t, n) for t in terms for n in nodes):
            continue
        out.append(Finding(
            rule="node-affinity-no-match", severity="critical", kind=kind, name=_wname(w),
            title="required node affinity matches no node",
            diagnosis=f"Pods of {kind}/{_wname(w)} require node affinity terms that no node satisfies, so they stay Pending.",
            evidence=[f"nodeSelectorTerms: {terms}", f"{len(nodes)} node(s) checked, none matches"],
            reasoning="Removing the required affinity unblocks scheduling. If the constraint is intentional, label a node instead.",
            fix=Fix(kind, _wname(w), "json",
                    [{"op": "remove", "path": "/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution"}],
                    "remove the required node affinity", confidence="medium"),
        ))
    return out


def check_probe_ports(b: Bundle) -> List[Finding]:
    out = []
    for kind, w in workloads(b):
        pods = pods_of(b, kind, _wname(w))
        pod_names = [_md(p).get("name") for p in pods]
        for ci, c in enumerate(pod_spec(w).get("containers", []) or []):
            ports = [p for p in (c.get("ports") or []) if "containerPort" in p]
            declared = {int(p["containerPort"]) for p in ports}
            named = {p.get("name"): int(p["containerPort"]) for p in ports if p.get("name")}
            known = WELL_KNOWN_PORTS.get(_image_repo(c.get("image", "")))
            for ptype in ("readinessProbe", "livenessProbe", "startupProbe"):
                pr = c.get(ptype) or {}
                for handler in ("tcpSocket", "httpGet", "grpc"):
                    h = pr.get(handler)
                    if not h or "port" not in h:
                        continue
                    port = h["port"]
                    target = None
                    why = ""
                    if isinstance(port, str) and not port.isdigit():
                        if port in named:
                            continue
                        why = f"named port '{port}' is not declared on container {c.get('name')}"
                        target = next(iter(declared)) if len(declared) == 1 else known
                    else:
                        port = int(port)
                        if declared:
                            if port in declared:
                                continue
                            why = f"port {port} is not one of the container's declared ports {sorted(declared)}"
                            target = next(iter(declared)) if len(declared) == 1 else (known if known in declared else None)
                        elif known and port != known:
                            why = f"port {port} differs from {known}, the port {_image_repo(c.get('image', ''))} listens on by default"
                            target = known
                        else:
                            continue
                    ev = [f"container {c.get('name')} image {c.get('image')}: {ptype}.{handler}.port = {h['port']}",
                          f"declared containerPorts: {sorted(declared) or 'none'}"]
                    ev += [_event_line(e) for e in events_for(b, pod_names, ["Unhealthy"])[:2]]
                    fix = None
                    if target:
                        fix = Fix(kind, _wname(w), "json",
                                  [{"op": "replace", "path": f"/spec/template/spec/containers/{ci}/{ptype}/{handler}/port", "value": target}],
                                  f"set {c.get('name')} {ptype} port to {target}",
                                  confidence="high" if declared else "medium")
                    out.append(Finding(
                        rule="probe-port-mismatch", severity="critical" if ptype != "startupProbe" else "warning",
                        kind=kind, name=_wname(w), container=c.get("name"),
                        title=f"{ptype} checks port {h['port']} but the container listens on {target or sorted(declared)}",
                        diagnosis=(f"The {ptype} of container {c.get('name')} probes a port nothing listens on ({why}). "
                                   + ("Readiness never passes, so the pod never becomes Ready and Services send it no traffic."
                                      if ptype == "readinessProbe" else
                                      "The kubelet will keep killing and restarting the container." if ptype == "livenessProbe"
                                      else "The container never finishes starting.")),
                        evidence=ev,
                        reasoning=(f"Pointing the probe at port {target} makes it test the process that is actually serving. "
                                   "The probe type and timings are left unchanged." if target else
                                   "kubeagt cannot tell which port is correct, so it does not propose a patch."),
                        fix=fix,
                    ))
    return out


def check_memory(b: Bundle, min_mib: float = MIN_MEMORY_MIB) -> List[Finding]:
    out = []
    for kind, w in workloads(b):
        for ci, c in enumerate(pod_spec(w).get("containers", []) or []):
            res = c.get("resources") or {}
            req = (res.get("requests") or {}).get("memory")
            lim = (res.get("limits") or {}).get("memory")
            try:
                req_m = mib(req) if req else None
                lim_m = mib(lim) if lim else None
            except ValueError:
                continue
            tiny_req = req_m is not None and req_m < min_mib
            tiny_lim = lim_m is not None and lim_m < min_mib
            if not (tiny_req or tiny_lim):
                continue
            new_lim = SAFE_LIMIT if (tiny_lim or lim_m is None) else lim
            new_req = SAFE_REQUEST if (tiny_req or req_m is None) else req
            if mib(new_req) > mib(new_lim):
                new_req = new_lim
            ops = []
            base = f"/spec/template/spec/containers/{ci}/resources"
            if not res:
                ops.append({"op": "add", "path": base, "value": {"requests": {"memory": new_req}, "limits": {"memory": new_lim}}})
            else:
                for sect, val, cur in (("requests", new_req, req), ("limits", new_lim, lim)):
                    if val == cur:
                        continue
                    if sect in res:
                        ops.append({"op": "add", "path": f"{base}/{sect}/memory", "value": val})
                    else:
                        ops.append({"op": "add", "path": f"{base}/{sect}", "value": {"memory": val}})
            pods = pods_of(b, kind, _wname(w))
            ev = [f"container {c.get('name')} ({c.get('image')}): requests.memory={req or 'unset'}, limits.memory={lim or 'unset'}"]
            failing = False
            for p in pods:
                for cs in _container_statuses(p):
                    if cs.get("name") != c.get("name"):
                        continue
                    for label, st in (("state", cs.get("state") or {}), ("lastState", cs.get("lastState") or {})):
                        t = st.get("terminated") or st.get("waiting")
                        if t and t.get("reason"):
                            failing = True
                            ev.append(f"pod {_md(p).get('name')} {label}: {t.get('reason')} {(t.get('message') or '')[:160]}".rstrip())
                    if cs.get("restartCount"):
                        failing = True
                        ev.append(f"pod {_md(p).get('name')} restartCount={cs.get('restartCount')}")
                    elif (cs.get("state") or {}).get("running"):
                        ev.append(f"pod {_md(p).get('name')} is running now with restartCount=0")
            if failing:
                diag = (f"Container {c.get('name')} is capped at {lim or req} of memory and its pods show failures. Under {fmt_mib(min_mib)} "
                        "most real processes cannot start or are OOM-killed soon after, so it crash-loops.")
            else:
                diag = (f"Container {c.get('name')} is capped at {lim or req} of memory. It may start while idle, but under {fmt_mib(min_mib)} "
                        "there is no headroom: real load (data, connections, buffers) will push it past the limit and the kernel will OOM-kill it.")
            out.append(Finding(
                rule="memory-too-small", severity="critical" if failing else "warning", kind=kind, name=_wname(w), container=c.get("name"),
                title=f"memory {('limit ' + lim) if tiny_lim else ('request ' + req)} is far too small",
                diagnosis=diag,
                evidence=ev[:8],
                reasoning=(f"Setting requests.memory={new_req} and limits.memory={new_lim} gives the process room to run with headroom for load. "
                           "Tune these from real usage (kubectl top pod) once it runs."),
                fix=Fix(kind, _wname(w), "json", ops, f"set {c.get('name')} memory request {new_req}, limit {new_lim}", confidence="high"),
            ))
    return out


# ---------------------------------------------------------------- status rules
def check_pod_status(b: Bundle) -> List[Finding]:
    out: List[Finding] = []
    seen = set()
    for p in b.items("pods"):
        if _md(p).get("deletionTimestamp") or is_stale_pod(b, p):
            continue
        okind, oname = pod_owner(b, p)
        w = None
        if okind in WORKLOAD_FILES.values():
            w = b.get({v: k for k, v in WORKLOAD_FILES.items()}[okind], oname)
        pname = _md(p).get("name")
        for cs in _container_statuses(p):
            cname = cs.get("name")
            state = cs.get("state") or {}
            last = cs.get("lastState") or {}
            waiting = state.get("waiting") or {}
            reason = waiting.get("reason", "")
            term = state.get("terminated") or last.get("terminated") or {}
            key = None
            if reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                key = ("image-pull-failure", okind, oname, cname)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_image_finding(b, okind, oname, w, cname, cs, pname, waiting))
                continue
            if reason == "CreateContainerConfigError":
                key = ("container-config-error", okind, oname, cname)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Finding(
                    rule="container-config-error", severity="critical", kind=okind, name=oname, container=cname,
                    title="container cannot be created: missing config reference",
                    diagnosis=f"Container {cname} references configuration that does not exist: {waiting.get('message', '')}.",
                    evidence=[f"pod {pname} waiting: {reason}: {waiting.get('message', '')}"],
                    reasoning=("Create the missing ConfigMap or Secret, or remove the reference. kubeagt never creates or reads "
                               "Secrets/ConfigMap contents, so this needs a human."),
                ))
                continue
            if term.get("reason") == "OOMKilled":
                key = ("oom-killed", okind, oname, cname)
                if key not in seen:
                    seen.add(key)
                    out.append(_oom_finding(b, okind, oname, w, cname, cs, pname))
                continue
            crash = reason in ("CrashLoopBackOff", "RunContainerError") or (
                term and term.get("exitCode", 0) != 0 and cs.get("restartCount", 0) >= 1)
            if crash:
                key = ("crash-loop", okind, oname, cname)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_crash_finding(b, okind, oname, w, cname, cs, pname, waiting, term))
        # unschedulable
        if (p.get("status", {}) or {}).get("phase") == "Pending":
            for cond in (p.get("status", {}) or {}).get("conditions") or []:
                if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                    key = ("unschedulable", okind, oname, None)
                    if key in seen:
                        break
                    seen.add(key)
                    msg = cond.get("message", "")
                    out.append(Finding(
                        rule="unschedulable", severity="critical", kind=okind, name=oname,
                        title="pod cannot be scheduled",
                        diagnosis=f"Pod {pname} is Pending: {msg}",
                        evidence=[f"pod {pname} PodScheduled=False reason={cond.get('reason')}: {msg}"]
                                 + [_event_line(e) for e in events_for(b, [pname], ["FailedScheduling"])[:2]],
                        reasoning="Check nodeSelector, affinity, taints and tolerations, and whether requests fit node allocatable.",
                    ))
                    break
    return out


def _image_finding(b, okind, oname, w, cname, cs, pname, waiting) -> Finding:
    image = cs.get("image") or ""
    if w is not None:
        ci = _container_index(w, cname)
        if ci is not None:
            image = pod_spec(w)["containers"][ci].get("image", image)
    ev = [f"pod {pname} container {cname} waiting: {waiting.get('reason')}: {(waiting.get('message') or '')[:220]}"]
    ev += [_event_line(e) for e in events_for(b, [pname], ["Failed"])[:1]]
    fix = None
    reasoning = "kubeagt found no earlier working image to roll back to; fix the tag or registry credentials by hand."
    if okind == "Deployment" and w is not None:
        ci = _container_index(w, cname)
        prev = _previous_image(b, oname, cname, image)
        if prev and ci is not None:
            rev, prev_image = prev
            ev.append(f"ReplicaSet revision {rev} of Deployment/{oname} ran image {prev_image}")
            fix = Fix("Deployment", oname, "json",
                      [{"op": "replace", "path": f"/spec/template/spec/containers/{ci}/image", "value": prev_image}],
                      f"roll {cname} image back to {prev_image}", confidence="medium")
            reasoning = (f"The image {image} cannot be pulled (wrong tag, missing image, or no access). Revision {rev} of this Deployment "
                         f"used {prev_image}; rolling the image back restores the last known version. Fix the tag and redeploy afterwards.")
    return Finding(
        rule="image-pull-failure", severity="critical", kind=okind, name=oname, container=cname,
        title=f"image {image} cannot be pulled",
        diagnosis=f"The kubelet cannot pull image {image} for container {cname}, so the container never starts.",
        evidence=ev, reasoning=reasoning, fix=fix,
    )


def _previous_image(b: Bundle, deploy: str, cname: str, bad_image: str) -> Optional[Tuple[int, str]]:
    cands = []
    for rs in b.items("replicasets"):
        owners = [r for r in _md(rs).get("ownerReferences", []) or [] if r.get("kind") == "Deployment" and r.get("name") == deploy]
        if not owners:
            continue
        try:
            rev = int((_md(rs).get("annotations") or {}).get("deployment.kubernetes.io/revision", "0"))
        except ValueError:
            rev = 0
        for c in pod_spec(rs).get("containers", []) or []:
            if c.get("name") == cname and c.get("image") and c.get("image") != bad_image:
                cands.append((rev, c["image"]))
    cands.sort(reverse=True)
    return cands[0] if cands else None


def _oom_finding(b, okind, oname, w, cname, cs, pname) -> Finding:
    fix = None
    lim = None
    if w is not None:
        ci = _container_index(w, cname)
        if ci is not None:
            res = pod_spec(w)["containers"][ci].get("resources") or {}
            lim = (res.get("limits") or {}).get("memory")
            req = (res.get("requests") or {}).get("memory")
            if lim:
                new_lim_m = max(mib(lim) * 2, 128)
                new_lim = fmt_mib(new_lim_m)
                ops = [{"op": "add", "path": f"/spec/template/spec/containers/{ci}/resources/limits/memory", "value": new_lim}]
                if req and mib(req) > new_lim_m:
                    ops.append({"op": "add", "path": f"/spec/template/spec/containers/{ci}/resources/requests/memory", "value": new_lim})
                fix = Fix(okind, oname, "json", ops, f"raise {cname} memory limit from {lim} to {new_lim}", confidence="medium")
    return Finding(
        rule="oom-killed", severity="critical", kind=okind, name=oname, container=cname,
        title=f"container {cname} was OOMKilled",
        diagnosis=f"Container {cname} used more memory than its limit ({lim or 'node memory'}) and was killed.",
        evidence=[f"pod {pname} container {cname} terminated reason=OOMKilled restartCount={cs.get('restartCount', 0)}"],
        reasoning="Doubling the limit (minimum 128Mi) is a conservative first step; check for a leak if it keeps growing.",
        fix=fix,
    )


_NOT_FOUND = re.compile(r"executable file not found|no such file or directory|not found in \$PATH", re.I)


def _crash_finding(b, okind, oname, w, cname, cs, pname, waiting, term) -> Finding:
    msg = (term.get("message") or waiting.get("message") or "").strip()
    logs = b.pod_logs(pname, cname, previous=True) or b.pod_logs(pname, cname)
    tail = [ln for ln in logs.strip().splitlines()[-5:] if ln.strip()]
    ev = [f"pod {pname} container {cname}: waiting={waiting.get('reason') or '-'} restartCount={cs.get('restartCount', 0)}",
          f"last termination: reason={term.get('reason')} exitCode={term.get('exitCode')} {msg[:240]}".rstrip()]
    ev += [f"log: {ln[:200]}" for ln in tail]
    fix = None
    reasoning = "The cause is in the application or its arguments; see the logs. An LLM investigation can dig further."
    if w is not None and _NOT_FOUND.search(msg + "\n" + logs):
        ci = _container_index(w, cname)
        c = pod_spec(w)["containers"][ci] if ci is not None else {}
        if c.get("command"):
            fix = Fix(okind, oname, "json", [{"op": "remove", "path": f"/spec/template/spec/containers/{ci}/command"}],
                      f"remove the command override {c.get('command')} so {c.get('image')} runs its own entrypoint",
                      confidence="medium")
            ev.append(f"spec overrides command: {c.get('command')}")
            reasoning = (f"The runtime cannot find the executable in the command override {c.get('command')}. The image "
                         f"{c.get('image')} ships its own entrypoint; removing the override runs the image as built. "
                         "If the override was intentional, correct the binary path instead.")
    return Finding(
        rule="crash-loop", severity="critical", kind=okind, name=oname, container=cname,
        title=f"container {cname} keeps crashing",
        diagnosis=f"Container {cname} exits repeatedly (reason {term.get('reason') or waiting.get('reason')}, exit code {term.get('exitCode')}).",
        evidence=ev[:9], reasoning=reasoning, fix=fix,
    )


def check_probe_failures(b: Bundle) -> List[Finding]:
    out = []
    for kind, w in workloads(b):
        names = [_md(p).get("name") for p in pods_of(b, kind, _wname(w), current_only=True)]
        evs = events_for(b, names, ["Unhealthy"])
        if evs:
            out.append(Finding(
                rule="probe-failing", severity="warning", kind=kind, name=_wname(w),
                title="health probes are failing",
                diagnosis=f"Probes for {kind}/{_wname(w)} are failing: {(evs[0].get('message') or '').strip()[:200]}",
                evidence=[_event_line(e) for e in evs[:3]],
                reasoning="Check the probe path, port, and timing against what the app serves.",
            ))
    return out


def check_services(b: Bundle) -> List[Finding]:
    out = []
    pods = [p for p in b.items("pods") if not _md(p).get("deletionTimestamp")]
    for svc in b.items("services"):
        spec = svc.get("spec", {}) or {}
        sel = spec.get("selector") or {}
        if not sel or spec.get("type") == "ExternalName":
            continue
        sname = _md(svc).get("name")
        matching = [p for p in pods if all(_labels(p).get(k) == v for k, v in sel.items())]
        sel_s = ",".join(f"{k}={v}" for k, v in sel.items())
        if not matching:
            want = [pt.get("targetPort", pt.get("port")) for pt in spec.get("ports", []) or []]
            cands = []
            for kind, w in workloads(b):
                cports = set()
                for c in pod_spec(w).get("containers", []) or []:
                    for p in c.get("ports") or []:
                        cports.add(p.get("containerPort"))
                        if p.get("name"):
                            cports.add(p.get("name"))
                if any(t in cports for t in want):
                    cands.append((kind, w))
            all_w = list(workloads(b))
            if not cands and len(all_w) == 1:
                cands = all_w
            pod_labels = sorted({",".join(f"{k}={v}" for k, v in sorted(_labels(p).items()) if k != "pod-template-hash") for p in pods})
            ev = [f"Service/{sname} selector {sel_s} matches 0 of {len(pods)} pod(s)",
                  "pod labels in namespace: " + ("; ".join(pod_labels[:5]) or "none")]
            fix = None
            reasoning = "No single workload clearly backs this Service; fix the selector by hand."
            if len(cands) == 1:
                ck, cw = cands[0]
                new_sel = (cw.get("spec", {}).get("selector") or {}).get("matchLabels")
                if new_sel:
                    fix = Fix("Service", sname, "json", [{"op": "replace", "path": "/spec/selector", "value": new_sel}],
                              "set selector to " + ",".join(f"{k}={v}" for k, v in new_sel.items()), confidence="medium")
                    ev.append(f"{ck}/{_wname(cw)} exposes port(s) {want} and labels its pods {new_sel}")
                    reasoning = (f"{ck}/{_wname(cw)} is the only workload serving the Service's target port, so pointing the selector "
                                 "at its pod labels restores endpoints. If the Service is meant for a different app, change that app's labels instead.")
            out.append(Finding(
                rule="service-no-matching-pods", severity="critical", kind="Service", name=sname,
                title=f"Service selector {sel_s} matches no pods",
                diagnosis=f"Service/{sname} has no endpoints because its selector {sel_s} matches no pod. Clients get connection refused or timeouts.",
                evidence=ev, reasoning=reasoning, fix=fix,
            ))
            continue
        # target port check
        cports = set()
        named = set()
        for p in matching:
            for c in (p.get("spec", {}) or {}).get("containers", []) or []:
                for cp in c.get("ports") or []:
                    cports.add(cp.get("containerPort"))
                    if cp.get("name"):
                        named.add(cp.get("name"))
        for i, pt in enumerate(spec.get("ports", []) or []):
            tp = pt.get("targetPort", pt.get("port"))
            if not cports:
                break
            if isinstance(tp, str) and not tp.isdigit():
                ok = tp in named
            else:
                ok = int(tp) in cports
            if ok:
                continue
            fix = None
            if len(cports) == 1:
                port = next(iter(cports))
                fix = Fix("Service", sname, "json", [{"op": "replace", "path": f"/spec/ports/{i}/targetPort", "value": port}],
                          f"set targetPort to {port}", confidence="medium")
            out.append(Finding(
                rule="service-target-port-mismatch", severity="critical", kind="Service", name=sname,
                title=f"Service targetPort {tp} is not a container port",
                diagnosis=f"Service/{sname} forwards to port {tp}, but its pods listen on {sorted(cports)}.",
                evidence=[f"Service/{sname} ports[{i}].targetPort={tp}", f"selected pods declare ports {sorted(cports)}"],
                reasoning="Pointing targetPort at the declared container port makes the Service reach the app.", fix=fix,
            ))
        ready = [p for p in matching if any(c.get("type") == "Ready" and c.get("status") == "True"
                                            for c in (p.get("status", {}) or {}).get("conditions") or [])]
        if not ready:
            okind, oname = pod_owner(b, matching[0])
            out.append(Finding(
                rule="service-no-ready-endpoints", severity="warning", kind="Service", name=sname,
                title="Service has no ready endpoints",
                diagnosis=f"Service/{sname} selects {len(matching)} pod(s) but none is Ready.",
                evidence=[f"selected pods: {', '.join(_md(p).get('name') for p in matching[:5])}", f"backing workload: {okind}/{oname}"],
                reasoning="Fix the backing workload first; the Service recovers on its own once a pod is Ready.",
                symptom_of=f"{okind}/{oname}",
            ))
    return out


def check_pvcs(b: Bundle) -> List[Finding]:
    out = []
    for pvc in b.items("persistentvolumeclaims"):
        if (pvc.get("status", {}) or {}).get("phase") == "Pending":
            n = _md(pvc).get("name")
            out.append(Finding(
                rule="pvc-pending", severity="warning", kind="PersistentVolumeClaim", name=n,
                title="PersistentVolumeClaim is Pending",
                diagnosis=f"PVC {n} is not bound (storageClass {pvc.get('spec', {}).get('storageClassName')}).",
                evidence=[_event_line(e) for e in events_for(b, [n])[:3]] or [f"PVC {n} phase=Pending"],
                reasoning="Check the StorageClass exists and has a provisioner, and the requested size and access mode.",
            ))
    return out


def check_workload_availability(b: Bundle) -> List[Finding]:
    out = []
    for kind, w in workloads(b):
        st = w.get("status", {}) or {}
        want = (w.get("spec", {}) or {}).get("replicas", 1) if kind != "DaemonSet" else st.get("desiredNumberScheduled", 0)
        ready = st.get("readyReplicas", 0) if kind != "DaemonSet" else st.get("numberReady", 0)
        if want and (ready or 0) < want:
            out.append(Finding(
                rule="workload-unavailable", severity="warning", kind=kind, name=_wname(w),
                title=f"{ready or 0}/{want} replicas ready",
                diagnosis=f"{kind}/{_wname(w)} has {ready or 0} of {want} replicas ready.",
                evidence=[f"status: {({k: v for k, v in st.items() if k != 'conditions'})}"]
                         + [f"condition {c.get('type')}={c.get('status')}: {c.get('message', '')[:160]}" for c in st.get("conditions", []) or []][:3],
                reasoning="No specific cause was found by the deterministic rules; an LLM investigation can look deeper.",
            ))
    return out


ALL_RULES = [check_node_selector, check_node_affinity, check_probe_ports, check_memory, check_pod_status,
             check_probe_failures, check_services, check_pvcs, check_workload_availability]

# symptom rule: root rules on the same workload that explain it
_SYMPTOMS = {
    "unschedulable": {"nodeselector-no-match", "node-affinity-no-match"},
    "crash-loop": {"memory-too-small", "oom-killed"},
    "oom-killed": {"memory-too-small"},
    "probe-failing": {"probe-port-mismatch"},
    "workload-unavailable": None,  # explained by any other finding on the workload
}


def run_rules(b: Bundle, min_memory_mib: float = MIN_MEMORY_MIB) -> List[Finding]:
    found: List[Finding] = []
    for r in ALL_RULES:
        found += r(b, min_memory_mib) if r is check_memory else r(b)
    by_target: Dict[str, List[Finding]] = {}
    for f in found:
        by_target.setdefault(f.target, []).append(f)
    for f in found:
        explain = _SYMPTOMS.get(f.rule, "missing")
        if explain == "missing":
            continue
        others = [o for o in by_target.get(f.target, []) if o is not f and o.root_cause and o.rule != "workload-unavailable"]
        if explain is not None:
            others = [o for o in others if o.rule in explain and (o.container in (None, f.container) or f.container is None)]
        if others:
            f.root_cause = False
            f.symptom_of = others[0].key()
            if f.rule == "crash-loop":
                f.fix = None
    for f in found:
        if f.rule == "service-no-ready-endpoints" and f.symptom_of:
            explained = [o for o in by_target.get(f.symptom_of, []) if o.root_cause]
            f.root_cause = not explained
    found.sort(key=lambda f: (not f.root_cause, PRIORITY.get(f.rule, 99), f.target, f.container or ""))
    return found
