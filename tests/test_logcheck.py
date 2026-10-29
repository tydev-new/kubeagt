"""Log check in verify: outcomes, baseline diff, no_signal, fetch errors, redaction, caps, and the fix loop."""
import json

from helpers import FakeCluster, node, redis_deployment, service

from kubeagt import logcheck
from kubeagt.audit import AuditLog, read_events
from kubeagt.fixer import FixSession, scripted_ask
from kubeagt.logcheck import NO_SIGNAL, capture_baseline, run_log_check
from test_fixer import KC

SEL = {"matchLabels": {"app": "api"}}


def pod(name, containers=("api",), restarts=0, state=None, last=None, labels=None):
    cs = [{"name": c, "restartCount": restarts, "state": state or {"running": {}},
           "lastState": {"terminated": last} if last else {}} for c in containers]
    return {"metadata": {"name": name, "labels": labels or {"app": "api"}},
            "spec": {"containers": [{"name": c} for c in containers]}, "status": {"containerStatuses": cs}}


class Src:
    """Fake log source. phases: pod lists returned in order (the last one repeats). logs: {(pod, container, prev): text}
    or a (ok, text, err) tuple."""

    def __init__(self, phases, logs, selector=SEL):
        self.phases, self.logs_map, self.sel = list(phases), logs, selector
        self.pod_calls = 0

    def selector(self, kind, name):
        return self.sel

    def pods(self):
        i = min(self.pod_calls, len(self.phases) - 1)
        self.pod_calls += 1
        return self.phases[i]

    def logs(self, p, c, previous=False, tail=500):
        v = self.logs_map.get((p, c, previous), "")
        return v if isinstance(v, tuple) else (True, v, "")


def check(before_pods, before_logs, after_pods, after_logs, window=10, sleeps=None):
    src = Src([before_pods], before_logs)
    base = capture_baseline(src, "Deployment", "api")
    src.phases, src.logs_map, src.pod_calls = [after_pods], after_logs, 0
    return run_log_check(src, "Deployment", "api", base, window, (sleeps.append if sleeps is not None else (lambda s: None)))


def test_pass_on_new_info_lines():
    r = check([pod("api-1")], {("api-1", "api", False): "booting"}, [pod("api-2")],
              {("api-2", "api", False): "api: started, upstream http://x\nlistening on 8080"})
    assert r["outcome"] == "ok" and r["check"] == "log_check" and r["new_lines"] == 2 and not r["no_signal"]
    assert set(r) >= {"check", "outcome", "reason", "evidence", "secs"}


def test_warn_on_new_error_lines():
    r = check([], {}, [pod("api-2")], {("api-2", "api", False): "started\nERROR cache warmup timed out\nlevel=error msg=retry"})
    assert r["outcome"] == "warn" and "2 new error-level" in r["reason"]
    assert r["evidence"] == ["ERROR cache warmup timed out", "level=error msg=retry"]


def test_fail_on_fatal_and_missing_config():
    r = check([], {}, [pod("api-2")], {("api-2", "api", False): "FATAL: API_URL not set"})
    assert r["outcome"] == "fail" and "FATAL: API_URL not set" in r["evidence"]
    for line in ("config file /etc/app.yaml missing", "DATABASE_URL is not set", "panic: runtime error: index out of range",
                 "Traceback (most recent call last):", "goroutine 1 [running]:"):
        assert check([], {}, [pod("p")], {("p", "api", False): line})["outcome"] == "fail", line


def test_fail_on_restart_in_window_and_stops_early():
    sleeps = []
    crashed = pod("api-2", restarts=1, state={"waiting": {"reason": "CrashLoopBackOff"}}, last={"exitCode": 1, "reason": "Error"})
    src = Src([[pod("api-1")]], {})
    base = capture_baseline(src, "Deployment", "api")
    src.phases, src.pod_calls = [[pod("api-2")], [crashed]], 0
    src.logs_map = {("api-2", "api", False): "", ("api-2", "api", True): "booting"}
    r = run_log_check(src, "Deployment", "api", base, 30, sleeps.append)
    assert r["outcome"] == "fail" and "exit or restart" in r["reason"]
    assert any("restarted 1 time" in e and "exit code 1" in e for e in r["evidence"])
    assert len(sleeps) == 1          # stopped watching as soon as the restart showed up (30s window, 5s polls)


