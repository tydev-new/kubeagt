"""Relay mode: requests go to files, a fake responder (stand-in for a human/agent) answers."""
import json
import os
import threading
import time

import pytest

from kubeagt.agent import Budget, Investigator
from kubeagt.audit import AuditLog
from kubeagt.cli import main
from kubeagt.llm import RELAY_NAME, LLMConfig, LLMError, RelayClient, make_client, tool_call

from test_agent_llm import FIX, FakeKC, msg


class Responder(threading.Thread):
    """Watches the relay dir and answers req-N.json with answers[N-1] (a message or a callable(req))."""

    def __init__(self, d, answers):
        super().__init__(daemon=True)
        self.d, self.answers, self.seen = d, list(answers), []
        self.stop = False

    def run(self):
        n = 1
        while not self.stop and n <= len(self.answers):
            req = os.path.join(self.d, f"req-{n}.json")
            if os.path.exists(req):
                with open(req) as f:
                    body = json.load(f)
                self.seen.append(body)
                a = self.answers[n - 1]
                a = a(body) if callable(a) else a
                with open(os.path.join(self.d, f"resp-{n}.json"), "w") as f:
                    f.write(a if isinstance(a, str) else json.dumps(a))
                n += 1
            time.sleep(0.01)


def relay(tmp_path, **kw):
    return RelayClient(str(tmp_path), timeout=kw.pop("timeout", 5), poll=0.01, log=lambda m: None)


def test_relay_round_trip_reads_then_proposes(tmp_path):
    def second(req):
        tool_msgs = [m for m in req["messages"] if m["role"] == "tool"]
        assert tool_msgs and "redis-1" in tool_msgs[0]["content"]  # responder sees the real tool result
        return msg(tool_call("propose_fix", FIX, "2"))
    r = Responder(str(tmp_path), [msg(tool_call("kubectl", {"args": ["get", "pods"]}, "1")), second])
    r.start()
    kc = FakeKC()
    audit_path = tmp_path / "audit.jsonl"
    res = Investigator(relay(tmp_path), kc, AuditLog(audit_path)).investigate("ns demo")
    r.join(2)
    assert kc.ran == [["get", "pods"]]
    assert res.proposal and res.proposal.fix.patch == FIX["patch"]
    first = r.seen[0]
    assert first["messages"][0]["role"] == "system" and first["messages"][1]["content"] == "ns demo"
    assert {t["function"]["name"] for t in first["tools"]} == {"kubectl", "propose_fix", "finish"}
    calls = [json.loads(line) for line in audit_path.read_text().splitlines() if '"llm_call"' in line]
    assert len(calls) == 2 and all(c["model"] == RELAY_NAME for c in calls)


def test_relay_name_says_not_grok():
    assert "not Grok" in RELAY_NAME and "relay" in RELAY_NAME


def test_relay_same_allowlist_refuses_writes(tmp_path):
    r = Responder(str(tmp_path), [msg(tool_call("kubectl", {"args": ["delete", "pod", "redis-1"]}, "1")),
                                  msg(tool_call("kubectl", {"args": ["get", "secret", "x", "-o", "yaml"]}, "2")),
                                  msg(tool_call("finish", {"summary": "stop"}, "3"))])
    r.start()
    kc = FakeKC()
    res = Investigator(relay(tmp_path), kc).investigate("t")
    r.join(2)
    assert kc.ran == []
    outs = [m["content"] for m in res.transcript if m["role"] == "tool"]
    assert all(o.startswith("REFUSED") for o in outs) and len(outs) == 2
    assert res.stopped == "finished"


def test_relay_step_cap_applies(tmp_path):
    r = Responder(str(tmp_path), [msg(tool_call("kubectl", {"args": ["get", "pods"]}, str(i))) for i in range(10)])
    r.start()
    res = Investigator(relay(tmp_path), FakeKC(), budget=Budget(max_steps=3)).investigate("t")
    r.stop = True
    assert res.stopped == "step cap reached (3)" and res.tool_steps == 3
    assert not (tmp_path / "req-4.json").exists()


def test_relay_timeout_is_llm_error(tmp_path):
    c = RelayClient(str(tmp_path), timeout=0.1, poll=0.02, log=lambda m: None)
    with pytest.raises(LLMError, match="timed out"):
        c.chat([{"role": "user", "content": "x"}], [])
    res = Investigator(RelayClient(str(tmp_path), timeout=0.1, poll=0.02, log=lambda m: None), FakeKC()).investigate("t")
    assert res.stopped.startswith("LLM error: relay timed out") and res.proposal is None


def test_relay_accepts_choices_wrapper_and_dict_arguments(tmp_path):
    wrapped = {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "kubectl", "arguments": {"args": ["get", "pods"]}}}]}}]}
    r = Responder(str(tmp_path), [wrapped])
    r.start()
    out = relay(tmp_path).chat([], [])
    r.join(2)
    assert out.message["tool_calls"][0]["function"]["arguments"] == '{"args": ["get", "pods"]}'
    assert out.usage == {}


def test_relay_partial_write_is_retried(tmp_path):
    def slow_writer():
        while not (tmp_path / "req-1.json").exists():
            time.sleep(0.01)
        p = tmp_path / "resp-1.json"
        p.write_text('{"role": "assist')
        time.sleep(0.1)
        p.write_text(json.dumps({"role": "assistant", "content": "hello"}))
    t = threading.Thread(target=slow_writer, daemon=True)
    t.start()
    out = relay(tmp_path).chat([], [])
    assert out.message["content"] == "hello"


@pytest.mark.parametrize("bad", [{"role": "assistant"}, {"role": "assistant", "tool_calls": [{"function": {}}]}, ["x"]])
def test_relay_rejects_malformed(bad):
    with pytest.raises(LLMError):
        RelayClient.parse_response(bad)


def test_relay_numbering_continues_after_existing_files(tmp_path):
    (tmp_path / "req-7.json").write_text("{}")
    (tmp_path / "resp-7.json").write_text("{}")
    c = relay(tmp_path)
    assert c.n == 7



    def answer8():
        while not (tmp_path / "req-8.json").exists():
            time.sleep(0.01)
        (tmp_path / "resp-8.json").write_text(json.dumps({"role": "assistant", "content": "ok"}))
    threading.Thread(target=answer8, daemon=True).start()
    assert c.chat([], []).message["content"] == "ok"


def test_make_client_relay_and_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEAGT_HOME", str(tmp_path))
    monkeypatch.delenv("KUBEAGT_RELAY_DIR", raising=False)
    c = make_client("relay", LLMConfig.from_env())
    assert isinstance(c, RelayClient) and c.dir == str(tmp_path / "relay") and c.name == RELAY_NAME
    c2 = make_client("relay", LLMConfig.from_env(), str(tmp_path / "x"), 3)
    assert c2.dir == str(tmp_path / "x") and c2.timeout == 3


def test_cli_help_lists_relay(capsys):
    with pytest.raises(SystemExit):
        main(["fix", "--help"])
    out = capsys.readouterr().out
    assert "relay" in out and "--relay-dir" in out and "--relay-timeout" in out
