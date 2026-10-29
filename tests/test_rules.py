from helpers import (bundle, event, node, pod_for, redis_deployment, replicaset_for, service)

from kubeagt.patching import apply_json_patch
from kubeagt.rules import run_rules


def roots(findings):
    return {f.rule for f in findings if f.root_cause}


def by_rule(findings, rule):
    return next(f for f in findings if f.rule == rule)


def test_healthy_has_no_findings():
    d = redis_deployment()
    d["status"] = {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1}
    b = bundle(d, service(), node(), pod_for(d))
    assert run_rules(b) == []


def test_nodeselector_no_match_with_fix():
    d = redis_deployment(node_selector={"disktype": "ssd"})
    p = pod_for(d, phase="Pending", ready=False, unschedulable_msg="0/1 nodes are available")
    b = bundle(d, node(), p, event("Pod", p["metadata"]["name"], "FailedScheduling", "0/1 nodes are available: didn't match selector"))
    f = run_rules(b)
    assert "nodeselector-no-match" in roots(f)
    ns = by_rule(f, "nodeselector-no-match")
    assert ns.fix.patch == [{"op": "remove", "path": "/spec/template/spec/nodeSelector"}]
    # Pending pod is reported as a symptom of the selector, not a separate problem
    assert by_rule(f, "unschedulable").root_cause is False
    after = apply_json_patch(d, ns.fix.patch)
    assert "nodeSelector" not in after["spec"]["template"]["spec"]


def test_nodeselector_partial_keys_only_removes_bad_key():
    d = redis_deployment(node_selector={"kubernetes.io/os": "linux", "gpu": "a100"})
    f = by_rule(run_rules(bundle(d, node())), "nodeselector-no-match")
    assert f.fix.patch == [{"op": "remove", "path": "/spec/template/spec/nodeSelector/gpu"}]


def test_nodeselector_satisfied_is_fine():
    d = redis_deployment(node_selector={"disktype": "ssd"})
    assert "nodeselector-no-match" not in roots(run_rules(bundle(d, node(labels={"disktype": "ssd"}))))


def test_probe_port_mismatch_uses_declared_port():
    d = redis_deployment(probe_port=5432)
    f = by_rule(run_rules(bundle(d, node())), "probe-port-mismatch")
    assert f.fix.patch == [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}]
    assert f.fix.confidence == "high"


def test_probe_port_falls_back_to_well_known_port_when_no_ports_declared():
    d = redis_deployment(probe_port=5432, ports=False)
    f = by_rule(run_rules(bundle(d, node())), "probe-port-mismatch")
    assert f.fix.patch[0]["value"] == 6379 and f.fix.confidence == "medium"


def test_tiny_memory():
    d = redis_deployment(mem_req="5Mi", mem_lim="5Mi")
    f = by_rule(run_rules(bundle(d, node())), "memory-too-small")
    after = apply_json_patch(d, f.fix.patch)
    res = after["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res == {"requests": {"memory": "128Mi"}, "limits": {"memory": "512Mi"}}


def test_oom_and_crashloop_are_symptoms_of_tiny_memory():
    d = redis_deployment(mem_req="5Mi", mem_lim="5Mi")
    p = pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff"}, last_terminated={"reason": "OOMKilled", "exitCode": 137}, restarts=4)
    f = run_rules(bundle(d, node(), p))
    assert roots(f) == {"memory-too-small"}
    assert by_rule(f, "oom-killed").root_cause is False


def test_combined_redis_three_root_causes_in_priority_order():
    d = redis_deployment(node_selector={"redisnode": "true"}, probe_port=5432, mem_req="5Mi", mem_lim="5Mi")
    p = pod_for(d, phase="Pending", ready=False, unschedulable_msg="0/1 nodes are available")
    f = run_rules(bundle(d, service(), node(), p))
    fixable = [x.rule for x in f if x.root_cause and x.fix]
    assert fixable == ["nodeselector-no-match", "memory-too-small", "probe-port-mismatch"]


def test_image_pull_rolls_back_to_previous_revision():
    d = redis_deployment(image="redis:7.2-typo")
    old = replicaset_for(d, "old1", revision=1, image="redis:7.2-alpine")
    new = replicaset_for(d, "new2", revision=2)
    p = pod_for(d, phase="Pending", ready=False, waiting={"reason": "ImagePullBackOff", "message": "not found"}, rs_hash="new2")
    f = by_rule(run_rules(bundle(d, old, new, node(), p)), "image-pull-failure")
    assert f.fix.patch == [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "redis:7.2-alpine"}]


