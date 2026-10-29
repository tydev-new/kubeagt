"""Standard audit envelope (schema_version 1) on every event type, and reading version 0 files."""
import json

from helpers import FakeCluster, node, redis_deployment, service

from kubeagt import kube
from kubeagt.agent import Investigator
from kubeagt.audit import ACTORS, ENVELOPE, OUTCOMES, AuditLog, check_result, read_events, upgrade_v0
from kubeagt.fixer import FixSession, scripted_ask
from kubeagt.llm import LLMError, ScriptedLLM, tool_call
from test_fixer import KC
from test_logcheck import ClusterLogs

PROBE = [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}]


def _full_session(tmp_path):
    """One session that emits every event type the fix loop writes."""
    fc = FakeCluster([redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432)], [service()], [node()])
    bad = {"kind": "Deployment", "name": "nope", "patch_type": "json", "patch": PROBE, "diagnosis": "d", "reasoning": "r"}
    good = {"kind": "Deployment", "name": "redis", "patch_type": "json", "patch": PROBE, "diagnosis": "d", "reasoning": "r",
            "rule": "probe-port-mismatch"}
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tool_call("kubectl", {"args": ["get", "pods"]}, "0")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", bad, "1")]},     # dry run fails
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", bad, "2")]},     # repeat
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", good, "3")]},
    ])
    audit = AuditLog(tmp_path / "s1" / "audit.jsonl", namespace="demo")
    audit.write("session_start", namespace="demo", context="kind-test")
    kube._run(["true"], audit, "read")                                   # a kubectl event
    FixSession("demo", "kind-test", fc.snapshot, fc, KC(), audit, llm=llm, ask=scripted_ask(["maybe", "y", "y"]),
               out=lambda s: None, verify_timeout=1, sleep=lambda s: None, log_source=ClusterLogs(fc, "ready"),
               log_watch_seconds=5).run()

    class Failing:
        name = "failing"

        def chat(self, m, t):
            raise LLMError("upstream 503")
    Investigator(Failing(), KC(), audit).investigate("task")              # an llm_call with outcome fail
    return read_events(tmp_path / "s1" / "audit.jsonl")


def test_envelope_on_every_event_type(tmp_path):
    ev = _full_session(tmp_path)
    names = {e["event"] for e in ev}
    assert names >= {"session_start", "kubectl", "rule_check", "llm_task", "llm_call", "proposal", "policy_check",
                     "dry_run", "prompt", "decision", "fix_apply", "log_check", "fix_verify", "session_end"}
    for e in ev:
        assert list(e) == list(ENVELOPE), e["event"]
        assert e["schema_version"] == 1 and e["actor"] in ACTORS and e["outcome"] in OUTCOMES
        assert isinstance(e["detail"], dict) and isinstance(e["reason"], str)
        if e["target"] is not None:
            assert set(e["target"]) == {"kind", "name", "namespace"}
    assert {e["outcome"] for e in ev if e["event"] == "dry_run"} >= {"fail", "ok"}
    assert any(e["event"] == "proposal" and e["outcome"] == "block" for e in ev)
    assert any(e["event"] == "llm_call" and e["outcome"] == "fail" and e["actor"] == "ai" for e in ev)
    assert any(e["event"] == "prompt" and e["outcome"] == "warn" and e["actor"] == "user" for e in ev)
    d = next(e for e in ev if e["event"] == "decision")
    assert d["actor"] == "user" and d["outcome"] == "ok" and d["detail"]["answer"] == "yes"
    assert d["target"] == {"kind": "Deployment", "name": "redis", "namespace": "demo"}


def test_seq_monotonic_and_session_id_constant(tmp_path):
    ev = _full_session(tmp_path)
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    assert {e["session_id"] for e in ev} == {"s1"}


def test_check_result_shape_on_check_events(tmp_path):
    ev = _full_session(tmp_path)
    for e in ev:
        if e["event"] in ("policy_check", "dry_run", "log_check"):
            assert {"check", "outcome", "reason", "evidence", "secs"} <= set(e["detail"])
            assert e["outcome"] == e["detail"]["outcome"]
    v = next(e for e in ev if e["event"] == "fix_verify")
    assert v["detail"]["result"] in ("verified", "verified_with_warnings", "failed")
    for c in v["detail"]["checks"]:
        assert set(c) >= {"check", "outcome", "reason", "evidence", "secs"}


def test_check_result_caps_evidence():
    r = check_result("log_check", "warn", "x", ["y" * 1000] * 30, 1.234)
    assert len(r["evidence"]) == 10 and all(len(x) == 300 for x in r["evidence"]) and r["secs"] == 1.23


def test_appending_keeps_seq_monotonic(tmp_path):
    p = tmp_path / "s" / "audit.jsonl"
    AuditLog(p).write("a")
    AuditLog(p).write("b")
    assert [e["seq"] for e in read_events(p)] == [1, 2]


def test_reads_version_0_files(tmp_path):
    p = tmp_path / "old" / "audit.jsonl"
    p.parent.mkdir()
    v0 = [{"ts": "t", "event": "session_start", "namespace": "demo"},
          {"ts": "t", "event": "prompt_invalid", "answer": "maybe"},
          {"ts": "t", "event": "decision", "n": 1, "target": "Deployment/redis", "decision": "yes", "patch": []},
          {"ts": "t", "event": "fix_applied", "n": 1, "target": "Deployment/redis"},
          {"ts": "t", "event": "fix_verify_failed", "n": 1, "target": "Deployment/redis", "status": "verify failed: x"},
          {"ts": "t", "event": "fix_invalid", "n": 2, "target": "Deployment/redis"}]
    p.write_text("\n".join(json.dumps(e) for e in v0) + "\n")
    ev = read_events(p)
    assert [e["event"] for e in ev] == ["session_start", "prompt", "decision", "fix_apply", "fix_verify", "dry_run"]
    assert [e["outcome"] for e in ev] == ["n/a", "warn", "ok", "ok", "fail", "fail"]
    assert ev[2]["fix_n"] == 1 and ev[2]["target"]["name"] == "redis" and ev[2]["detail"]["answer"] == "yes"
    assert ev[4]["detail"]["result"] == "failed" and ev[4]["reason"] == "verify failed: x"
    assert all(e["schema_version"] == 0 for e in ev) and upgrade_v0(ev[0]) is ev[0]
