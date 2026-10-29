"""Every answer a human can give at the fix prompt, against the fake cluster. The same cases run on a
real kind cluster as the 10-16 eval scenarios; these tests also exercise the eval's audit checker."""
import json
import subprocess

import pytest
from helpers import FakeCluster, node, redis_deployment, service

from kubeagt.audit import AuditLog
from kubeagt.evals import Harness, _events, check_human
from kubeagt.fixer import FixSession, scripted_ask
from kubeagt.kube import KubeTarget
from kubeagt.llm import HintFollowingMock
from test_fixer import KC


def combined():
    return FakeCluster([redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432, mem_req="5Mi", mem_lim="5Mi")],
                       [service()], [node()])


def selector_only():
    return FakeCluster([redis_deployment(node_selector={"redisnode": "true"})], [service()], [node()])


def run(fc, answers, tmp_path, llm=None):
    path = tmp_path / "audit.jsonl"
    out = []
    rep = FixSession("demo", "kind-test", fc.snapshot, fc, KC(), AuditLog(path), llm=llm, ask=scripted_ask(answers),
                     out=out.append, verify_timeout=1, sleep=lambda s: None).run()
    return rep, _events(path), out


def kinds(ev, name):
    return [e for e in ev if e["event"] == name]


def assert_invariants(rep, ev, spec=None, mode="none"):
    bad = [c for c in check_human(spec or {}, mode, ev, rep, {}, {}) if not c["ok"]]
    assert not bad, bad


@pytest.mark.parametrize("llm", [None, HintFollowingMock()], ids=["rules", "mock"])
def test_yes_applies_and_verifies(tmp_path, llm):
    fc = selector_only()
    rep, ev, _ = run(fc, ["y"], tmp_path, llm)
    assert [d["decision"] for d in kinds(ev, "decision")] == ["yes"]
    assert len(fc.applied) == 1 and len(kinds(ev, "fix_verified")) == 1 and rep.healthy
    assert_invariants(rep, ev)


@pytest.mark.parametrize("llm", [None, HintFollowingMock()], ids=["rules", "mock"])
def test_no_is_never_reoffered_and_no_rule_rerun(tmp_path, llm):
    fc = selector_only()
    rep, ev, _ = run(fc, ["n", "y"], tmp_path, llm)    # the spare y would apply it if it were re-offered
    assert [d["decision"] for d in kinds(ev, "decision")] == ["no"]
    assert fc.applied == [] and len(kinds(ev, "rule_check")) == 1
    assert rep.stopped.startswith("no fix available")
    assert_invariants(rep, ev)


@pytest.mark.parametrize("llm", [None, HintFollowingMock()], ids=["rules", "mock"])
def test_skip_sets_finding_aside(tmp_path, llm):
    fc = combined()
    rep, ev, _ = run(fc, ["s", "y", "y"], tmp_path, llm)
    assert [d["decision"] for d in kinds(ev, "decision")] == ["skip", "yes", "yes"]
    end = kinds(ev, "session_end")[0]
    assert end["skipped"] == ["nodeselector-no-match:Deployment/redis:"]
    assert "nodeselector-no-match:Deployment/redis:" in end["open_findings"] and not rep.healthy
    for t in kinds(ev, "llm_task")[1:]:
        assert not any(k.startswith("nodeselector-no-match:") for k in t["findings"])
    assert_invariants(rep, ev)


def test_quit_stops_and_writes_nothing_after(tmp_path):
    fc = combined()
    rep, ev, _ = run(fc, ["y", "q", "y"], tmp_path)
    assert [d["decision"] for d in kinds(ev, "decision")] == ["yes", "quit"]
    assert len(fc.applied) == 1 and rep.stopped == "user quit"
    assert_invariants(rep, ev)