def test_image_pull_without_history_has_no_fix():
    d = redis_deployment(image="redis:nope")
    p = pod_for(d, phase="Pending", ready=False, waiting={"reason": "ErrImagePull", "message": "not found"})
    f = by_rule(run_rules(bundle(d, node(), p)), "image-pull-failure")
    assert f.fix is None


def test_crashloop_command_not_found_removes_override():
    d = redis_deployment(command=["redis-srv"])
    p = pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff"}, restarts=3,
                last_terminated={"reason": "StartError", "exitCode": 128, "message": 'exec: "redis-srv": executable file not found in $PATH'})
    f = by_rule(run_rules(bundle(d, node(), p)), "crash-loop")
    assert f.fix.patch == [{"op": "remove", "path": "/spec/template/spec/containers/0/command"}]


def test_crashloop_app_error_has_no_fix_but_shows_logs():
    d = redis_deployment()
    p = pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff"}, restarts=3, last_terminated={"reason": "Error", "exitCode": 1})
    logs = {f"{p['metadata']['name']}__redis.previous": "FATAL CONFIG FILE ERROR\nBad directive"}
    f = by_rule(run_rules(bundle(d, node(), p, logs=logs)), "crash-loop")
    assert f.fix is None and any("Bad directive" in e for e in f.evidence)


def test_service_selector_matches_no_pods():
    d = redis_deployment()
    f = by_rule(run_rules(bundle(d, service(selector={"app": "redis-cache"}), node(), pod_for(d))), "service-no-matching-pods")
    assert f.fix.patch == [{"op": "replace", "path": "/spec/selector", "value": {"app": "redis"}}]


def test_service_target_port_mismatch():
    d = redis_deployment()
    d["status"] = {"replicas": 1, "readyReplicas": 1}
    f = by_rule(run_rules(bundle(d, service(target_port=6380), node(), pod_for(d))), "service-target-port-mismatch")
    assert f.fix.patch[0]["value"] == 6379


def test_missing_configmap_detected_without_fix():
    d = redis_deployment()
    p = pod_for(d, phase="Pending", ready=False, waiting={"reason": "CreateContainerConfigError", "message": 'configmap "x" not found'})
    f = by_rule(run_rules(bundle(d, node(), p)), "container-config-error")
    assert f.fix is None and f.root_cause


def test_stale_pod_from_old_replicaset_is_ignored():
    d = redis_deployment()
    old = replicaset_for(d, "old1", revision=1)
    new = replicaset_for(d, "new2", revision=2)
    stale = pod_for(d, phase="Pending", ready=False, unschedulable_msg="no nodes", rs_hash="old1")
    fresh = pod_for(d, rs_hash="new2", suffix="zz")
    assert "unschedulable" not in {f.rule for f in run_rules(bundle(d, old, new, node(), stale, fresh))}


def test_no_nodes_visible_skips_node_rules():
    d = redis_deployment(node_selector={"disktype": "ssd"})
    assert "nodeselector-no-match" not in roots(run_rules(bundle(d)))


def test_tiny_memory_diagnosis_matches_observed_state():
    d = redis_deployment(mem_req="5Mi", mem_lim="5Mi")
    running = by_rule(run_rules(bundle(d, node(), pod_for(d))), "memory-too-small")
    assert running.severity == "warning" and "may start while idle" in running.diagnosis
    assert any("running now" in e for e in running.evidence)
    p = pod_for(d, ready=False, waiting={"reason": "CrashLoopBackOff"}, last_terminated={"reason": "OOMKilled", "exitCode": 137}, restarts=4)
    failing = by_rule(run_rules(bundle(d, node(), p)), "memory-too-small")
    assert failing.severity == "critical" and "show failures" in failing.diagnosis