def test_fail_on_terminated_container():
    r = check([], {}, [pod("p", state={"terminated": {"exitCode": 2, "reason": "Error"}})], {("p", "api", False): "bye"})
    assert r["outcome"] == "fail" and "exited (exit code 2" in r["evidence"][0]


def test_no_signal_when_pods_log_nothing():
    r = check([pod("api-1")], {}, [pod("api-1")], {("api-1", "api", False): ""})
    assert r["outcome"] == "warn" and r["reason"] == NO_SIGNAL and r["no_signal"] is True


def test_no_signal_when_no_pods():
    r = check([], {}, [], {})
    assert r["outcome"] == "warn" and r["reason"] == NO_SIGNAL and r["no_signal"]


def test_logs_fetch_error_is_warn():
    r = check([], {}, [pod("p")], {("p", "api", False): (False, "", 'container "api" in pod "p" is waiting to start')})
    assert r["outcome"] == "warn" and r["reason"].startswith("could not fetch logs") and r["fetch_errors"] == 1


def test_source_exception_is_warn_not_crash():
    class Boom(Src):
        def pods(self):
            raise RuntimeError("connection refused")
    r = run_log_check(Boom([], {}), "Deployment", "api", {"lines": set(), "restarts": {}, "selector": SEL}, 5, lambda s: None)
    assert r["outcome"] == "warn" and "connection refused" in r["reason"]


def test_pre_existing_errors_not_counted():
    old = "ERROR disk at 91% (request 4411)\nERROR upstream 10.0.0.7 slow"
    new = "ERROR disk at 93% (request 4412)\nERROR upstream 10.0.0.9 slow\nready"
    r = check([pod("api-1")], {("api-1", "api", False): old}, [pod("api-2")], {("api-2", "api", False): new})
    assert r["outcome"] == "ok" and r["new_lines"] == 1


def test_multiple_pods_and_containers():
    after = [pod("a", ("api", "sidecar")), pod("b", ("api", "sidecar"))]
    logs = {(p, c, False): "ok" for p in "ab" for c in ("api", "sidecar")}
    logs[("b", "sidecar", False)] = "ok\nERROR proxy upstream reset"
    r = check([], {}, after, logs)
    assert r["outcome"] == "warn" and r["evidence"] == ["ERROR proxy upstream reset"] and r["pods"] == ["a", "b"]


def test_redaction_and_evidence_caps():
    noisy = "\n".join(f"ERROR request {i} failed password=hunter{i} " + "x" * 900 for i in range(40))
    r = check([], {}, [pod("p")], {("p", "api", False): noisy})
    assert r["outcome"] == "warn" and len(r["evidence"]) == 10 and all(len(e) <= 300 for e in r["evidence"])
    assert "hunter" not in json.dumps(r) and "<redacted>" in r["evidence"][0]


def test_very_large_logs_are_capped():
    big = "\n".join(f"info line {i}" for i in range(200_000))
    r = check([], {}, [pod("p")], {("p", "api", False): big})
    assert r["outcome"] == "ok" and r["lines_seen"] < 20_000


def test_skip_for_kinds_without_pods():
    r = run_log_check(Src([], {}), "ConfigMap", "cfg", capture_baseline(Src([], {}), "ConfigMap", "cfg"), 5, lambda s: None)
    assert r["outcome"] == "skip"


# ---------------------------------------------------------------- in the fix loop
class ClusterLogs:
    """Log source over FakeCluster pods: the pod produced after the fix logs `after_text`."""

    def __init__(self, fc, after_text):
        self.fc, self.after_text = fc, after_text

    def selector(self, kind, name):
        return {"matchLabels": {"app": name}} if kind == "Deployment" else None

    def pods(self):
        return self.fc.snapshot().items("pods")

    def logs(self, p, c, previous=False, tail=500):
        return True, (self.after_text if self.fc.applied else ""), ""


