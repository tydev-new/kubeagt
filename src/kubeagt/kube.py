"""kubectl wrappers.

There are two separate classes on purpose:

* ``ReadOnlyKubectl`` is the only thing the LLM tool path can reach. It validates
  every argv against an allowlist of verbs, resources, and flags. Anything not on
  the list is refused before a process is started.
* ``KubeWriter`` is the write path. It does not accept free-form argv at all: it
  builds a ``kubectl patch`` command from validated structured fields, and it is
  only called by the fixer after a human approved the change (or ``--yes`` in tests).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .audit import NULL_AUDIT, AuditLog
from .redact import redact_text


class KubectlError(RuntimeError):
    pass


class ToolRefused(ValueError):
    """Raised when an argv is not allowed on the read-only path."""


READ_VERBS = {"get", "describe", "logs", "events", "explain", "top", "version", "api-resources"}

# Canonical resource names plus the short names kubectl accepts.
_RESOURCE_ALIASES = {
    "pods": ["pod", "pods", "po"],
    "deployments": ["deployment", "deployments", "deploy"],
    "replicasets": ["replicaset", "replicasets", "rs"],
    "statefulsets": ["statefulset", "statefulsets", "sts"],
    "daemonsets": ["daemonset", "daemonsets", "ds"],
    "jobs": ["job", "jobs"],
    "cronjobs": ["cronjob", "cronjobs", "cj"],
    "services": ["service", "services", "svc"],
    "endpoints": ["endpoints", "endpoint", "ep"],
    "endpointslices": ["endpointslice", "endpointslices"],
    "configmaps": ["configmap", "configmaps", "cm"],
    "persistentvolumeclaims": ["persistentvolumeclaim", "persistentvolumeclaims", "pvc"],
    "persistentvolumes": ["persistentvolume", "persistentvolumes", "pv"],
    "storageclasses": ["storageclass", "storageclasses", "sc"],
    "nodes": ["node", "nodes", "no"],
    "events": ["event", "events", "ev"],
    "ingresses": ["ingress", "ingresses", "ing"],
    "networkpolicies": ["networkpolicy", "networkpolicies", "netpol"],
    "serviceaccounts": ["serviceaccount", "serviceaccounts", "sa"],
    "poddisruptionbudgets": ["poddisruptionbudget", "poddisruptionbudgets", "pdb"],
    "horizontalpodautoscalers": ["horizontalpodautoscaler", "horizontalpodautoscalers", "hpa"],
    "resourcequotas": ["resourcequota", "resourcequotas", "quota"],
    "limitranges": ["limitrange", "limitranges", "limits"],
}
READ_RESOURCES = {alias: canon for canon, aliases in _RESOURCE_ALIASES.items() for alias in aliases}
for _canon in list(_RESOURCE_ALIASES):
    # allow group-qualified forms like deployments.apps
    for _g in ("apps", "batch", "networking.k8s.io", "policy", "autoscaling", "storage.k8s.io", "discovery.k8s.io"):
        READ_RESOURCES[f"{_canon}.{_g}"] = _canon

# Flags the LLM may pass. Values are validated loosely; everything else is refused.
_ALLOWED_FLAGS = {
    "-o", "--output", "-l", "--selector", "--field-selector", "--show-labels", "--sort-by",
    "--tail", "--previous", "-p", "-c", "--container", "--all-containers", "--since",
    "--timestamps", "--for", "--types", "--recursive", "--no-headers", "--show-kind",
    "--limit-bytes", "--prefix", "--max-log-requests", "--api-group", "--namespaced",
    "--containers",
}
_FLAGS_WITH_VALUE = {"-o", "--output", "-l", "--selector", "--field-selector", "--sort-by", "--tail",
                     "-c", "--container", "--since", "--for", "--types", "--limit-bytes", "--api-group",
                     "--max-log-requests"}
_OUTPUT_OK = ("json", "yaml", "wide", "name", "jsonpath=", "custom-columns=")


def validate_read_argv(argv: Sequence[str]) -> List[str]:
    """Return a normalized copy of argv if it is allowed on the read-only path, else raise ToolRefused."""
    argv = [str(a) for a in argv]
    if not argv:
        raise ToolRefused("empty command")
    if argv[0] == "kubectl":
        argv = argv[1:]
    if not argv:
        raise ToolRefused("empty command")
    verb = argv[0]
    if verb not in READ_VERBS:
        raise ToolRefused(f"verb '{verb}' is not allowed; allowed verbs: {sorted(READ_VERBS)}")
    out = [verb]
    i = 1
    positional: List[str] = []
    while i < len(argv):
        a = argv[i]
        if any(ch in a for ch in "\n\r\x00"):
            raise ToolRefused("control characters are not allowed")
        if a.startswith("-"):
            name, eq, val = a.partition("=")
            if name not in _ALLOWED_FLAGS:
                raise ToolRefused(f"flag '{name}' is not allowed on the read-only path")
            if name in ("-p",) and verb != "logs":
                raise ToolRefused("-p is only allowed for logs (--previous)")
            if name in _FLAGS_WITH_VALUE and not eq:
                if i + 1 >= len(argv):
                    raise ToolRefused(f"flag '{name}' needs a value")
                val = argv[i + 1]
                i += 1
            if name in ("-o", "--output") and not val.startswith(_OUTPUT_OK):
                raise ToolRefused(f"output format '{val}' is not allowed")
            out.append(f"{name}={val}" if (name in _FLAGS_WITH_VALUE) else a)
        else:
            positional.append(a)
            out.append(a)
        i += 1
    if verb in ("get", "describe", "explain") and positional:
        _check_resource_token(positional[0])
        # `kubectl get pod/a secret/b` form: every kind/name token must be allowed too
        for p in positional[1:]:
            if "/" in p:
                _check_resource_token(p)
    if verb == "logs":
        if not positional:
            raise ToolRefused("logs needs a pod name")
        target = positional[0]
        if "/" in target:
            kind = target.split("/", 1)[0]
            if READ_RESOURCES.get(kind) not in ("pods", "deployments", "replicasets", "statefulsets", "daemonsets", "jobs"):
                raise ToolRefused(f"logs target kind '{kind}' not allowed")
    if verb in ("get", "describe") and not positional:
        raise ToolRefused(f"{verb} needs a resource type")
    if verb == "top" and (not positional or positional[0] not in ("pod", "pods", "po", "node", "nodes", "no")):
        raise ToolRefused("top supports pods or nodes only")
    return out


def _check_resource_token(tok: str) -> None:
    # forms: pods | pods,svc | pod/name | pod/name,svc/name
    for part in tok.split(","):
        kind = part.split("/", 1)[0].lower()
        if kind not in READ_RESOURCES:
            raise ToolRefused(f"resource '{kind}' is not on the read allowlist (Secrets are never readable)")


@dataclass
class KubeTarget:
    namespace: str
    context: Optional[str] = None
    kubeconfig: Optional[str] = None
    kubectl: str = "kubectl"

    def base(self) -> List[str]:
        exe = shutil.which(self.kubectl) or self.kubectl
        b = [exe]
        if self.kubeconfig:
            b += ["--kubeconfig", self.kubeconfig]
        if self.context:
            b += ["--context", self.context]
        return b


def _run(argv: List[str], audit: AuditLog, path: str, timeout: int = 60, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    t0 = time.time()
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
    except FileNotFoundError as e:
        raise KubectlError(f"kubectl not found: {e}") from e
    except subprocess.TimeoutExpired:
        audit.write("kubectl", path=path, argv=_display(argv), rc=None, error="timeout", secs=round(time.time() - t0, 2))
        raise KubectlError(f"timeout after {timeout}s: {' '.join(_display(argv))}") from None
    audit.write("kubectl", path=path, argv=_display(argv), rc=cp.returncode, secs=round(time.time() - t0, 2))
    return cp


def _display(argv: List[str]) -> List[str]:
    out = []
    skip = False
    for a in argv:
        if skip:
            out.append("<path>")
            skip = False
            continue
        out.append(a)
        if a == "--kubeconfig":
            skip = True
    return out


class ReadOnlyKubectl:
    """Read-only kubectl, scoped to one namespace. No method on this class can mutate the cluster."""

    def __init__(self, target: KubeTarget, audit: AuditLog = NULL_AUDIT, max_output: int = 12000):
        self.t = target
        self.audit = audit
        self.max_output = max_output

    def run(self, argv: Sequence[str], path: str = "read", redact: bool = True) -> str:
        args = validate_read_argv(argv)
        full = self.t.base() + args + ["-n", self.t.namespace]
        cp = _run(full, self.audit, path)
        text = cp.stdout if cp.returncode == 0 else (cp.stdout + "\n" + cp.stderr).strip()
        if redact:
            text = redact_text(text)
        if len(text) > self.max_output:
            text = text[: self.max_output] + f"\n... [truncated {len(text) - self.max_output} chars]"
        if cp.returncode != 0:
            return f"[exit {cp.returncode}] {text}"
        return text

    def get_json(self, resource: str, all_namespaces_ok: bool = False) -> dict:
        args = validate_read_argv(["get", resource, "-o", "json"])
        full = self.t.base() + args + ["-n", self.t.namespace]
        cp = _run(full, self.audit, "collect", timeout=90)
        if cp.returncode != 0:
            raise KubectlError(cp.stderr.strip())
        return json.loads(cp.stdout or "{}")

    def secret_metadata(self) -> dict:
        """Secret names and key names only. Uses a jsonpath that never prints values."""
        tmpl = '{{range .items}}{{.metadata.name}}\t{{.type}}\t{{range $k, $v := .data}}{{$k}},{{end}}\n{{end}}'
        full = self.t.base() + ["get", "secrets", "-n", self.t.namespace, "-o", "go-template=" + tmpl]
        cp = _run(full, self.audit, "collect")
        items = []
        if cp.returncode == 0:
            for line in cp.stdout.splitlines():
                parts = line.split("\t")
                if parts and parts[0]:
                    keys = [k for k in (parts[2].split(",") if len(parts) > 2 else []) if k]
                    items.append({"kind": "Secret", "metadata": {"name": parts[0], "namespace": self.t.namespace},
                                  "type": parts[1] if len(parts) > 1 else "", "data": {k: "<redacted>" for k in keys}})
            return {"items": items}
        full = self.t.base() + ["get", "secrets", "-n", self.t.namespace, "-o", "name"]
        cp = _run(full, self.audit, "collect")
        for line in cp.stdout.splitlines():
            if line.strip():
                items.append({"kind": "Secret", "metadata": {"name": line.split("/", 1)[-1], "namespace": self.t.namespace}, "data": {}})
        return {"items": items}

    def logs(self, pod: str, container: str, previous: bool = False, tail: int = 50) -> str:
        argv = ["logs", pod, "-c", container, "--tail", str(tail)]
        if previous:
            argv.append("--previous")
        args = validate_read_argv(argv)
        cp = _run(self.t.base() + args + ["-n", self.t.namespace], self.audit, "collect")
        return redact_text(cp.stdout if cp.returncode == 0 else "")

    def get_object(self, kind: str, name: str) -> Optional[dict]:
        args = validate_read_argv(["get", f"{kind}/{name}", "-o", "json"])
        cp = _run(self.t.base() + args + ["-n", self.t.namespace], self.audit, "read")
        if cp.returncode != 0:
            return None
        return json.loads(cp.stdout or "{}")

    def current_context(self) -> str:
        if self.t.context:
            return self.t.context
        cp = _run(self.t.base() + ["config", "current-context"], self.audit, "read")
        return cp.stdout.strip() or "(unknown)"

    def namespace_exists(self) -> bool:
        cp = _run(self.t.base() + ["get", "namespace", self.t.namespace, "-o", "name"], self.audit, "read")
        return cp.returncode == 0

    def can_i(self, verb: str, resource: str) -> bool:
        cp = _run(self.t.base() + ["auth", "can-i", verb, resource, "-n", self.t.namespace], self.audit, "read")
        return cp.stdout.strip() == "yes"


WRITE_KINDS = {
    "deployment": "deployment", "deployments": "deployment",
    "statefulset": "statefulset", "statefulsets": "statefulset",
    "daemonset": "daemonset", "daemonsets": "daemonset",
    "service": "service", "services": "service",
    "configmap": "configmap", "configmaps": "configmap",
    "persistentvolumeclaim": "persistentvolumeclaim", "pvc": "persistentvolumeclaim",
}
PATCH_TYPES = {"strategic", "merge", "json"}


class KubeWriter:
    """The only write path. Builds `kubectl patch` from structured, validated fields."""

    def __init__(self, target: KubeTarget, audit: AuditLog = NULL_AUDIT):
        self.t = target
        self.audit = audit

    @staticmethod
    def normalize(kind: str, name: str, patch_type: str, patch) -> tuple:
        k = WRITE_KINDS.get(str(kind).lower())
        if not k:
            raise ToolRefused(f"kind '{kind}' cannot be patched by kubeagt (allowed: {sorted(set(WRITE_KINDS.values()))})")
        if not name or "/" in name or " " in name:
            raise ToolRefused(f"bad resource name '{name}'")
        pt = str(patch_type).lower()
        if pt not in PATCH_TYPES:
            raise ToolRefused(f"patch type '{patch_type}' must be one of {sorted(PATCH_TYPES)}")
        if isinstance(patch, str):
            patch = json.loads(patch)
        if pt == "json" and not isinstance(patch, list):
            raise ToolRefused("json patch must be a list of operations")
        if pt != "json" and not isinstance(patch, dict):
            raise ToolRefused("strategic/merge patch must be an object")
        return k, name, pt, patch

    def patch(self, kind: str, name: str, patch_type: str, patch, dry_run: bool = False) -> subprocess.CompletedProcess:
        k, name, pt, patch = self.normalize(kind, name, patch_type, patch)
        argv = self.t.base() + ["patch", k, name, "-n", self.t.namespace, "--type", pt, "-p", json.dumps(patch)]
        if dry_run:
            argv += ["--dry-run=server", "-o", "json"]
        return _run(argv, self.audit, "write-dryrun" if dry_run else "write", timeout=60)

    def rollout_status(self, kind: str, name: str, timeout: int = 90) -> subprocess.CompletedProcess:
        k = WRITE_KINDS.get(kind.lower())
        if k not in ("deployment", "statefulset", "daemonset"):
            return subprocess.CompletedProcess([], 0, "", "")
        argv = self.t.base() + ["rollout", "status", f"{k}/{name}", "-n", self.t.namespace, f"--timeout={timeout}s"]
        return _run(argv, self.audit, "verify", timeout=timeout + 15)
