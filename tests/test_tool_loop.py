"""Inner tool-error loop in the AI investigation, step budget counting, and LLM response checks."""
import json

import pytest

from kubeagt.agent import Budget, Investigator, ToolLimits
from kubeagt.audit import AuditLog, read_events
from kubeagt.kube import validate_read_argv
from kubeagt.llm import LLMError, parse_completion, ScriptedLLM, tool_call

FIX = {"kind": "Deployment", "name": "redis", "patch_type": "json",
       "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}],
       "diagnosis": "probe on wrong port", "reasoning": "redis listens on 6379"}


class KC:
    """Fake read-only kubectl: a pod named missing* exits 1, anything off the allowlist is refused."""

    def __init__(self):
        self.ran = []

    def run(self, argv, path="read"):
        validate_read_argv(argv)
        self.ran.append(list(argv))
        if any(str(a).startswith("missing") for a in argv):
            return "[exit 1] Error from server (NotFound): pods not found"
        return "NAME READY\nredis-1 1/1"


def turn(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def kube(args, i):
    return tool_call("kubectl", {"args": args}, str(i))


def bad(i):  # a distinct failing call each time, so the repeat rule does not apply
    return kube(["get", "pod", f"missing-{i}"], i)


def user_msgs(messages):
    return [m["content"] for m in messages if m["role"] == "user"][1:]


def test_nudge_after_three_errors_in_a_row():
    llm = ScriptedLLM([turn(bad(1)), turn(bad(2)), turn(bad(3)), turn(tool_call("finish", {"summary": "ok"}, "f"))])
    res = Investigator(llm, KC()).investigate("x")
    nudges = user_msgs(llm.calls[3])
    assert len(nudges) == 1 and "last 3 kubectl calls failed" in nudges[0] and "finish" in nudges[0]
    assert not user_msgs(llm.calls[2])            # no nudge after only 2 errors
    assert res.stopped == "finished"


def test_stop_after_five_errors_with_audit_event(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    llm = ScriptedLLM([turn(bad(i)) for i in range(10)])
    kc = KC()
    res = Investigator(llm, kc, audit=audit).investigate("x")
    assert res.stopped.startswith("tool error limit") and len(kc.ran) == 5 and len(llm.calls) == 5
    ev = [e for e in read_events(tmp_path / "audit.jsonl") if e["event"] == "llm_task"]
    assert len(ev) == 1 and ev[0]["outcome"] == "fail" and ev[0]["reason"].startswith("tool error limit")
    assert ev[0]["schema_version"] == 1 and ev[0]["detail"]["consecutive_errors"] == 5 and ev[0]["detail"]["limit"] == 5


def test_success_resets_the_error_count():
    script = [turn(bad(1)), turn(bad(2)), turn(kube(["get", "pods"], 3)), turn(bad(4)), turn(bad(5)),
              turn(bad(6)), turn(bad(7)), turn(tool_call("propose_fix", FIX, "p"))]
    llm = ScriptedLLM(script)
    res = Investigator(llm, KC()).investigate("x")
    assert res.proposal is not None               # 2 + reset + 4 errors never reaches 5
    assert not any("failed" in u for u in user_msgs(llm.calls[3]))
    assert any("last 3 kubectl calls failed" in u for u in user_msgs(llm.calls[6]))


def test_exact_repeat_of_a_failed_call_is_refused_and_counts():
    same = ["get", "pod", "missing"]
    llm = ScriptedLLM([turn(kube(same, 1)), turn(kube(same, 2)), turn(tool_call("finish", {"summary": "s"}, "f"))])
    kc = KC()
    Investigator(llm, kc, limits=ToolLimits(nudge_after_errors=2)).investigate("x")
    assert kc.ran == [same]                        # second identical call never ran
    tool = [m["content"] for m in llm.calls[2] if m["role"] == "tool"]
    assert tool[1].startswith("REFUSED: this exact kubectl call just failed")
    assert any("last 2 kubectl calls failed" in u for u in user_msgs(llm.calls[2]))   # the refusal counted


def test_repeat_after_success_is_allowed():
    llm = ScriptedLLM([turn(kube(["get", "pod", "missing"], 1)), turn(kube(["get", "pods"], 2)), turn(kube(["get", "pod", "missing"], 3)),
                       turn(tool_call("finish", {"summary": "s"}, "f"))])
    kc = KC()
    Investigator(llm, kc).investigate("x")
    assert kc.ran.count(["get", "pod", "missing"]) == 2


def test_per_turn_tool_call_cap():
    calls = [kube(["get", "pods", f"p{i}"], i) for i in range(7)]
    llm = ScriptedLLM([turn(*calls), turn(tool_call("finish", {"summary": "s"}, "f"))])
    kc = KC()
    Investigator(llm, kc).investigate("x")
    assert len(kc.ran) == 5
    tool = [m["content"] for m in llm.calls[1] if m["role"] == "tool"]
    assert len(tool) == 7 and all(t.startswith("ERROR: too many tool calls in one turn") for t in tool[5:])


def test_executed_calls_count_toward_the_step_budget():
    batch = lambda k: turn(*[kube(["get", "pods", f"p{k}{i}"], f"{k}{i}") for i in range(4)])
    llm = ScriptedLLM([batch(0), batch(1), batch(2), batch(3)])
    kc = KC()
    res = Investigator(llm, kc, budget=Budget(max_steps=6)).investigate("x")
    assert len(kc.ran) == 6 and len(llm.calls) == 2     # 4 calls, then 2 more before the budget ran out
    assert "step cap" in res.stopped and res.tool_steps == 6
    tool = [m["content"] for m in res.transcript if m["role"] == "tool"]
    assert sum(t.startswith("ERROR: step budget used up") for t in tool) >= 1


def test_proposal_needs_diagnosis_and_reasoning():
    empty = dict(FIX, diagnosis=" ", reasoning="")
    llm = ScriptedLLM([turn(tool_call("propose_fix", empty, "1")), turn(tool_call("propose_fix", FIX, "2"))])
    res = Investigator(llm, KC()).investigate("x")
    fb = [m["content"] for m in llm.calls[1] if m["role"] == "tool"][0]
    assert fb.startswith("INVALID") and "diagnosis" in fb and "reasoning" in fb and res.proposal is not None


def test_finish_reason_length_is_recorded(tmp_path):
    class Cut(ScriptedLLM):
        def chat(self, messages, tools):
            r = super().chat(messages, tools)
            r.finish_reason = "length"
            return r
    audit = AuditLog(tmp_path / "audit.jsonl")
    llm = Cut([{"role": "assistant", "content": "The problem is"}, {"role": "assistant", "content": "the probe"}])
    res = Investigator(llm, KC(), audit=audit).investigate("x")
    ev = [e for e in read_events(tmp_path / "audit.jsonl") if e["event"] == "llm_call"]
    assert all(e["outcome"] == "warn" and e["detail"]["finish_reason"] == "length" for e in ev)
    assert "cut off" in res.stopped and "cut off" in user_msgs(llm.calls[1])[0]


@pytest.mark.parametrize("payload,needle", [
    ({}, "no choices"),
    ({"choices": []}, "no choices"),
    ({"choices": [{"finish_reason": "stop"}]}, "no message"),
    ({"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}, "empty API response"),
    ({"choices": [{"message": {"role": "assistant", "tool_calls": "x"}}]}, "tool_calls is not a list"),
    ({"error": {"message": "upstream overloaded"}}, "upstream overloaded"),
    ([1, 2], "not a JSON object"),
])
def test_malformed_or_empty_api_responses_raise(payload, needle):
    with pytest.raises(LLMError, match=needle):
        parse_completion(payload)


def test_good_completion_parses_with_finish_reason():
    r = parse_completion({"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "length"}],
                          "usage": {"prompt_tokens": 1}})
    assert r.message["content"] == "hi" and r.finish_reason == "length" and r.usage == {"prompt_tokens": 1}