def test_invalid_input_reprompts_then_valid(tmp_path):
    fc = selector_only()
    rep, ev, out = run(fc, ["maybe", "", "Y"], tmp_path)
    assert len(kinds(ev, "prompt_invalid")) == 2 and out.count("  please answer y, n, s, or q") == 2
    assert [d["decision"] for d in kinds(ev, "decision")] == ["yes"] and len(fc.applied) == 1


def test_too_many_invalid_answers_quits(tmp_path):
    fc = selector_only()
    rep, ev, _ = run(fc, ["?"] * 20, tmp_path)
    assert fc.applied == [] and rep.stopped == "user quit" and len(kinds(ev, "prompt_invalid")) == 6


def test_eof_is_quit_and_applies_nothing(tmp_path):
    fc = combined()
    rep, ev, _ = run(fc, [], tmp_path)
    assert len(kinds(ev, "prompt_eof")) == 1 and [d["decision"] for d in kinds(ev, "decision")] == ["quit"]
    assert fc.applied == [] and rep.stopped == "user quit"
    assert not [e for e in ev if e["event"] == "kubectl" and e.get("path") == "write"]


def test_tty_ask_eof_from_closed_stdin(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    from kubeagt import fixer
    fc = selector_only()
    rep = FixSession("demo", "kind-test", fc.snapshot, fc, KC(), AuditLog(None), ask=fixer.tty_ask,
                     out=lambda s: None, verify_timeout=1, sleep=lambda s: None).run()
    assert fc.applied == [] and rep.stopped == "user quit"


@pytest.mark.parametrize("llm", [None, HintFollowingMock()], ids=["rules", "mock"])
def test_mixed_sequence_on_redis(tmp_path, llm):
    fc = combined()
    rep, ev, _ = run(fc, ["n", "y", "s", "y"], tmp_path, llm)
    assert [d["decision"] for d in kinds(ev, "decision")] == ["no", "yes", "skip"]
    assert [a[3][0]["path"] for a in fc.applied] == ["/spec/template/spec/containers/0/resources/requests/memory"]
    spec = {"expect": {"decisions": ["no", "yes", "skip"], "events": {"fix_applied": 1},
                       "open": ["nodeselector-no-match", "probe-port-mismatch"], "stopped": "no fix available"}}
    assert_invariants(rep, ev, spec)


def test_checker_catches_a_rule_rerun_after_no():
    ev = [{"event": "rule_check"}, {"event": "decision", "decision": "no", "n": 1, "rule": "r", "target": "Deployment/x",
                                    "patch_type": "json", "patch": []}, {"event": "rule_check"}, {"event": "session_end"}]

    class R:
        stopped, remaining_findings = "", []
    bad = [c["check"] for c in check_human({}, "none", ev, R(), {}, {}) if not c["ok"]]
    assert any("no rule re-run after no" in b for b in bad)


class FakeHarness(Harness):
    def __init__(self, namespaces, labels):
        super().__init__(KubeTarget(""), print)
        self.ns, self.labels = namespaces, labels

    def _k(self, args, timeout=120, check=True):
        if args[:2] == ["get", "namespaces"]:
            return subprocess.CompletedProcess(args, 0, "\n".join("namespace/" + n for n in self.ns), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"items": [
            {"metadata": {"name": k, "labels": v}} for k, v in self.labels.items()]}), "")


def test_preflight_clean():
    h = FakeHarness(["default", "kube-system"], {"n1": {"a": "1"}})
    assert h.preflight({"n1": {"a": "1"}}, wait=0) is None


def test_preflight_stale_namespace():
    h = FakeHarness(["kubeagt-eval-01-nodeselector"], {"n1": {}})
    assert "stale namespace" in h.preflight({"n1": {}}, wait=0)


def test_preflight_node_label_drift():
    h = FakeHarness([], {"n1": {"a": "1", "redisnode": "true"}})
    why = h.preflight({"n1": {"a": "1"}}, wait=0)
    assert "node labels differ" in why and "redisnode" in why
