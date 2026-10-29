"""Kubernetes resource quantity parsing."""
from __future__ import annotations

import re

_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
_RE = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")


def parse_quantity(q) -> float:
    if q is None:
        raise ValueError("empty quantity")
    if isinstance(q, (int, float)):
        return float(q)
    s = str(q).strip()
    m = _RE.match(s)
    if not m:
        raise ValueError(f"bad quantity {q!r}")
    num, suf = float(m.group(1)), m.group(2)
    if suf in _BIN:
        return num * _BIN[suf]
    if suf in _DEC:
        return num * _DEC[suf]
    raise ValueError(f"bad quantity suffix {q!r}")


def mib(q) -> float:
    return parse_quantity(q) / 2**20


def fmt_mib(n: float) -> str:
    return f"{int(n)}Mi" if n >= 1 else f"{n:.2f}Mi"
