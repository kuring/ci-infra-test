"""Offline tests for kind isolation, credential handling and shared-controller reuse."""

import argparse
import base64
import json
import subprocess
import sys
from unittest.mock import Mock

import drain_test_kind as module
import pytest
import yaml


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("OSDC_KIND_DIRECT_NETWORK", raising=False)
    monkeypatch.delenv("OSDC_KIND_ARC_PUBLIC_IMAGES", raising=False)
    monkeypatch.setenv("GH_TOKEN", "fixture")
    token = tmp_path / "github-token"
    token.write_text("github_pat_fixture")
    token.chmod(0o600)
    return tmp_path


def args(root):
    return argparse.Namespace(
        root_dir=root,
        upstream_dir=module.UPSTREAM,
        clusters_yaml=root / "clusters.yaml",
        run_timeout=module.RUN_TIMEOUT,
        cleanup_timeout=module.CLEANUP_TIMEOUT,
        restore_timeout=module.RESTORE_TIMEOUT,
        start_timeout=600,
        finish_timeout=600,
        drain_timeout=180,
        observe_seconds=60,
    )


def test_token_read_rejects_world_access_and_symlinks(local):
    assert module.read_token(local) == "github_pat_fixture"
    file = local / "github-token"
    file.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        module.read_token(local)
    file.chmod(0o600)
    file.write_text("not-a-pat")
    with pytest.raises(ValueError, match="fine-grained"):
        module.read_token(local)
    file.unlink()
    file.symlink_to(local / "missing")
    with pytest.raises(ValueError, match="owner-only"):
        module.read_token(local)


def test_guard_and_kube_always_use_owned_context(local, monkeypatch):
    cmd = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(module, "command", cmd)
    module.guard()
    assert cmd.call_args.args[0] == ["just", "--justfile", str(module.LOCAL / "justfile"), "kind-drain-guard"]
    assert module.kube("get", "nodes") == "ok"
    assert cmd.call_args.args[0][:5] == [
        "kubectl",
        "--kubeconfig",
        str(local / "kubeconfig"),
        "--context",
        module.CONTEXT,
    ]


def test_render_keeps_real_recipes_and_changes_only_connection(local):
    module.render()
    justfile = (local / "justfile").read_text()
    assert f'import "{module.UPSTREAM}/justfile"' in justfile
    assert "kubeconfig cluster:" in justfile
    assert "drain-runners cluster:" not in justfile
    assert "resume-runners cluster:" not in justfile
    assert "Isolated kubeconfig required" in justfile
    assert (local / "mise.toml").read_text() == (module.UPSTREAM / "mise.toml").read_text()
    assert set(yaml.safe_load((local / "clusters.yaml").read_text())["clusters"]) == {module.LOCAL_CLUSTER}
    definition = yaml.safe_load((local / "modules/arc-runners/defs/local.yaml").read_text())
    assert definition == {"runner": {"name": module.LABEL, "max_runners": 3}}
    values = yaml.safe_load((local / "runner-values.yaml").read_text())
    assert values["githubConfigSecret"] == "osdc-kind-github"
    assert values["runnerGroup"] == "default"
    assert "github_pat_" not in (local / "runner-values.yaml").read_text()
    assert values["containerMode"]["type"] == "kubernetes-novolume"
    env = {e["name"]: e["value"] for e in values["listenerTemplate"]["spec"]["containers"][0]["env"]}
    assert env["CAPACITY_AWARE_ENABLED"] == "true"
    assert env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "3"


def test_install_sends_secret_via_stdin_and_checks_version(local, monkeypatch):
    guard = Mock()
    monkeypatch.setattr(module, "guard", guard)
    api = Mock(return_value={"permissions": {"admin": True}})
    monkeypatch.setattr(module, "api", api)
    kube = Mock(return_value="")
    monkeypatch.setattr(module, "kube", kube)
    chart = local / "charts/gha-runner-scale-set"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(f"version: {module.VERSION}\n")
    cmd = Mock()
    monkeypatch.setattr(module, "command", cmd)
    module.install()
    guard.assert_called_once()
    api.assert_called_once_with("", repository=module.REPOSITORY)
    secret = json.loads(kube.call_args_list[1].kwargs["input"])
    assert base64.b64decode(secret["data"]["github_token"]).decode() == "github_pat_fixture"
    assert all("github_pat_" not in str(c.args) for c in kube.call_args_list)
    assert cmd.call_args.args[0][:3] == ["helm", "upgrade", "--install"]
    assert module.CONTEXT in cmd.call_args.args[0]
    (chart / "Chart.yaml").write_text("version: wrong\n")
    with pytest.raises(ValueError, match="chart version"):
        module.install()


