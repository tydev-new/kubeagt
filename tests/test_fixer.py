import json

from helpers import FakeCluster, node, redis_deployment, service

from kdiag.audit import AuditLog
from kdiag.fixer import FixSession
from kdiag.llm import HintFollowingMock, ScriptedLLM, tool_call


class KC:
    def __init__(self):
        self.ran = []

    def run(self, argv, path="read"):
        from kdiag.kube import validate_read_argv
        validate_read_argv(argv)
        self.ran.append(argv)
        return "ok"


def combined():
    return FakeCluster([redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432, mem_req="5Mi", mem_lim="5Mi")],
                       [service()], [node()])


def session(fc, answers=None, yes=False, llm=None, audit=None, out=None):
    answers = list(answers or [])
    lines = out if out is not None else []
    return FixSession("demo", "kind-test", fc.snapshot, fc, KC(), audit or AuditLog(None), llm=llm,
                      ask=lambda prompt: answers.pop(0), yes=yes, out=lines.append, verify_timeout=1,
                      sleep=lambda s: None)


def test_combined_redis_fixed_one_at_a_time():
    fc = combined()
    out = []
    rep = session(fc, answers=["y", "y", "y"], out=out).run()
    assert [a[3][0]["path"] for a in fc.applied] == [
        "/spec/template/spec/nodeSelector",
        "/spec/template/spec/containers/0/resources/requests/memory",
        "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port"]
    assert rep.healthy and all(r.applied for r in rep.fixes)
    c = fc.objs[("Deployment", "redis")]["spec"]["template"]["spec"]
    assert "nodeSelector" not in c and c["containers"][0]["readinessProbe"]["tcpSocket"]["port"] == 6379
    assert c["containers"][0]["resources"] == {"requests": {"memory": "128Mi"}, "limits": {"memory": "512Mi"}}
    text = "\n".join(out)
    assert text.count("Apply this fix?") == 0  # prompts go through ask(), not out()
    assert "Diagnosis:" in text and "Evidence:" in text and "Why:" in text and "+            port: 6379" in text


def test_nothing_applied_without_yes():
    fc = combined()
    rep = session(fc, answers=["n", "s", "n"]).run()
    assert fc.applied == [] and not rep.healthy
    assert [r.decision for r in rep.fixes] == ["no", "skip", "no"]


def test_quit_stops_immediately():
    fc = combined()
    rep = session(fc, answers=["y", "q"]).run()
    assert len(fc.applied) == 1 and rep.stopped == "user quit"


def test_yes_flag_applies_all():
    fc = combined()
    rep = session(fc, yes=True).run()
    assert len(fc.applied) == 3 and rep.healthy


def test_healthy_namespace_proposes_nothing():
    fc = FakeCluster([redis_deployment()], [service()], [node()])
    rep = session(fc, answers=[]).run()
    assert rep.fixes == [] and rep.healthy


def test_crashloop_command_fixed():
    fc = FakeCluster([redis_deployment(command=["redis-srv"])], [service()], [node()])
    rep = session(fc, yes=True).run()
    assert rep.healthy and "command" not in fc.objs[("Deployment", "redis")]["spec"]["template"]["spec"]["containers"][0]


def test_llm_led_session_with_mock_provider():
    fc = combined()
    rep = session(fc, yes=True, llm=HintFollowingMock()).run()
    assert rep.healthy and len(fc.applied) == 3
    assert rep.tool_steps == 6  # two read-only calls per investigation
    assert all(r.source == "llm" for r in rep.fixes)


def test_llm_alternative_after_rejection_and_audit_redaction(tmp_path):
    fc = FakeCluster([redis_deployment(probe_port=5432, env=[{"name": "REDIS_PASSWORD", "value": "hunter2"}])], [service()], [node()])
    alt = {"kind": "Deployment", "name": "redis", "patch_type": "json",
           "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": "redis"}],
           "diagnosis": "use the named port", "reasoning": "named port survives port changes", "rule": "probe-port-mismatch"}
    first = dict(alt, patch=[{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}],
                 diagnosis="use 6379", reasoning="password=hunter2 should never be logged")
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", first, "1")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", alt, "2")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("finish", {"summary": "done"}, "3")]},
    ])
    audit = AuditLog(tmp_path / "audit.jsonl")
    rep = session(fc, answers=["n", "y"], llm=llm, audit=audit).run()
    assert [r.decision for r in rep.fixes] == ["no", "yes"]
    assert fc.applied[0][3][0]["value"] == "redis"
    # the rejection was fed back to the model
    second_task = [m for m in llm.calls[1] if m["role"] == "user"][0]["content"]
    assert "user rejected fix" in second_task
    log = (tmp_path / "audit.jsonl").read_text()
    assert "hunter2" not in log
    events = [json.loads(line)["event"] for line in log.splitlines()]
    assert "decision" in events and "fix_applied" in events and "session_end" in events


def test_failed_verification_is_fed_back():
    fc = FakeCluster([redis_deployment(probe_port=5432)], [service()], [node()])
    wrong = {"kind": "Deployment", "name": "redis", "patch_type": "json",
             "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/periodSeconds", "value": 10}],
             "diagnosis": "probe too aggressive", "reasoning": "slow it down"}
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", wrong, "1")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("finish", {"summary": "stuck"}, "2")]},
    ])
    rep = session(fc, answers=["y", "y"], llm=llm).run()
    assert rep.fixes[0].applied and not rep.fixes[0].verified
    second_task = [m for m in llm.calls[1] if m["role"] == "user"][0]["content"]
    assert "verification failed" in second_task and "probe-port-mismatch" in second_task
    # after the LLM gave up, the deterministic fix is still offered and fixes it
    assert rep.healthy and rep.fixes[1].source == "rule"


def test_invalid_patch_is_not_shown_or_applied():
    fc = FakeCluster([redis_deployment(probe_port=5432)], [service()], [node()])
    bad = {"kind": "Deployment", "name": "redis", "patch_type": "json",
           "patch": [{"op": "remove", "path": "/spec/template/spec/doesNotExist"}], "diagnosis": "x", "reasoning": "y"}
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", bad, "1")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("finish", {"summary": "ok"}, "2")]},
    ])
    rep = session(fc, answers=["y"], llm=llm).run()
    assert fc.dry_runs >= 1 and len(fc.applied) == 1 and rep.fixes[0].source == "rule"
