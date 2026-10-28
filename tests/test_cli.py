import json

from helpers import bundle, node, pod_for, redis_deployment, service

from kdiag.cli import main
from kdiag.evals import check_expectation, default_scenarios_dir, get_path, list_scenarios


def test_check_offline_bundle(tmp_path, capsys):
    d = redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432, mem_req="5Mi", mem_lim="5Mi")
    p = bundle(d, service(), node(), pod_for(d, phase="Pending", ready=False, unschedulable_msg="no")).save(tmp_path / "b.zip")
    rc = main(["check", "--bundle", str(p), "--json"])
    rules = {f["rule"] for f in json.loads(capsys.readouterr().out) if f["root_cause"]}
    assert rc == 1 and rules == {"nodeselector-no-match", "probe-port-mismatch", "memory-too-small"}


def test_check_healthy_bundle_exit_zero(tmp_path, capsys):
    d = redis_deployment()
    d["status"] = {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1}
    p = bundle(d, node(), pod_for(d)).save(tmp_path / "b")
    assert main(["check", "--bundle", str(p)]) == 0
    assert "No problems found" in capsys.readouterr().out


def test_eval_refuses_without_explicit_context(capsys):
    assert main(["eval"]) == 2


def test_scenarios_are_packaged_and_complete():
    names = [s.name for s in list_scenarios(default_scenarios_dir())]
    assert names[:5] == ["00-healthy", "01-nodeselector", "02-probe-port", "03-memory-5mi", "04-redis-combined"]
    assert len(names) >= 8


def test_expectation_checks():
    obj = {"spec": {"a": [{"b": 6379}], "sel": {"app": "x"}}}
    assert get_path(obj, "spec.a[0].b") == (True, 6379)
    assert check_expectation(obj, {"path": "spec.a[0].b", "equals": 6379})[0]
    assert check_expectation(obj, {"path": "spec.nope", "absent": True})[0]
    assert check_expectation(obj, {"path": "spec.sel", "equals": {"app": "x"}})[0]
    assert not check_expectation(obj, {"path": "spec.a[0].b", "equals": 5432})[0]
