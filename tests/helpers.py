"""Builders for Kubernetes objects and a tiny in-memory cluster simulator for fixer tests."""
from __future__ import annotations

import copy
import json
import subprocess
from typing import Dict, List

from kdiag.bundle import Bundle
from kdiag.patching import apply_patch
from kdiag.quantity import mib

NS = "demo"


def node(name="node-1", labels=None):
    lb = {"kubernetes.io/hostname": name, "kubernetes.io/os": "linux"}
    lb.update(labels or {})
    return {"kind": "Node", "metadata": {"name": name, "labels": lb},
            "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}}


def redis_deployment(name="redis", node_selector=None, probe_port=6379, mem_req="128Mi", mem_lim="512Mi",
                     image="redis:7.2-alpine", command=None, ports=True, env=None):
    c = {"name": "redis", "image": image,
         "readinessProbe": {"tcpSocket": {"port": probe_port}, "periodSeconds": 3},
         "resources": {"requests": {"memory": mem_req}, "limits": {"memory": mem_lim}}}
    if ports:
        c["ports"] = [{"containerPort": 6379, "name": "redis"}]
    if command:
        c["command"] = command
    if env:
        c["env"] = env
    spec = {"containers": [c]}
    if node_selector:
        spec["nodeSelector"] = dict(node_selector)
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": spec}},
            "status": {}}


def service(name="redis", selector=None, target_port=6379):
    return {"kind": "Service", "metadata": {"name": name, "namespace": NS},
            "spec": {"selector": selector or {"app": name}, "ports": [{"port": 6379, "targetPort": target_port}]}}


def pod_for(dep: dict, phase="Running", ready=True, waiting=None, last_terminated=None, restarts=0,
            unschedulable_msg=None, suffix="abc12", rs_hash="hash1", rs_name=None):
    name = dep["metadata"]["name"]
    tmpl = copy.deepcopy(dep["spec"]["template"])
    labels = dict(tmpl["metadata"]["labels"], **{"pod-template-hash": rs_hash})
    cs = {"name": tmpl["spec"]["containers"][0]["name"], "image": tmpl["spec"]["containers"][0]["image"],
          "ready": ready, "restartCount": restarts, "state": {}, "lastState": {}}
    if waiting:
        cs["state"] = {"waiting": waiting}
    elif phase == "Running":
        cs["state"] = {"running": {"startedAt": "2026-01-01T00:00:00Z"}}
    if last_terminated:
        cs["lastState"] = {"terminated": last_terminated}
    conds = [{"type": "Ready", "status": "True" if ready else "False"}]
    if unschedulable_msg:
        conds = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": unschedulable_msg}]
    st = {"phase": phase, "conditions": conds}
    if phase != "Pending" or not unschedulable_msg:
        st["containerStatuses"] = [cs]
    return {"kind": "Pod", "metadata": {"name": f"{name}-{rs_hash}-{suffix}", "namespace": NS, "labels": labels,
                                        "ownerReferences": [{"kind": "ReplicaSet", "name": rs_name or f"{name}-{rs_hash}"}]},
            "spec": tmpl["spec"], "status": st}