def test_render_preserves_optional_machine_proxy_without_changing_runner_identity(local):
    proxy = {
        "http": {"url": "http://host.docker.internal:13660"},
        "https": {"url": "http://host.docker.internal:13660"},
        "noProxy": ["kubernetes.default.svc", ".svc", ".cluster.local", "10.96.0.0/12"],
    }
    (local / "proxy.yaml").write_text(yaml.safe_dump({"proxy": proxy}))
    module.render()
    values = yaml.safe_load((local / "runner-values.yaml").read_text())
    assert values["proxy"] == proxy
    assert values["githubConfigSecret"] == "osdc-kind-github"
    assert values["maxRunners"] == 3
    assert values["runnerScaleSetName"] == module.LABEL


def test_install_refuses_wrong_github_access(local, monkeypatch):
    monkeypatch.setattr(module, "guard", Mock())
    monkeypatch.setattr(module, "api", Mock(return_value={}))
    kube = Mock()
    monkeypatch.setattr(module, "kube", kube)
    with pytest.raises(ValueError, match="Administration"):
        module.install()
    kube.assert_not_called()


def test_install_fetches_missing_chart(local, monkeypatch):
    monkeypatch.setattr(module, "guard", Mock())
    monkeypatch.setattr(module, "api", Mock(return_value={"permissions": {"admin": True}}))
    monkeypatch.setattr(module, "kube", Mock(return_value=""))
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        chart = local / "charts/gha-runner-scale-set"
        chart.mkdir(parents=True, exist_ok=True)
        (chart / "Chart.yaml").write_text(f"version: {module.VERSION}\n")

    monkeypatch.setattr(module, "command", command)
    module.install()
    assert commands[0][:2] == ["helm", "pull"]
    assert module.VERSION in commands[0]


def test_kind_controller_reuses_shared_logic_with_local_identity(local, monkeypatch):
    test = module.KindDrainTest(args(local), "abc")
    assert test.cluster == module.LOCAL_CLUSTER
    assert test.env["KUBECONFIG"] == str(local / "kubeconfig")
    assert str(local) in test.env["MISE_TRUSTED_CONFIG_PATHS"].split(module.os.pathsep)
    api = Mock(return_value={"jobs": [{"runner_group_name": "Default"}, {"runner_group_name": "other"}, {}]})
    monkeypatch.setattr(module, "api", api)
    assert test.jobs() == [{"runner_group_name": "default"}, {"runner_group_name": "other"}, {}]
    assert api.call_args.kwargs["repository"] == module.REPOSITORY
    kube = Mock(return_value="ok")
    monkeypatch.setattr(module, "kube", kube)
    assert test.kube("get", "nodes") == "ok"
    guard = Mock()
    monkeypatch.setattr(module, "guard", guard)
    recipe = Mock()
    monkeypatch.setattr(test, "recipe", recipe)
    monkeypatch.setattr(
        module, "command", Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({module.LABEL: 3}), ""))
    )
    test.prepare()
    guard.assert_called_once()
    recipe.assert_called_once_with("kubeconfig")
    test.check_probe([{"metadata": {"name": module.LABEL}, "spec": {"runnerScaleSetName": module.LABEL}}])
    workflow = yaml.safe_load(test.workflow())
    for job in workflow["jobs"].values():
        assert job["runs-on"] == module.LABEL
        assert "head.repo.full_name" in job["if"]


def test_kind_workflow_preserves_shared_probes_and_pull_request_trigger():
    expected = yaml.safe_load(module.generate_workflow("abc123"))
    for job in expected["jobs"].values():
        job["runs-on"] = module.LABEL

    content = module.generate_kind_workflow("abc123")
    assert yaml.safe_load(content) == expected
    # YAML 1.1 treats `on` as a boolean; ensure it remains an Actions event key.
    assert "\non:\n" in content


@pytest.mark.parametrize("action", ["render", "install", "test", "restore", "probe", "dry-run"])
def test_cli_routes_and_dry_run_needs_no_credentials(local, monkeypatch, capsys, action):
    argv = ["kind", "test", "--dry-run"] if action == "dry-run" else ["kind", action]
    monkeypatch.setattr(sys, "argv", argv)
    render = Mock(return_value={})
    install = Mock()
    run = Mock()
    monkeypatch.setattr(module, "render", render)
    monkeypatch.setattr(module, "install", install)
    monkeypatch.setattr(module, "run_controller", run)
    module.main()
    if action == "dry-run":
        assert module.LABEL in capsys.readouterr().out
        render.assert_not_called()
        run.assert_not_called()
    elif action == "render":
        render.assert_called_once()
    elif action == "install":
        install.assert_called_once()
    else:
        assert run.call_args.kwargs["restore_only"] == (action == "restore")


