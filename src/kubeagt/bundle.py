"""Collect a redacted, portable snapshot of one namespace, and load it back for offline analysis."""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .kube import KubectlError, ReadOnlyKubectl
from .redact import redact_obj

NAMESPACED = ["pods", "deployments", "replicasets", "statefulsets", "daemonsets", "services",
              "endpoints", "configmaps", "persistentvolumeclaims", "events"]
CLUSTER = ["nodes", "storageclasses"]
KIND_FILE = {
    "Pod": "pods", "Deployment": "deployments", "ReplicaSet": "replicasets", "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets", "Service": "services", "Endpoints": "endpoints", "ConfigMap": "configmaps",
    "PersistentVolumeClaim": "persistentvolumeclaims", "Event": "events", "Node": "nodes",
    "StorageClass": "storageclasses", "Secret": "secrets",
}


class Bundle:
    """In-memory view of a bundle. Rules only ever read from this, so they work offline."""

    def __init__(self, namespace: str, resources: Dict[str, List[dict]], logs: Optional[Dict[str, str]] = None,
                 manifest: Optional[dict] = None):
        self.namespace = namespace
        self.resources = {k: list(v) for k, v in resources.items()}
        self.logs = logs or {}
        self.manifest = manifest or {"namespace": namespace}

    def items(self, kind_file: str) -> List[dict]:
        return self.resources.get(kind_file, [])

    def get(self, kind_file: str, name: str) -> Optional[dict]:
        for it in self.items(kind_file):
            if it.get("metadata", {}).get("name") == name:
                return it
        return None

    def pod_logs(self, pod: str, container: str, previous: bool = False) -> str:
        return self.logs.get(log_key(pod, container, previous), "")

    # ---- persistence ----
    def save(self, out: Path, as_zip: bool = False) -> Path:
        out = Path(out)
        files = {"manifest.json": json.dumps(self.manifest, indent=2)}
        for k, items in self.resources.items():
            files[f"resources/{k}.json"] = json.dumps({"items": items}, indent=1)
        for k, txt in self.logs.items():
            files[f"logs/{k}.log"] = txt
        if as_zip or out.suffix == ".zip":
            out = out if out.suffix == ".zip" else out.with_suffix(".zip")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in files.items():
                    z.writestr(name, data)
        else:
            for name, data in files.items():
                p = out / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(data, encoding="utf-8")
        return out

    @classmethod
    def load(cls, path) -> "Bundle":
        path = Path(path)
        files: Dict[str, str] = {}
        if path.is_file() and path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    files[n] = z.read(n).decode("utf-8", "replace")
        elif path.is_dir():
            for p in path.rglob("*"):
                if p.is_file():
                    files[str(p.relative_to(path)).replace("\\", "/")] = p.read_text(encoding="utf-8", errors="replace")
        else:
            raise FileNotFoundError(f"no bundle at {path}")
        manifest = json.loads(files.get("manifest.json", "{}"))
        resources: Dict[str, List[dict]] = {}
        logs: Dict[str, str] = {}
        for n, data in files.items():
            if n.startswith("resources/") and n.endswith(".json"):
                resources[n[len("resources/"):-5]] = json.loads(data).get("items", [])
            elif n.startswith("logs/") and n.endswith(".log"):
                logs[n[len("logs/"):-4]] = data
        return cls(manifest.get("namespace", "default"), resources, logs, manifest)

    @classmethod
    def from_objects(cls, namespace: str, objs: List[dict], logs: Optional[Dict[str, str]] = None) -> "Bundle":
        """Build a bundle from a flat list of k8s objects (used by tests and fixtures)."""
        res: Dict[str, List[dict]] = {}
        for o in objs:
            if o.get("kind") == "List":
                for sub in o.get("items", []):
                    res.setdefault(KIND_FILE.get(sub.get("kind"), sub.get("kind", "").lower() + "s"), []).append(sub)
                continue
            res.setdefault(KIND_FILE.get(o.get("kind"), o.get("kind", "").lower() + "s"), []).append(o)
        return cls(namespace, res, logs or {})


def log_key(pod: str, container: str, previous: bool = False) -> str:
    return f"{pod}__{container}" + (".previous" if previous else "")


def collect(kc: ReadOnlyKubectl, log_tail: int = 50, progress=None, kinds: Optional[List[str]] = None,
            with_logs: bool = True, with_secrets: bool = True) -> Bundle:
    """Read-only snapshot of a namespace. Secret values are never read; everything is redacted."""
    ns = kc.t.namespace
    resources: Dict[str, List[dict]] = {}
    errors: Dict[str, str] = {}
    for kind in (kinds or NAMESPACED + CLUSTER):
        try:
            data = kc.get_json(kind)
            resources[kind] = [redact_obj(_strip(i, kind)) for i in data.get("items", [])]
        except (KubectlError, json.JSONDecodeError) as e:
            errors[kind] = str(e)[:300]
            resources[kind] = []
        if progress:
            progress(f"{kind}: {len(resources[kind])}")
    if with_secrets:
        try:
            resources["secrets"] = kc.secret_metadata()["items"]
        except KubectlError as e:
            errors["secrets"] = str(e)[:300]
    logs: Dict[str, str] = {}
    for pod in (resources.get("pods", []) if with_logs else []):
        pname = pod["metadata"]["name"]
        for cs in (pod.get("status", {}).get("containerStatuses") or []) + (pod.get("status", {}).get("initContainerStatuses") or []):
            c = cs.get("name")
            if cs.get("state", {}).get("running") or cs.get("state", {}).get("terminated") or cs.get("restartCount", 0):
                txt = kc.logs(pname, c, tail=log_tail)
                if txt:
                    logs[log_key(pname, c)] = txt
            if cs.get("restartCount", 0) > 0 or cs.get("lastState", {}).get("terminated"):
                txt = kc.logs(pname, c, previous=True, tail=log_tail)
                if txt:
                    logs[log_key(pname, c, True)] = txt
    manifest = {
        "namespace": ns,
        "context": kc.t.context,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kubeagt_version": __version__,
        "counts": {k: len(v) for k, v in resources.items()},
        "errors": errors,
        "redaction": "Secret values never read; ConfigMap values and env values with sensitive names redacted; token-like strings masked in logs.",
    }
    return Bundle(ns, resources, logs, manifest)


def _strip(obj: dict, kind: str) -> dict:
    md = obj.get("metadata", {})
    md.pop("managedFields", None)
    if kind == "nodes":
        st = obj.get("status", {})
        # keep image names (useful for air-gapped mirroring checks) but drop digests/sizes noise
        st["images"] = [{"names": [n for n in im.get("names", []) if "@sha256" not in n]} for im in st.get("images", [])]
    return obj
