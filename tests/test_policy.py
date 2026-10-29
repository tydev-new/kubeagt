"""Policy filter skeleton: the hook runs once per proposal, before the dry run, and allows everything."""
from helpers import FakeCluster, node, redis_deployment, service

from kubeagt import policy
from kubeagt.audit import AuditLog
from kubeagt.evals import _events
from kubeagt.fixer import FixSession, scripted_ask
from test_fixer import KC


def _session(tmp_path, answers, **kw):
    fc = FakeCluster([redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432)], [service()], [node()])
    path = tmp_path / "audit.jsonl"
    rep = FixSession("demo", "kind-test", fc.snapshot, fc, KC(), AuditLog(path), ask=scripted_ask(answers),
                     out=lambda s: None, verify_timeout=1, sleep=lambda s: None, **kw).run()
    return fc, rep, _events(path)


def test_registry_is_empty_and_allows_everything():
    assert policy.REGISTRY == []
    assert policy.evaluate(object(), None, policy.PolicyContext("ns", "ctx"))["allow"] is True


def test_hook_called_once_per_proposal_before_dry_run_and_is_a_no_op(tmp_path):
    fc, rep, ev = _session(tmp_path, ["y", "y"])
    names = [e["event"] for e in ev]
    pol = [e for e in ev if e["event"] == "policy_check"]
    assert len(pol) == len(rep.fixes) == 2 and all(e["allow"] and e["checks_run"] == [] for e in pol)
    # each policy_check comes before the human is asked about that proposal
    for e in pol:
        d = next(x for x in ev if x["event"] == "decision" and x["n"] == e["n"])
        assert ev.index(e) < ev.index(d)
    assert "fix_blocked" not in names and len(fc.applied) == 2 and rep.healthy


def test_hook_receives_proposal_and_live_object(tmp_path):
    seen = []

    class Spy:
        name = "spy"

        def check(self, proposal, live_object, context):
            seen.append((proposal.fix.kind, (live_object or {}).get("metadata", {}).get("name"), context.namespace))
            return policy.PolicyResult.allowed()
    _session(tmp_path, ["y", "y"], policy_checks=[Spy()])
    assert seen and all(s == ("Deployment", "redis", "demo") for s in seen)