def _run(tmp_path, text):
    fc = FakeCluster([redis_deployment(node_selector={"redisnode": "true"})], [service()], [node()])
    out = []
    sess = FixSession("demo", "kind-test", fc.snapshot, fc, KC(), AuditLog(tmp_path / "audit.jsonl"), ask=scripted_ask(["y"]),
                      out=out.append, verify_timeout=1, sleep=lambda s: None, log_source=ClusterLogs(fc, text),
                      log_watch_seconds=5)
    rep = sess.run()
    return rep, sess, "\n".join(out), read_events(tmp_path / "audit.jsonl")


def test_loop_verified_with_warnings_on_no_signal(tmp_path):
    rep, sess, out, ev = _run(tmp_path, "")
    r = rep.fixes[0]
    assert r.verified and r.verify_result == "verified_with_warnings" and NO_SIGNAL in r.status
    assert "verify: verified with warnings" in out and f"log_check: warn: {NO_SIGNAL}" in out
    assert any("verified with warnings" in h and NO_SIGNAL in h for h in sess.history)
    v = [e for e in ev if e["event"] == "fix_verify"][0]
    assert v["outcome"] == "warn" and v["detail"]["result"] == "verified_with_warnings"
    assert [c["check"] for c in v["detail"]["checks"]] == ["rollout_check", "log_check", "rule_recheck"]
    lc = [e for e in ev if e["event"] == "log_check"][0]
    assert lc["outcome"] == "warn" and lc["detail"]["no_signal"] and lc["fix_n"] == 1


def test_loop_verified_when_logs_are_clean(tmp_path):
    rep, _, out, _ = _run(tmp_path, "1:M Ready to accept connections tcp")
    assert rep.fixes[0].verify_result == "verified" and "verify: verified" in out


def test_loop_failed_on_fatal_line_is_fed_back(tmp_path):
    rep, sess, out, ev = _run(tmp_path, "FATAL: REDIS_URL not set")
    r = rep.fixes[0]
    assert r.applied and not r.verified and r.verify_result == "failed" and r.status.startswith("verify failed: log check")
    assert "| FATAL: REDIS_URL not set" in out
    assert any("verification failed" in h and "FATAL: REDIS_URL not set" in h for h in sess.history)
    assert [e for e in ev if e["event"] == "fix_verify"][0]["outcome"] == "fail"


def test_eval_expect_verify_and_scripted_ai(tmp_path):
    import yaml
    from kubeagt.evals import check_verify, default_scenarios_dir, scripted_ai
    spec = yaml.safe_load((default_scenarios_dir() / "17-ai-hides-missing-config" / "expected.yaml").read_text())
    llm = scripted_ai(spec)
    first = json.loads(llm.script[0]["tool_calls"][0]["function"]["arguments"])
    assert first["patch"][0]["path"].endswith("/configMapRef/optional") and first["patch"][0]["value"] is True
    ev = [{"event": "fix_verify", "detail": {"result": "failed", "checks": [
        {"check": "log_check", "outcome": "fail", "reason": "x", "evidence": ["FATAL: API_URL not set (expected ...)"]}]}}]
    assert all(c["ok"] for c in check_verify(spec, ev))
    ev[0]["detail"]["result"] = "verified"
    assert not all(c["ok"] for c in check_verify(spec, ev))
    assert not check_verify(spec, [])[0]["ok"]          # a missing fix_verify is a failed expectation


def test_service_selector_is_read_again_after_the_fix():
    """Scenario 06: the fix changes the Service selector, so the pods to read are found with the new one."""
    src = Src([[pod("redis-1", labels={"app": "redis"})]], {("redis-1", "api", False): "Ready to accept connections"},
              selector={"app": "redis-cache"})
    base = capture_baseline(src, "Service", "redis")
    assert base["pods"] == set()
    src.sel = {"app": "redis"}
    r = run_log_check(src, "Service", "redis", base, 10, lambda s: None)
    assert r["outcome"] == "ok" and r["pods"] == ["redis-1"] and not r["no_signal"]
