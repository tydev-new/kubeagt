
import pytest
from helpers import bundle, node, redis_deployment, service

from kubeagt.bundle import Bundle
from kubeagt.patching import PatchError, apply_json_patch, apply_merge_patch, spec_diff
from kubeagt.quantity import mib, parse_quantity
from kubeagt.redact import redact_obj, redact_text


def test_quantities():
    assert mib("5Mi") == 5
    assert mib("1Gi") == 1024
    assert parse_quantity("500m") == 0.5
    assert round(mib("128M"), 2) == 122.07
    with pytest.raises(ValueError):
        parse_quantity("lots")


def test_redact_text_and_objects():
    t = "password=s3cret Authorization: Bearer abcdefghijklmnopqrstuvwxyz xai-ABCDEFGHIJKLMNOPQRSTUV"
    r = redact_text(t)
    assert "s3cret" not in r and "abcdefghijklmnop" not in r and "xai-ABCDEF" not in r
    obj = {"kind": "Secret", "data": {"pw": "aGVsbG8="}}
    assert redact_obj(obj)["data"] == {"pw": "<redacted>"}
    d = redis_deployment(env=[{"name": "REDIS_PASSWORD", "value": "hunter2"}, {"name": "MODE", "value": "cache"}])
    env = redact_obj(d)["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env[0]["value"] == "<redacted>" and env[1]["value"] == "cache"


def test_json_patch_ops():
    doc = {"a": {"b": [1, 2], "c": "x"}}
    assert apply_json_patch(doc, [{"op": "replace", "path": "/a/c", "value": "y"}])["a"]["c"] == "y"
    assert apply_json_patch(doc, [{"op": "remove", "path": "/a/b/0"}])["a"]["b"] == [2]
    assert apply_json_patch(doc, [{"op": "add", "path": "/a/d", "value": 1}])["a"]["d"] == 1
    with pytest.raises(PatchError):
        apply_json_patch(doc, [{"op": "remove", "path": "/a/zzz"}])
    assert doc == {"a": {"b": [1, 2], "c": "x"}}  # input untouched
    assert apply_merge_patch({"a": 1, "b": 2}, {"b": None, "c": 3}) == {"a": 1, "c": 3}


def test_spec_diff_shows_change():
    d = redis_deployment(probe_port=5432)
    after = apply_json_patch(d, [{"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/tcpSocket/port", "value": 6379}])
    diff = spec_diff(d, after, "Deployment/redis")
    assert "-            port: 5432" in diff and "+            port: 6379" in diff


@pytest.mark.parametrize("as_zip", [False, True])
def test_bundle_roundtrip(tmp_path, as_zip):
    b = bundle(redis_deployment(), service(), node(), logs={"redis-x__redis": "hello"})
    out = b.save(tmp_path / ("b.zip" if as_zip else "bdir"))
    b2 = Bundle.load(out)
    assert b2.namespace == "demo"
    assert b2.get("deployments", "redis")["spec"]["replicas"] == 1
    assert b2.pod_logs("redis-x", "redis") == "hello"
