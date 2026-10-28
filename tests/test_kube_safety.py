import json
import stat
import sys
import textwrap

import pytest

from kdiag.kube import KubeTarget, KubeWriter, ReadOnlyKubectl, ToolRefused, validate_read_argv


@pytest.mark.parametrize("argv", [
    ["get", "pods"], ["get", "pods", "-o", "wide"], ["describe", "deploy", "redis"], ["logs", "redis-abc", "--previous", "--tail", "20"],
    ["get", "events", "--sort-by", ".lastTimestamp"], ["events", "--for", "pod/redis"], ["top", "pods"], ["get", "deployments.apps"],
    ["get", "pod/a", "svc/b"],
])
def test_read_allowed(argv):
    assert validate_read_argv(argv)[0] == argv[0]


@pytest.mark.parametrize("argv", [
    ["delete", "pod", "x"], ["apply", "-f", "x.yaml"], ["patch", "deploy", "x"], ["exec", "x", "--", "sh"], ["edit", "deploy/x"],
    ["scale", "deploy/x", "--replicas=0"], ["get", "secrets"], ["get", "secret/db"], ["describe", "secrets"], ["get", "pods,secrets"],
    ["get", "pod/a", "secret/b"], ["get", "pods", "--kubeconfig", "/tmp/k"], ["get", "pods", "--context=prod"], ["get", "pods", "--token=x"],
    ["get", "pods", "-o", "go-template={{.data}}"], ["get", "--raw", "/api"], ["proxy"], ["port-forward", "x", "80"], ["auth", "can-i", "*", "*"],
    ["get", "pods\nrm -rf /"], ["get", "pods", "--server", "https://evil"], ["cp", "x:/etc/passwd", "."], [],
])
def test_read_refused(argv):
    with pytest.raises(ToolRefused):
        validate_read_argv(argv)


def test_writer_refuses_unknown_kinds_and_bad_patches():
    with pytest.raises(ToolRefused):
        KubeWriter.normalize("Secret", "x", "merge", {"data": {}})
    with pytest.raises(ToolRefused):
        KubeWriter.normalize("ClusterRoleBinding", "x", "merge", {})
    with pytest.raises(ToolRefused):
        KubeWriter.normalize("Deployment", "a/b", "json", [])
    with pytest.raises(ToolRefused):
        KubeWriter.normalize("Deployment", "x", "json", {"not": "a list"})
    with pytest.raises(ToolRefused):
        KubeWriter.normalize("Deployment", "x", "apply", {})
    assert KubeWriter.normalize("Deployment", "x", "json", "[]")[0] == "deployment"


def _fake_kubectl(tmp_path, stdout_text):
    log = tmp_path / "argv.log"
    script = tmp_path / "kubectl"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
        sys.stdout.write({stdout_text!r})
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), log


def test_readonly_run_scopes_namespace_and_redacts(tmp_path):
    exe, log = _fake_kubectl(tmp_path, "DB_PASSWORD=hunter2 token: abcdefghijklmnop\nok\n")
    kc = ReadOnlyKubectl(KubeTarget("demo", context="kind-kdiag-test", kubeconfig="/tmp/kc", kubectl=exe))
    out = kc.run(["get", "pods"])
    assert "hunter2" not in out and "<redacted>" in out
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[:4] == ["--kubeconfig", "/tmp/kc", "--context", "kind-kdiag-test"]
    assert argv[-2:] == ["-n", "demo"]
    with pytest.raises(ToolRefused):
        kc.run(["delete", "pod", "x"])
    assert len(log.read_text().splitlines()) == 1  # refused command never reached kubectl


def test_writer_builds_patch_command(tmp_path):
    exe, log = _fake_kubectl(tmp_path, "deployment.apps/redis patched\n")
    w = KubeWriter(KubeTarget("demo", context="ctx", kubectl=exe))
    cp = w.patch("Deployment", "redis", "json", [{"op": "remove", "path": "/spec/template/spec/nodeSelector"}], dry_run=True)
    assert cp.returncode == 0
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[2:8] == ["patch", "deployment", "redis", "-n", "demo", "--type"] and "--dry-run=server" in argv
