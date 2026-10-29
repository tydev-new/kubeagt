import json

from helpers import FakeCluster, node, redis_deployment, service

from kubeagt.audit import AuditLog
from kubeagt.fixer import FixSession
from kubeagt.llm import HintFollowingMock, ScriptedLLM, tool_call


class KC:
    def __init__(self):
        self.ran = []

    def run(self, argv, path="read"):
        from kubeagt.kube import validate_read_argv
        validate_read_argv(argv)
        self.ran.append(argv)
        return "ok"


def combined():
    return FakeCluster([redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432, mem_req="5Mi", mem_lim="5Mi")],
                       [service()], [node()])


def session(fc, answers=None, yes=False, llm=None, audit=None, out=None, **kw):
    answers = list(answers or [])
    lines = out if out is not None else []
    kw.setdefault("snapshot", fc.snapshot)
    return FixSession("demo", "kind-test", kw.pop("snapshot"), fc, KC(), audit or AuditLog(None), llm=llm,
                      ask=lambda prompt: answers.pop(0), yes=yes, out=lines.append, verify_timeout=1,
                      sleep=lambda s: None, **kw)


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
    # the rejected dry run is recorded, never shown or applied; then the rule fix is offered and applied
    assert [(r.source, r.decision) for r in rep.fixes] == [("llm", "invalid"), ("rule", "yes")]
    assert len(fc.applied) == 1 and fc.applied[0][3][0]["path"].endswith("tcpSocket/port")
    second_task = [m for m in llm.calls[1] if m["role"] == "user"][0]["content"]
    assert "rejected by server dry run" in second_task


# ---------------------------------------------------------------- flow chart conformance


def test_max_fixes_counts_applied_fixes_not_proposals():
    fc = combined()
    rep = session(fc, answers=["n", "y"], max_fixes=1).run()
    assert [r.decision for r in rep.fixes] == ["no", "yes"]  # a declined proposal does not use up the cap
    assert len(fc.applied) == 1 and rep.stopped == "fix cap reached (1 applied)"


def test_proposal_guard_stops_endless_rejected_proposals():
    fc = FakeCluster([redis_deployment(probe_port=5432)], [service()], [node()])

    def bad(i):
        return {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", {
            "kind": "Deployment", "name": "redis", "patch_type": "json",
            "patch": [{"op": "remove", "path": f"/spec/template/spec/nope{i}"}], "diagnosis": "x", "reasoning": "y"}, str(i))]}
    llm = ScriptedLLM([bad(i) for i in range(50)])
    rep = session(fc, answers=[], llm=llm, max_proposals=4).run()
    assert fc.applied == [] and len(rep.fixes) == 4
    assert all(r.decision == "invalid" for r in rep.fixes) and rep.stopped == "proposal cap reached (4 proposals)"


def test_one_snapshot_per_apply():
    fc = combined()
    calls = []

    def snap():
        calls.append(1)
        return fc.snapshot()
    rep = session(fc, yes=True, snapshot=snap, health_snapshot=fc.snapshot).run()
    assert len(fc.applied) == 3 and rep.healthy
    assert len(calls) == 1 + 3 + 1  # initial, one per apply, one final check


def test_no_rule_rerun_after_decline():
    fc = combined()
    calls = []

    def snap():
        calls.append(1)
        return fc.snapshot()
    rep = session(fc, answers=["n", "n", "n"], snapshot=snap).run()
    assert fc.applied == [] and len(rep.fixes) == 3
    assert len(calls) == 1  # nothing changed in the cluster, so the rules never re-ran


class RecordingMock(HintFollowingMock):
    def __init__(self):
        self.tasks = []

    def chat(self, messages, tools):
        if sum(1 for m in messages if m.get("role") == "tool") == 0:
            self.tasks.append(next(m["content"] for m in messages if m["role"] == "user"))
        return super().chat(messages, tools)


def _findings_part(task):
    return task.split("Deterministic findings (JSON):", 1)[1].split("</candidate_fixes>", 1)[0]


def test_skipped_findings_not_sent_to_llm_again():
    fc = combined()
    llm = RecordingMock()
    rep = session(fc, answers=["s", "y", "y"], llm=llm).run()
    skipped_rule = rep.fixes[0].rule
    assert rep.fixes[0].decision == "skip" and skipped_rule in _findings_part(llm.tasks[0])
    assert len(llm.tasks) >= 2
    for t in llm.tasks[1:]:
        assert skipped_rule not in _findings_part(t)


def test_repeated_ai_patch_is_recorded_and_loop_continues(tmp_path):
    fc = FakeCluster([redis_deployment(probe_port=5432)], [service()], [node()])
    named = {"kind": "Deployment", "name": "redis", "patch_type": "json",
             "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": "redis"}],
             "diagnosis": "use the named port", "reasoning": "r", "rule": "probe-port-mismatch"}
    llm = ScriptedLLM([
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", named, "1")]},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", named, "2")]},
    ])
    audit = AuditLog(tmp_path / "audit.jsonl")
    sess = session(fc, answers=["n", "y"], llm=llm, audit=audit)
    rep = sess.run()
    assert [r.decision for r in rep.fixes] == ["no", "repeat", "yes"]
    assert rep.fixes[1].status.startswith("rejected: repeats") and rep.fixes[2].source == "rule"
    assert rep.healthy and len(fc.applied) == 1
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e["event"] == "fix_repeat" for e in events)
    assert any("repeats a patch already tried" in h for h in sess.history)  # in session history for the AI
    assert len(llm.calls) == 2  # after the repeat the AI is not re-asked until the open errors change


def test_verified_only_when_rollout_completes():
    fc = combined()
    rep = session(fc, yes=True).run()
    # nodeSelector fix: the finding cleared, but the rollout is still blocked by the 5Mi memory limit
    assert rep.fixes[0].applied and not rep.fixes[0].verified
    assert rep.fixes[0].status.startswith("verify failed: rollout did not complete")
    assert rep.fixes[-1].verified and rep.fixes[-1].status.startswith("verified: rollout complete")
    assert rep.healthy


def test_one_dry_run_per_ai_proposal():
    fc = combined()
    rep = session(fc, yes=True, llm=HintFollowingMock()).run()
    assert len(fc.applied) == 3 and rep.healthy
    assert fc.dry_runs == 3
