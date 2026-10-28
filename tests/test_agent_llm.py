import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kdiag.agent import Budget, Investigator
from kdiag.kube import validate_read_argv
from kdiag.llm import HintFollowingMock, LLMConfig, LLMError, OpenAICompatClient, ScriptedLLM, make_client, tool_call


class FakeKC:
    """Same contract as ReadOnlyKubectl.run: validate, then 'execute'."""

    def __init__(self):
        self.ran = []

    def run(self, argv, path="read"):
        validate_read_argv(argv)
        self.ran.append(list(argv))
        return "NAME READY STATUS\nredis-1 0/1 Running"


def msg(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


FIX = {"kind": "Deployment", "name": "redis", "patch_type": "json",
       "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}],
       "diagnosis": "probe on wrong port", "evidence": ["Readiness probe failed: 5432 refused"], "reasoning": "redis listens on 6379",
       "rule": "probe-port-mismatch"}


def test_agent_reads_then_proposes():
    llm = ScriptedLLM([msg(tool_call("kubectl", {"args": ["get", "pods"]}, "1")), msg(tool_call("propose_fix", FIX, "2"))])
    kc = FakeKC()
    res = Investigator(llm, kc).investigate("ns demo")
    assert kc.ran == [["get", "pods"]]
    assert res.proposal.fix.source == "llm" and res.proposal.fix.patch == FIX["patch"]
    assert res.tool_steps == 1


def test_agent_write_attempts_are_refused_and_reported_back():
    llm = ScriptedLLM([
        msg(tool_call("kubectl", {"args": ["delete", "pod", "redis-1"]}, "1"),
            tool_call("kubectl", {"args": ["get", "secrets"]}, "2"),
            tool_call("kubectl", {"args": ["get", "pods", "-n", "kube-system"]}, "3")),
        msg(tool_call("finish", {"summary": "gave up"}, "4"))])
    kc = FakeKC()
    res = Investigator(llm, kc).investigate("x")
    assert kc.ran == []
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert all(m["content"].startswith("REFUSED") for m in tool_msgs) and len(tool_msgs) == 3
    assert res.proposal is None and res.summary == "gave up"


def test_agent_invalid_proposal_gets_feedback():
    bad = dict(FIX, kind="Secret")
    llm = ScriptedLLM([msg(tool_call("propose_fix", bad, "1")), msg(tool_call("propose_fix", FIX, "2"))])
    res = Investigator(llm, FakeKC()).investigate("x")
    assert res.proposal is not None
    fb = [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert fb.startswith("INVALID") and "cannot be patched" in fb


def test_agent_dry_run_validation_feedback():
    seen = []

    def validate(fix):
        seen.append(fix)
        return "field is immutable" if len(seen) == 1 else None
    llm = ScriptedLLM([msg(tool_call("propose_fix", FIX, "1")), msg(tool_call("propose_fix", FIX, "2"))])
    res = Investigator(llm, FakeKC(), validate=validate).investigate("x")
    assert res.proposal and len(seen) == 2


def test_step_cap():
    llm = ScriptedLLM([msg(tool_call("kubectl", {"args": ["get", "pods"]}, str(i))) for i in range(20)])
    res = Investigator(llm, FakeKC(), budget=Budget(max_steps=3)).investigate("x")
    assert res.proposal is None and "step cap" in res.stopped and len(llm.calls) == 3


def test_token_and_spend_caps():
    llm = ScriptedLLM([msg(tool_call("kubectl", {"args": ["get", "pods"]}, str(i))) for i in range(20)],
                      usage={"prompt_tokens": 600, "completion_tokens": 100})
    res = Investigator(llm, FakeKC(), budget=Budget(max_tokens=1000)).investigate("x")
    assert "token cap" in res.stopped and len(llm.calls) == 2
    llm = ScriptedLLM([msg(tool_call("kubectl", {"args": ["get", "pods"]}, str(i))) for i in range(20)],
                      usage={"prompt_tokens": 1_000_000, "completion_tokens": 0})
    res = Investigator(llm, FakeKC(), budget=Budget(max_cost_usd=1.0, max_tokens=10**9), cfg=LLMConfig()).investigate("x")
    assert "spend cap" in res.stopped and len(llm.calls) == 1


def test_text_answer_is_nudged_then_accepted():
    llm = ScriptedLLM([{"role": "assistant", "content": "I think it's the probe"}, {"role": "assistant", "content": "really"}])
    res = Investigator(llm, FakeKC()).investigate("x")
    assert res.proposal is None and res.summary == "really"


def test_hint_mock_proposes_first_candidate():
    hints = [{"rule": "probe-port-mismatch", "kind": "Deployment", "name": "redis", "patch_type": "json", "patch": FIX["patch"]}]
    task = "ns\n<candidate_fixes>\n" + json.dumps(hints) + "\n</candidate_fixes>"
    kc = FakeKC()
    res = Investigator(HintFollowingMock(), kc).investigate(task)
    assert len(kc.ran) == 2 and res.proposal.fix.patch == FIX["patch"]


class _Handler(BaseHTTPRequestHandler):
    requests = []
    status = [200]

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        code = _Handler.status.pop(0) if len(_Handler.status) > 1 else _Handler.status[0]
        if code != 200:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"error":"busy"}')
            return
        resp = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            tool_call("kubectl", {"args": ["get", "pods"]}, "c1")]}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.requests = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_openai_compatible_client_request_shape(server, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "k-123")
    _Handler.status = [200]
    cfg = LLMConfig(base_url=server, model="grok-test", api_key_env="MY_TEST_KEY")
    r = OpenAICompatClient(cfg).chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "kubectl"}}])
    req = _Handler.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer k-123"
    assert req["body"]["model"] == "grok-test" and req["body"]["tools"][0]["function"]["name"] == "kubectl"
    assert r.message["tool_calls"][0]["function"]["name"] == "kubectl" and r.usage["total_tokens"] == 60


def test_client_retries_then_fails(server, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "k")
    monkeypatch.setattr("time.sleep", lambda s: None)
    _Handler.status = [503, 200]
    cfg = LLMConfig(base_url=server, api_key_env="MY_TEST_KEY")
    assert OpenAICompatClient(cfg).chat([], []).usage["total_tokens"] == 60
    _Handler.status = [401]
    with pytest.raises(LLMError):
        OpenAICompatClient(cfg).chat([], [])


def test_make_client_modes(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    cfg = LLMConfig.from_env()
    assert cfg.base_url == "https://api.x.ai/v1" and cfg.api_key_env == "XAI_API_KEY"
    assert make_client("auto", cfg) is None
    assert make_client("none", cfg) is None
    assert isinstance(make_client("mock", cfg), HintFollowingMock)
    with pytest.raises(LLMError):
        make_client("openai", cfg)
    monkeypatch.setenv("XAI_API_KEY", "x")
    assert isinstance(make_client("auto", cfg), OpenAICompatClient)


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("KDIAG_LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("KDIAG_LLM_MODEL", "gpt-x")
    monkeypatch.setenv("KDIAG_LLM_API_KEY_ENV", "OPENAI_API_KEY")
    cfg = LLMConfig.from_env(model="override")
    assert (cfg.base_url, cfg.model, cfg.api_key_env) == ("https://api.openai.com/v1", "override", "OPENAI_API_KEY")