def test_cli_render_prints_generated_files(local, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kind", "render"])
    module.main()
    paths = [
        local / "modules/arc-runners/defs/local.yaml",
        local / "clusters.yaml",
        local / "runner-values.yaml",
        local / "justfile",
        local / "mise.toml",
    ]
    output = capsys.readouterr().out
    assert output == "".join(f"# {path}\n{path.read_text()}\n" for path in paths)
    assert "github_pat_fixture" not in output


def test_probe_checks_real_limits_and_capacity_without_github(local, monkeypatch):
    values = args(local)
    values.action = "probe"
    test = module.KindDrainTest(values, "abc")
    test.desired = {module.LABEL: 3}
    monkeypatch.setattr(test, "prepare", Mock())
    monkeypatch.setattr(test, "scale_sets", lambda: [{"metadata": {"name": module.LABEL}, "spec": {"maxRunners": 3}}])
    monkeypatch.setattr(test, "drained", Mock(return_value=True))
    caps = Mock(return_value={module.LABEL: 1})
    monkeypatch.setattr(test, "capacities", caps)
    recipe = Mock()
    monkeypatch.setattr(test, "recipe", recipe)
    resume = Mock()
    monkeypatch.setattr(test, "resume", resume)
    monkeypatch.setattr(module, "wait_for", lambda description, check, timeout: check())
    test.run()
    recipe.assert_called_once_with("drain-runners")
    resume.assert_called_once()
    caps.side_effect = [{module.LABEL: 1}, {module.LABEL: 0}]
    with pytest.raises(ValueError, match="warm reservation"):
        test.run()


def test_kind_full_test_uses_shared_run(local, monkeypatch):
    parent = Mock()
    monkeypatch.setattr(module.DrainTest, "run", parent)
    module.KindDrainTest(args(local), "abc").run()
    parent.assert_called_once()


def test_upstream_import_is_the_configured_checkout():
    import drain_test_run

    assert module.UPSTREAM == drain_test_run.UPSTREAM
    assert module.generate_workflow is drain_test_run.generate_workflow
    assert module.run_controller is drain_test_run.run_controller


def test_real_upstream_max_map_uses_local_definitions(local):
    module.render()
    test = module.KindDrainTest(args(local), "abc")
    # Run the actual upstream script with the existing test interpreter. No uv
    # resolution, cluster, GitHub calls or copies of upstream implementation.
    result = subprocess.run(
        [sys.executable, str(module.UPSTREAM / "modules/arc-runners/scripts/python/runner_max_map.py"), test.cluster],
        env=test.env,
        cwd=local,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {module.LABEL: 3}


def test_generated_recipes_resolve_consumer_root_and_upstream(local):
    module.render()
    result = subprocess.run(
        ["just", "--justfile", str(local / "justfile"), "--evaluate"],
        check=True,
        capture_output=True,
        text=True,
    )
    variables = dict(line.split(":=", 1) for line in result.stdout.splitlines())
    variables = {key.strip(): json.loads(value.strip()) for key, value in variables.items()}
    assert variables["ROOT"] == str(local)
    assert variables["UPSTREAM"] == str(module.UPSTREAM)
    assert variables["CLUSTERS_YAML"] == str(local / "clusters.yaml")


@pytest.mark.parametrize("field", ["start-timeout", "finish-timeout", "drain-timeout", "observe-seconds"])
def test_cli_rejects_nonpositive_timeouts(local, monkeypatch, field):
    monkeypatch.setattr(sys, "argv", ["kind", "test", f"--{field}", "0"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("group", ["Default", "default", "other", None])
def test_local_completed_keeps_upstream_group_validation(local, monkeypatch, group):
    test = module.KindDrainTest(args(local), "abc")
    job = {
        "id": 1,
        "name": "drain-inflight",
        "status": "completed",
        "conclusion": "success",
        "runner_group_name": group,
    }
    monkeypatch.setattr(module, "api", Mock(return_value={"jobs": [job]}))
    if group in {"Default", "default"}:
        assert test.completed("inflight", 1)
    else:
        with pytest.raises(RuntimeError, match="did not succeed"):
            test.completed("inflight", 1)


def test_direct_network_ignores_machine_proxy_and_preserves_capacity_settings(local, monkeypatch):
    (local / "proxy.yaml").write_text("proxy: {https: {url: 'http://unavailable:13660'}}\n")
    monkeypatch.setenv("OSDC_KIND_DIRECT_NETWORK", "true")
    monkeypatch.setenv("OSDC_KIND_ARC_PUBLIC_IMAGES", "true")
    module.render()
    values = yaml.safe_load((local / "runner-values.yaml").read_text())
    assert "proxy" not in values
    assert values["template"]["spec"]["containers"][0]["image"] == "ghcr.io/actions/actions-runner:2.338.0"
    assert values["maxRunners"] == 3
    env = {item["name"]: item["value"] for item in values["listenerTemplate"]["spec"]["containers"][0]["env"]}
    assert env["CAPACITY_AWARE_ENABLED"] == "true"
    assert env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "3"


def test_default_runner_image_remains_internal(local, monkeypatch):
    monkeypatch.delenv("OSDC_KIND_ARC_PUBLIC_IMAGES", raising=False)
    assert module.runner_values()["template"]["spec"]["containers"][0]["image"] == (
        "mirror.aone.alibaba-inc.com/mirrors/ghcr.io/actions/actions-runner:2.338.0"
    )
