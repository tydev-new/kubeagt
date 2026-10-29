"""Local patch application, used to preview diffs offline and in tests.

When a live cluster is available the fixer prefers a server-side dry run, which
gives the exact result the API server would store.
"""
from __future__ import annotations

import copy
import difflib
from typing import Any, List

import yaml


class PatchError(ValueError):
    pass


def _unescape(tok: str) -> str:
    return tok.replace("~1", "/").replace("~0", "~")


def escape_pointer(tok: str) -> str:
    return str(tok).replace("~", "~0").replace("/", "~1")


def _walk(doc: Any, parts: List[str]):
    cur = doc
    for p in parts:
        if isinstance(cur, list):
            cur = cur[int(p)]
        elif isinstance(cur, dict):
            if p not in cur:
                raise PatchError(f"path segment '{p}' not found")
            cur = cur[p]
        else:
            raise PatchError(f"cannot descend into {type(cur).__name__} at '{p}'")
    return cur


def apply_json_patch(doc: Any, ops: List[dict]) -> Any:
    """RFC 6902 subset: add, remove, replace, test."""
    doc = copy.deepcopy(doc)
    for op in ops:
        path = op.get("path", "")
        if not path.startswith("/"):
            raise PatchError(f"bad path {path!r}")
        parts = [_unescape(t) for t in path.split("/")[1:]]
        parent = _walk(doc, parts[:-1])
        last = parts[-1]
        kind = op.get("op")
        if kind == "test":
            if _walk(doc, parts) != op.get("value"):
                raise PatchError(f"test failed at {path}")
            continue
        if isinstance(parent, list):
            idx = len(parent) if last == "-" else int(last)
            if kind == "add":
                parent.insert(idx, copy.deepcopy(op["value"]))
            elif kind == "remove":
                parent.pop(idx)
            elif kind == "replace":
                parent[idx] = copy.deepcopy(op["value"])
            else:
                raise PatchError(f"unsupported op {kind}")
        elif isinstance(parent, dict):
            if kind == "add":
                parent[last] = copy.deepcopy(op["value"])
            elif kind == "remove":
                if last not in parent:
                    raise PatchError(f"cannot remove missing {path}")
                del parent[last]
            elif kind == "replace":
                if last not in parent:
                    raise PatchError(f"cannot replace missing {path}")
                parent[last] = copy.deepcopy(op["value"])
            else:
                raise PatchError(f"unsupported op {kind}")
        else:
            raise PatchError(f"parent of {path} is not a container")
    return doc


def apply_merge_patch(doc: Any, patch: Any) -> Any:
    """RFC 7386 merge patch. Strategic merge is approximated by this for previews only."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = apply_merge_patch(out.get(k), v)
    return out


def apply_patch(doc: Any, patch_type: str, patch: Any) -> Any:
    if patch_type == "json":
        return apply_json_patch(doc, patch)
    return apply_merge_patch(doc, patch)


def spec_diff(before: dict, after: dict, label: str = "") -> str:
    a = yaml.safe_dump({"spec": (before or {}).get("spec", {})}, sort_keys=True, default_flow_style=False).splitlines()
    b = yaml.safe_dump({"spec": (after or {}).get("spec", {})}, sort_keys=True, default_flow_style=False).splitlines()
    lines = difflib.unified_diff(a, b, fromfile=f"{label} (current)", tofile=f"{label} (proposed)", lineterm="", n=2)
    return "\n".join(lines)
