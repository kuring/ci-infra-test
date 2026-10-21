"""Offline lifecycle checks; never contact a cluster or GitHub."""

import argparse
import contextlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import kind_slow as operations
import pytest
import yaml
from kind_slow_config import CONTEXT, LABEL, NODES, OWNER, POOL, REPO, WORKERS


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "STATE", tmp_path)
    monkeypatch.setattr(operations, "KUBE", tmp_path / "kubeconfig")
    monkeypatch.setattr(operations, "guard", Mock())
    return tmp_path


def ready_nodes():
    return [
        {
            "metadata": {"name": name, "labels": {OWNER: "true", "karpenter.sh/nodepool": POOL}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for name in NODES
    ]


def test_command_keeps_payload_out_of_failure_messages(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(operations.subprocess, "run", run)
    assert operations.command(["kubectl", "apply"], input="private-payload") == "ok"
    assert run.call_args.kwargs["input"] == "private-payload"
    run.return_value = SimpleNamespace(returncode=1, stdout="private-payload", stderr="forbidden")
    with pytest.raises(RuntimeError, match="forbidden") as exc:
        operations.command(["kubectl", "apply"], input="private-payload")
    assert "private-payload" not in str(exc.value)


def test_kube_and_guard_always_use_owned_kubeconfig(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "KUBE", tmp_path / "kubeconfig")
    cmd = Mock(
        side_effect=["output", json.dumps({"items": ready_nodes()}), CONTEXT, json.dumps({"items": ready_nodes()})]
    )
    monkeypatch.setattr(operations, "command", cmd)
    assert operations.kube("get", "nodes") == "output"
    assert operations.get("nodes")["items"] == ready_nodes()
    operations.guard()
    for call in cmd.call_args_list:
        assert call.args[0][:3] == ["kubectl", "--kubeconfig", str(tmp_path / "kubeconfig")]


def test_wait_and_state_permissions(local_state, monkeypatch):
    operations.write_json(local_state / "record.json", {"ok": True})
    assert json.loads((local_state / "record.json").read_text()) == {"ok": True}
    assert (local_state / "record.json").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(operations.time, "sleep", Mock())
    check = Mock(side_effect=[False, "done"])
    assert operations.wait_for("done", check) == "done"
    monkeypatch.setattr(operations.time, "monotonic", Mock(side_effect=[0, 2]))
    with pytest.raises(TimeoutError, match="Timed out"):
        operations.wait_for("timeout", lambda: False, timeout=1)


def test_render_uses_upstream_sources_and_dry_run_baseline(local_state, monkeypatch):
    monkeypatch.setattr(operations, "command", Mock(return_value="kind: ConfigMap\ndata:\n  source: real\n"))
    operations.render()
    assert yaml.safe_load((local_state / "kind.yaml").read_text())["nodes"][0]["role"] == "control-plane"
    assert yaml.safe_load((local_state / "compactor.yaml").read_text())["data"] == {"source": "real"}
    assert len(list(yaml.safe_load_all((local_state / "nodepools.yaml").read_text()))) == 2


@pytest.mark.parametrize(("persistent", "current"), [(False, 128), (True, 128), (False, 1024), (True, 1024)])
def test_host_limits_are_explicit_preserve_higher_values_and_capture_original(
    local_state, monkeypatch, persistent, current
):
    monkeypatch.setattr(operations.sys, "platform", "linux")
    monkeypatch.setattr(operations.os, "geteuid", lambda: 0)
    monkeypatch.setattr(operations, "SYSCTL_CONFIG", local_state / "limits.conf")
    cmd = Mock(side_effect=[str(current), "updated", str(current), "updated"])
    monkeypatch.setattr(operations, "command", cmd)
    operations.host_limits(persistent=persistent)
    original = (local_state / "host-limits-before.json").read_text()
    operations.host_limits(persistent=persistent)
    assert (local_state / "host-limits-before.json").read_text() == original
    assert f"fs.inotify.max_user_instances={max(current, 512)}" in cmd.call_args.args[0]
    assert operations.SYSCTL_CONFIG.exists() == persistent
    assert json.loads(original)["fs.inotify.max_user_instances"] == current


def test_host_limits_refuse_nonroot_and_existing_unrelated_config(local_state, monkeypatch):
    monkeypatch.setattr(operations.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="root on Linux"):
        operations.host_limits()
    monkeypatch.setattr(operations.sys, "platform", "linux")
    monkeypatch.setattr(operations.os, "geteuid", lambda: 1)
    with pytest.raises(ValueError, match="root on Linux"):
        operations.host_limits()
    monkeypatch.setattr(operations.os, "geteuid", lambda: 0)
    path = local_state / "limits.conf"
    path.write_text("user-owned configuration")
    monkeypatch.setattr(operations, "SYSCTL_CONFIG", path)
    monkeypatch.setattr(operations, "command", Mock(return_value="128"))
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        operations.host_limits(persistent=True)
    assert path.read_text() == "user-owned configuration"


@pytest.mark.parametrize("existing", [False, True])
def test_create_does_not_touch_existing_drain_cluster(local_state, monkeypatch, existing):
    monkeypatch.setattr(operations, "render", Mock())
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:13660")
    calls = []

    def cmd(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["kind", "get", "clusters"]:
            return "osdc-drain-local\n" + (operations.CLUSTER if existing else "")
        operations.KUBE.write_text("owned kubeconfig")
        return "ready"

    monkeypatch.setattr(operations, "command", cmd)
    operations.create()
    assert not any("osdc-drain-local" in arg for args, _ in calls for arg in args)
    action = "export" if existing else "create"
    action_args, options = next((args, kw) for args, kw in calls if args[1] == action)
    assert operations.CLUSTER in action_args
    if not existing:
        assert options["env"]["HTTP_PROXY"] == "http://host.docker.internal:13660"
        assert ".alibaba-inc.com" in options["env"]["NO_PROXY"]
    assert operations.KUBE.stat().st_mode & 0o777 == 0o600


def test_deploy_builds_native_images_and_loads_kind(local_state, monkeypatch):
    monkeypatch.setattr(operations, "render", Mock())
    monkeypatch.setattr(operations, "preload", Mock())
    monkeypatch.setattr(operations, "get", Mock(return_value={"metadata": {"labels": {"kubernetes.io/arch": "arm64"}}}))

    def build_command(args):
        if args[:2] == ["docker", "build"] and args[-1].startswith(str(local_state)):
            files = list(Path(args[-1]).iterdir())
            assert any(file.name == "compactor.py" for file in files)
            assert all(file.stat().st_mode & 0o777 == 0o644 for file in files)
        return "ok"

    cmd = Mock(side_effect=build_command)
    kube = Mock(return_value="ok")
    monkeypatch.setattr(operations, "command", cmd)
    monkeypatch.setattr(operations, "kube", kube)
    monkeypatch.setattr(operations, "apply", Mock())
    operations.deploy()
    builds = [call.args[0] for call in cmd.call_args_list if call.args[0][:2] == ["docker", "build"]]
    assert len(builds) == 2
    assert all("linux/arm64" in args for args in builds)
    assert any(call.args[0][:3] == ["kind", "load", "docker-image"] for call in cmd.call_args_list)
    assert not list(local_state.glob("build-*"))


@pytest.mark.parametrize("scenario", ["import", "cached", "download", "architecture", "foreign", "empty"])
def test_preload_checks_archives_before_loading_and_keeps_uv_version(local_state, monkeypatch, scenario):
    monkeypatch.setattr(operations, "get", Mock(return_value={"metadata": {"labels": {"kubernetes.io/arch": "amd64"}}}))
    images = [
        ("python-image.tar", "python:3.12.9-slim"),
        ("uv-image.tar", "ghcr.io/astral-sh/uv:0.10.12"),
        ("janitor-base-image.tar", "public.ecr.aws/docker/library/python:3.12-alpine"),
        ("alpine-image.tar", operations.TEST_IMAGE),
    ]

    def archive(path, tag):
        metadata = [{"Config": "config.json", "RepoTags": ["foreign" if scenario == "foreign" else tag]}]
        if scenario == "empty":
            metadata = []
        with tarfile.open(path, "w") as tar:
            for name, value in [
                ("manifest.json", metadata),
                ("config.json", {"architecture": "arm64" if scenario == "architecture" else "amd64"}),
            ]:
                data = json.dumps(value).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    if scenario != "download":
        for filename, image in images:
            archive(local_state / filename, image)
    calls = []

    def cmd(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            if scenario == "cached":
                return "amd64"
            raise RuntimeError("not cached")
        if args[:2] == ["crane", "pull"]:
            archive(Path(args[3]), args[2])
        return "ok"

    monkeypatch.setattr(operations, "command", cmd)
    if scenario in {"architecture", "foreign", "empty"}:
        with pytest.raises(ValueError, match="Cached archive"):
            operations.preload()
        assert not any(args[:2] == ["docker", "load"] for args in calls)
    else:
        operations.preload()
        assert sum(args[:2] == ["docker", "load"] for args in calls) == (0 if scenario == "cached" else 4)
        if scenario == "import":
            tag = next(args for args in calls if args[:2] == ["docker", "tag"])
            assert tag[2:] == [
                "ghcr.io/astral-sh/uv:0.10.12",
                "mirror.aone.alibaba-inc.com/mirrors/ghcr.io/astral-sh/uv:0.10.12",
            ]


@pytest.mark.parametrize("cached", [False, True])
def test_public_uv_cache_is_explicit_native_and_same_version(local_state, monkeypatch, cached):
    monkeypatch.setattr(operations, "get", Mock(return_value={"metadata": {"labels": {"kubernetes.io/arch": "amd64"}}}))
    preload = Mock()
    monkeypatch.setattr(operations, "preload", preload)
    if cached:
        (local_state / "uv-image.tar").write_text("cached archive")

    def download(args):
        assert args[2] == "ghcr.io/astral-sh/uv:0.10.12"
        assert args[-1] == "linux/amd64"
        Path(args[3]).write_text("downloaded archive")

    cmd = Mock(side_effect=download)
    monkeypatch.setattr(operations, "command", cmd)
    operations.cache_uv_public()
    assert cmd.call_count == (0 if cached else 1)
    assert (local_state / "uv-image.tar").exists()
    preload.assert_called_once()


def test_token_permissions_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "LOCAL", tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="permissions 600"):
        operations.gh_env()
    directory = tmp_path / ".state"
    directory.mkdir()
    file = directory / "github-token"
    file.write_text("fixture-token")
    file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions 600"):
        operations.gh_env()
    file.chmod(0o600)
    assert operations.gh_env()["GH_TOKEN"] == file.read_text()
    file.write_text("")
    with pytest.raises(ValueError, match="Empty"):
        operations.gh_env()
    monkeypatch.setenv("GH_TOKEN", "environment-token")
    assert operations.gh_env()["GH_TOKEN"] == os.environ["GH_TOKEN"]


def test_api_uses_stdin_without_global_gh_auth_changes(monkeypatch):
    monkeypatch.setattr(operations, "gh_env", Mock(return_value={"GH_TOKEN": "fixture-token"}))
    cmd = Mock(side_effect=['{"ok":true}', ""])
    monkeypatch.setattr(operations, "command", cmd)
    assert operations.api("path", {"value": "fixture"}, method="POST") == {"ok": True}
    assert cmd.call_args.args[0] == ["gh", "api", "--method", "POST", f"repos/{REPO}/path", "--input", "-"]
    assert operations.api("path", method="DELETE") is None


def test_repository_endpoint_has_no_trailing_slash(monkeypatch):
    monkeypatch.setattr(operations, "gh_env", Mock(return_value={}))
    cmd = Mock(return_value='{"permissions":{"admin":true}}')
    monkeypatch.setattr(operations, "command", cmd)
    assert operations.api("")["permissions"]["admin"]
    assert cmd.call_args.args[0][-1] == f"repos/{REPO}"


def test_install_arc_rejects_non_admin(local_state, monkeypatch):
    monkeypatch.setattr(operations, "api", Mock(return_value={"permissions": {"admin": False}}))
    with pytest.raises(ValueError, match="administer"):
        operations.install_arc()


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("public", [True, False])
def test_install_arc_reuses_pinned_chart_and_secret_never_rendered(local_state, monkeypatch, cached, public):
    monkeypatch.setenv("OSDC_KIND_ARC_PUBLIC_IMAGES", "true" if public else "false")
    profile = local_state / "profile"
    (profile / "helm").mkdir(parents=True)
    template = yaml.safe_load((operations.LOCAL / "helm/controller.yaml").read_text())
    (profile / "helm/controller.yaml").write_text(yaml.safe_dump(template))
    monkeypatch.setattr(operations, "LOCAL", profile)
    for chart_name in ("gha-runner-scale-set-controller", "gha-runner-scale-set"):
        chart = profile / ".state/charts" / chart_name
        chart.mkdir(parents=True)
        if cached:
            (chart / "Chart.yaml").write_text(yaml.safe_dump({"version": template["image"]["tag"]}))
    monkeypatch.setattr(operations, "api", Mock(return_value={"permissions": {"admin": True}}))
    monkeypatch.setattr(operations, "gh_env", Mock(return_value={"GH_TOKEN": "fixture-token"}))
    monkeypatch.setattr(operations, "apply", Mock())
    monkeypatch.setattr(operations, "kube", Mock())
    monkeypatch.setattr(operations, "get", Mock(return_value={"items": [{}]}))

    def command(args, **kwargs):
        if args[:2] == ["helm", "pull"]:
            chart_name = args[2].rsplit("/", 1)[-1]
            (profile / ".state/charts" / chart_name / "Chart.yaml").write_text(
                yaml.safe_dump({"version": template["image"]["tag"]})
            )
        return "ok"

    cmd = Mock(side_effect=command)
    monkeypatch.setattr(operations, "command", cmd)
    operations.install_arc()
    assert sum(call.args[0][:3] == ["helm", "upgrade", "--install"] for call in cmd.call_args_list) == 2
    assert "fixture-token" not in (local_state / "runner-values.yaml").read_text()
    assert yaml.safe_load((local_state / "runner-values.yaml").read_text())["runnerScaleSetName"] == LABEL
    image = yaml.safe_load((local_state / "controller-values.yaml").read_text())["image"]
    assert image["tag"] == template["image"]["tag"]
    assert image["repository"] == (
        "ghcr.io/jeanschmidt/gha-runner-scale-set-controller" if public else template["image"]["repository"]
    )
    runner = yaml.safe_load((local_state / "runner-values.yaml").read_text())["template"]["spec"]["containers"][0]
    assert runner["image"] == ("ghcr.io/actions/actions-runner:2.338.0" if public else operations.RUNNER_IMAGE)


def test_restore_returns_to_safe_local_baseline(local_state, monkeypatch):
    kube = Mock(return_value="ok")
    monkeypatch.setattr(operations, "kube", kube)
    operations.restore()
    env_calls = [call.args for call in kube.call_args_list if call.args[:2] == ("set", "env")]
    assert "COMPACTOR_DRY_RUN=true" in env_calls[0]
    assert "IMAGE_CACHE_LIMIT_GI=9999" in env_calls[1]


@pytest.mark.parametrize("failure", [False, True])
def test_janitor_restore_stops_gc_before_reimport_and_restores_selector(local_state, monkeypatch, failure):
    kube = Mock(return_value="ok")
    monkeypatch.setattr(operations, "kube", kube)
    monkeypatch.setattr(operations, "get", Mock(return_value={"items": []}))
    cmd = Mock(side_effect=RuntimeError("import failed") if failure else None)
    monkeypatch.setattr(operations, "command", cmd)
    if failure:
        with pytest.raises(RuntimeError, match="import failed"):
            operations.restore(reload_janitor=True)
    else:
        operations.restore(reload_janitor=True)
    patches = [json.loads(call.args[-1]) for call in kube.call_args_list if call.args[0] == "patch"]
    assert [p["spec"]["template"]["spec"]["nodeSelector"] for p in patches] == [{OWNER: "false"}, {OWNER: "true"}]
    assert cmd.call_args.args[0] == [
        "kind",
        "load",
        "docker-image",
        "--name",
        operations.CLUSTER,
        operations.JANITOR_IMAGE,
        operations.TEST_IMAGE,
    ]


@pytest.mark.parametrize(
    ("component", "output", "rc"),
    [
        ("compactor", "5 passed", 0),
        ("janitor", "5 passed", 0),
        ("janitor", "5 skipped", 0),
        ("compactor", "failure", 1),
    ],
)
def test_live_suites_fail_closed_and_always_restore(local_state, monkeypatch, component, output, rc):
    monkeypatch.setattr(operations, "paused_arc", contextlib.nullcontext)
    run = Mock(return_value=SimpleNamespace(returncode=rc, stdout=output, stderr=""))
    restore = Mock()
    monkeypatch.setattr(operations.subprocess, "run", run)
    monkeypatch.setattr(operations, "restore", restore)
    if rc or "skipped" in output:
        with pytest.raises(RuntimeError, match="did not pass"):
            operations.pytest_suite(component)
    else:
        operations.pytest_suite(component)
    assert run.call_args.kwargs["env"]["KUBECONFIG"] == str(operations.KUBE)
    restore.assert_called_once()
    assert (local_state / f"{component}-e2e.log").read_text() == output


@pytest.mark.parametrize("scenario", ["absent", "success", "test-failure", "drain-failure"])
def test_compactor_pauses_only_owned_arc_and_restores_on_failure(local_state, monkeypatch, scenario):
    scales = [
        {"metadata": {"name": "other-profile"}, "spec": {"maxRunners": 99}},
        {"metadata": {"name": LABEL}, "spec": {"minRunners": 0, "maxRunners": 3}},
    ]
    monkeypatch.setattr(operations, "get", Mock(return_value={"items": scales[:1] if scenario == "absent" else scales}))
    kube = Mock()
    monkeypatch.setattr(operations, "kube", kube)
    monkeypatch.setattr(
        operations, "wait_for", Mock(side_effect=TimeoutError("drain") if scenario == "drain-failure" else None)
    )
    if scenario in {"test-failure", "drain-failure"}:
        with pytest.raises((TimeoutError, ValueError)), operations.paused_arc():
            raise ValueError("test failed")
    else:
        with operations.paused_arc():
            pass
    assert kube.call_count == (0 if scenario == "absent" else 2)
    if scenario != "absent":
        assert all(call.args[2] == LABEL for call in kube.call_args_list)
        assert json.loads(kube.call_args_list[0].args[-1])["spec"] == {"minRunners": 0, "maxRunners": 0}
        assert json.loads(kube.call_args_list[-1].args[-1])["spec"] == {"minRunners": 0, "maxRunners": 3}


@pytest.mark.parametrize("has_github_directory", [True, False])
def test_prepare_pr_replaces_only_temporary_branch_workflows(local_state, monkeypatch, has_github_directory):
    calls = []

    def api(path, payload=None, **kwargs):
        calls.append((path, payload))
        if path.startswith("git/ref/"):
            return {"object": {"sha": "head"}}
        if path == "git/commits/head":
            return {"tree": {"sha": "tree"}}
        if path == "git/trees/tree":
            return {"tree": [{"path": ".github", "sha": "existing-github"}] if has_github_directory else []}
        if path == "pulls":
            return {"number": 1, "html_url": "https://github.com/fixture/pr/1"}
        return {"sha": "new-sha"}

    monkeypatch.setattr(operations, "api", api)
    record = {"repo": REPO, "run_key": "a" * 32}
    assert operations.prepare_pr("a" * 32, "workflow", "main", record) == 1
    assert record["branch"].startswith("osdc-kind-load/")
    roots = [payload for path, payload in calls if path == "git/trees" and payload.get("base_tree") == "tree"]
    assert roots[0]["tree"][0]["path"] == ".github"
    assert all("main" not in (payload or {}).get("ref", "") for path, payload in calls if path == "git/refs")


def test_runner_evidence_survives_pod_deletion(local_state, monkeypatch):
    pod = {"metadata": {"name": "resident"}, "spec": {"nodeName": WORKERS[0], "containers": [{"name": "runner"}]}}
    events = [
        {
            "involvedObject": {"kind": "Pod", "name": f"{LABEL}-finished"},
            "reason": "Scheduled",
            "message": f"Successfully assigned runner to {WORKERS[1]}",
        }
    ]
    monkeypatch.setattr(operations, "get", Mock(side_effect=[{"items": [pod]}, {"items": events}]))
    assert operations.local_runner_names() == {"resident", f"{LABEL}-finished"}


@pytest.mark.parametrize("failure", [None, "read", "cancel", "pr", "branch", "404"])
def test_cleanup_attempts_all_owned_resources_even_after_error(local_state, monkeypatch, failure):
    calls = []
    record = {"repo": REPO, "run_key": "a" * 32, "run_ids": [123], "pr": 1, "branch": "osdc-kind-load/fixture"}

    def api(path, payload=None, **kwargs):
        calls.append(path)
        matches = {
            "read": "actions/runs/123",
            "cancel": "actions/runs/123/cancel",
            "pr": "pulls/1",
            "branch": "git/refs/heads/osdc-kind-load/fixture",
            "404": "git/refs/heads/osdc-kind-load/fixture",
        }
        if failure and path == matches[failure]:
            raise RuntimeError("HTTP 404" if failure == "404" else "fixture failure")
        return {"status": "in_progress"} if path == "actions/runs/123" else None

    monkeypatch.setattr(operations, "api", api)
    monkeypatch.setattr(operations, "wait_for", Mock())
    if failure in {"read", "cancel", "pr", "branch"}:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            operations.cleanup_github(record)
    else:
        operations.cleanup_github(record)
    assert "pulls/1" in calls
    assert "git/refs/heads/osdc-kind-load/fixture" in calls
    assert record["cleanup_complete"] == (failure in {None, "404"})


def test_cleanup_recovers_pr_created_before_record_saved(local_state, monkeypatch):
    record = {"repo": REPO, "run_key": "a" * 32, "branch": "osdc-kind-load/fixture"}
    api = Mock(side_effect=[[{"head": {"ref": record["branch"]}, "title": record["run_key"], "number": 7}], None, None])
    monkeypatch.setattr(operations, "api", api)
    operations.cleanup_github(record)
    assert record["pr"] == 7
    assert record["cleanup_complete"]
    with pytest.raises(ValueError, match="does not belong"):
        operations.cleanup_github({"repo": "foreign"})


@pytest.mark.parametrize("action", ["test", "compactor", "load"])
def test_report_preserves_unrun_and_failure_status(local_state, monkeypatch, action):
    monkeypatch.setattr(operations, "load_test", Mock(side_effect=RuntimeError("fixture failure")))
    monkeypatch.setattr(operations, "pytest_suite", Mock(side_effect=RuntimeError("fixture failure")))
    args = argparse.Namespace(action=action, jobs=12, timeout=30, dry_run=False)
    with pytest.raises(RuntimeError, match="fixture failure"):
        operations.run_tests(args)
    report = json.loads((local_state / "report.json").read_text())
    assert report["results"]["load" if action == "test" else action] == "failed"
    if action == "test":
        assert report["results"]["compactor"] == report["results"]["janitor"] == "not_run"


def test_load_preview_makes_no_github_calls(local_state, monkeypatch, capsys):
    monkeypatch.setattr(operations, "get", Mock(return_value={"metadata": {"labels": {"kubernetes.io/arch": "arm64"}}}))
    api = Mock(side_effect=AssertionError("GitHub must not be contacted"))
    monkeypatch.setattr(operations, "api", api)
    operations.load_test(12, 30, True)
    assert "aarch64" in capsys.readouterr().out
    api.assert_not_called()


@pytest.mark.parametrize("scenario", ["success", "failure", "duplicate", "timeout", "stale", "missing-scale"])
def test_load_result_and_cleanup_are_fail_closed(local_state, monkeypatch, scenario):
    monkeypatch.setattr(operations, "restore", Mock())

    def prepare(key, workflow, base, record):
        assert (
            yaml.safe_load(workflow)["jobs"]["load"]["container"]["image"] == "ghcr.io/actions/actions-runner:2.338.0"
        )
        record.update(branch=f"osdc-kind-load/{key}")

    monkeypatch.setattr(operations, "prepare_pr", prepare)
    monkeypatch.setattr(operations, "local_runner_names", Mock(return_value={"observed"}))
    cleanup = Mock()
    monkeypatch.setattr(operations, "cleanup_github", cleanup)
    monkeypatch.setattr(operations.time, "sleep", Mock())
    monkeypatch.setattr(operations.time, "monotonic", Mock(side_effect=[0, 2] if scenario == "timeout" else [0, 0]))
    monkeypatch.setattr(operations.uuid, "uuid4", Mock(return_value=SimpleNamespace(hex="a" * 32)))
    run = {
        "id": 123,
        "name": f"OSDC Kind Load {'a' * 32}",
        "status": "completed",
        "conclusion": "failure" if scenario == "failure" else "success",
        "html_url": "https://github.com/fixture/run/123",
    }
    jobs = [{"name": "load (1)", "status": "completed", "conclusion": "success", "runner_name": "observed"}]

    def get(*args):
        if args[0] == "node":
            return {"metadata": {"labels": {"kubernetes.io/arch": "arm64"}}}
        return {
            "items": []
            if scenario == "missing-scale"
            else [
                {
                    "spec": {
                        "runnerScaleSetName": LABEL,
                        "maxRunners": 3,
                        "template": {
                            "spec": {
                                "containers": [{"name": "runner", "image": "ghcr.io/actions/actions-runner:2.338.0"}]
                            }
                        },
                    }
                }
            ]
        }

    def api(path, **kwargs):
        if not path:
            return {"default_branch": "main"}
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": jobs}
        return {"workflow_runs": [run, run] if scenario == "duplicate" else [run]}

    monkeypatch.setattr(operations, "api", api)
    monkeypatch.setattr(operations, "get", get)
    if scenario == "stale":
        operations.write_json(local_state / "github-run.json", {"cleanup_complete": False})
    if scenario == "success":
        operations.load_test(1, 1, False)
        cleanup.assert_called_once()
    else:
        with pytest.raises((ValueError, TimeoutError), match=r".+"):
            operations.load_test(1, 1, False)
        assert cleanup.called == (scenario not in {"stale", "missing-scale"})


def test_successful_report_and_preview_are_distinct(local_state, monkeypatch):
    monkeypatch.setattr(operations, "load_test", Mock())
    monkeypatch.setattr(operations, "pytest_suite", Mock())
    args = argparse.Namespace(action="test", jobs=12, timeout=30, dry_run=False)
    operations.run_tests(args)
    assert set(json.loads((local_state / "report.json").read_text())["results"].values()) == {"passed"}
    args.action, args.dry_run = "load", True
    operations.run_tests(args)
    assert json.loads((local_state / "report.json").read_text())["results"] == {"load": "preview"}