def replicaset_for(dep: dict, rs_hash="hash1", revision=1, image=None):
    name = dep["metadata"]["name"]
    tmpl = copy.deepcopy(dep["spec"]["template"])
    if image:
        tmpl["spec"]["containers"][0]["image"] = image
    return {"kind": "ReplicaSet", "metadata": {"name": f"{name}-{rs_hash}", "namespace": NS,
                                               "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                                               "ownerReferences": [{"kind": "Deployment", "name": name}]},
            "spec": {"template": tmpl}}


def event(obj_kind, obj_name, reason, message, count=1):
    return {"kind": "Event", "involvedObject": {"kind": obj_kind, "name": obj_name}, "reason": reason,
            "message": message, "count": count, "lastTimestamp": "2026-01-01T00:00:00Z"}


def bundle(*objs, logs=None) -> Bundle:
    return Bundle.from_objects(NS, list(objs), logs)


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeCluster:
    """Simulates just enough kubelet/scheduler behavior to exercise the fix loop end to end."""

    def __init__(self, deployments: List[dict], services: List[dict], nodes: List[dict]):
        self.objs: Dict[tuple, dict] = {}
        for d in deployments:
            self.objs[("Deployment", d["metadata"]["name"])] = copy.deepcopy(d)
        for s in services:
            self.objs[("Service", s["metadata"]["name"])] = copy.deepcopy(s)
        self.nodes = nodes
        self.applied: List[tuple] = []
        self.dry_runs = 0
        self.generation = 0

    # -- writer interface
    def patch(self, kind, name, patch_type, patch, dry_run=False):
        key = (kind if kind[0].isupper() else kind.capitalize(), name)
        key = ("Deployment", name) if kind.lower() == "deployment" else ("Service", name) if kind.lower() == "service" else key
        if key not in self.objs:
            return cp(1, err=f'Error from server (NotFound): {kind} "{name}" not found')
        try:
            new = apply_patch(self.objs[key], patch_type, patch)
        except Exception as e:  # noqa: BLE001
            return cp(1, err=f"The request is invalid: {e}")
        if dry_run:
            self.dry_runs += 1
            return cp(0, out=json.dumps(new))
        self.objs[key] = new
        self.applied.append((kind, name, patch_type, patch))
        self.generation += 1
        return cp(0, out=f"{kind.lower()}.apps/{name} patched")

    def rollout_status(self, kind, name, timeout=90):
        ok, _ = self._pod_state(self.objs[("Deployment", name)]) if kind.lower() == "deployment" else (True, None)
        return cp(0 if ok else 1, out="successfully rolled out" if ok else "", err="" if ok else "timed out waiting")

    # -- snapshot
    def _pod_state(self, d: dict):
        spec = d["spec"]["template"]["spec"]
        c = spec["containers"][0]
        sel = spec.get("nodeSelector") or {}
        if sel and not any(all(n["metadata"]["labels"].get(k) == v for k, v in sel.items()) for n in self.nodes):
            return False, pod_for(d, phase="Pending", ready=False,
                                  unschedulable_msg="0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector.",
                                  rs_hash=f"g{self.generation}")
        lim = (c.get("resources", {}).get("limits") or {}).get("memory")
        if lim and mib(lim) < 16:
            return False, pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff", "message": "back-off"},
                                  last_terminated={"reason": "OOMKilled", "exitCode": 137}, restarts=3,
                                  rs_hash=f"g{self.generation}")
        if c.get("command") == ["redis-srv"]:
            return False, pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff"},
                                  last_terminated={"reason": "StartError", "exitCode": 128,
                                                   "message": 'exec: "redis-srv": executable file not found in $PATH'},
                                  restarts=2, rs_hash=f"g{self.generation}")
        port = c.get("readinessProbe", {}).get("tcpSocket", {}).get("port")
        ready = port in (None, 6379)
        return ready, pod_for(d, ready=ready, rs_hash=f"g{self.generation}")

    def snapshot(self) -> Bundle:
        objs = list(copy.deepcopy(o) for o in self.objs.values()) + list(self.nodes)
        pods = []
        for (k, n), o in self.objs.items():
            if k != "Deployment":
                continue
            ok, p = self._pod_state(o)
            pods.append(p)
            o2 = next(x for x in objs if x.get("kind") == "Deployment" and x["metadata"]["name"] == n)
            o2["status"] = {"replicas": 1, "readyReplicas": 1 if ok else 0, "updatedReplicas": 1,
                            **({} if ok else {"unavailableReplicas": 1})}
        eps = []
        for (k, n), s in self.objs.items():
            if k != "Service":
                continue
            sel = s["spec"]["selector"]
            addrs = [{"ip": "10.0.0.1"} for p in pods if all(p["metadata"]["labels"].get(a) == b for a, b in sel.items())
                     and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"]["conditions"])]
            eps.append({"kind": "Endpoints", "metadata": {"name": n}, "subsets": [{"addresses": addrs}] if addrs else []})
        return Bundle.from_objects(NS, objs + pods + eps)
