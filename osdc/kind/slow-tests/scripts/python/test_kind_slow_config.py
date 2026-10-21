"""Offline checks for cluster isolation, configuration and fail-closed results."""

import copy

import pytest
import yaml
from kind_slow_config import (
    BASE_ENV,
    CLUSTER,
    COMPACTOR_IMAGE,
    CONTEXT,
    JANITOR_ENV,
    JANITOR_IMAGE,
    LABEL,
    NODES,
    OWNER,
    POOL,
    WORKERS,
    adapt_manifests,
    kind_config,
    load_workflow,
    merge_env,
    nodepool_documents,
    runner_values,
    validate_load_jobs,
    validate_nodes,
)


def nodes():
    return [
        {
            "metadata": {"name": name, "labels": {OWNER: "true", "karpenter.sh/nodepool": POOL}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for name in NODES
    ]


def test_fixed_topology_and_guard():
    cfg = kind_config()
    assert cfg["kind"] == "Cluster"
    assert len(cfg["nodes"]) == 4
    assert [n["role"] for n in cfg["nodes"]] == ["control-plane", "worker", "worker", "worker"]
    assert CLUSTER not in {"osdc-drain-local", "meta-staging-aws-uw1"}
    validate_nodes(CONTEXT, nodes())


@pytest.mark.parametrize("damage", ["context", "missing", "duplicate", "owner", "deleting", "ready", "pool"])
def test_guard_rejects_wrong_or_unready_clusters(damage):
    observed = nodes()
    context = CONTEXT
    if damage == "context":
        context = "kind-osdc-drain-local"
    elif damage == "missing":
        observed.pop()
    elif damage == "duplicate":
        observed.append(observed[-1])
    elif damage == "owner":
        observed[0]["metadata"]["labels"].pop(OWNER)
    elif damage == "deleting":
        observed[0]["metadata"]["deletionTimestamp"] = "now"
    elif damage == "ready":
        observed[0]["status"]["conditions"][0]["status"] = "False"
    else:
        observed[1]["metadata"]["labels"]["karpenter.sh/nodepool"] = "another"
    with pytest.raises(ValueError, match=r".+"):
        validate_nodes(context, observed)


def test_env_merge_keeps_valuefrom():
    container = {
        "env": [
            {"name": "identity", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "COMPACTOR_DRY_RUN", "value": "false"},
        ]
    }
    merge_env(container, BASE_ENV)
    assert container["env"][0]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert len([e for e in container["env"] if e["name"] == "COMPACTOR_DRY_RUN"]) == 1


@pytest.mark.parametrize(
    ("component", "image", "env"), [("compactor", COMPACTOR_IMAGE, BASE_ENV), ("janitor", JANITOR_IMAGE, JANITOR_ENV)]
)
def test_real_manifests_are_adapted_without_mutating_the_source(component, image, env):
    docs = [
        {"kind": "ConfigMap", "data": {"unchanged": "yes"}},
        {
            "kind": "Deployment",
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": "upstream", "livenessProbe": {}, "readinessProbe": {}}]
                    }
                }
            },
        },
    ]
    original = copy.deepcopy(docs)
    result = adapt_manifests(docs, component)
    assert docs == original
    assert result[0] == docs[0]
    spec = result[1]["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert container["image"] == image
    assert {e["name"]: e["value"] for e in container["env"]} == env
    if component == "compactor":
        assert spec["tolerations"][0]["key"] == "node-role.kubernetes.io/control-plane"
    else:
        assert spec["nodeSelector"] == {OWNER: "true", "karpenter.sh/nodepool": POOL}
    with pytest.raises(ValueError, match="Unknown component"):
        adapt_manifests([], "unknown")


def test_nodepool_crd_is_discovery_metadata():
    crd, pool = nodepool_documents()
    assert crd["spec"]["group"] == "karpenter.sh"
    assert pool["metadata"]["labels"]["osdc.io/node-compactor"] == "true"
    assert "nodeClassRef" not in pool["spec"]["template"]
    assert len(WORKERS) == 3


def test_actual_compactor_template_has_no_unresolved_environment():
    from kind_slow import OSDC

    upstream = OSDC / "base/node-compactor/kubernetes/deployment.yaml"
    document = yaml.safe_load(upstream.read_text())
    result = adapt_manifests([document], "compactor")
    env = result[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not any("_PLACEHOLDER" in item["value"] for item in env)
    assert next(item["value"] for item in env if item["name"] == "COMPACTOR_MAX_UPTIME_HOURS") == "48"
    document["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "FUTURE_SETTING", "value": "FUTURE_PLACEHOLDER"}
    )
    with pytest.raises(ValueError, match="Unresolved"):
        adapt_manifests([document], "compactor")


def test_runner_profile_uses_separate_scale_set_and_keeps_capacity_awareness():
    values = runner_values("runner-image")
    assert values["runnerScaleSetName"] == LABEL
    assert values["maxRunners"] == 3
    settings = {e["name"]: e["value"] for e in values["listenerTemplate"]["spec"]["containers"][0]["env"]}
    assert settings["CAPACITY_AWARE_ENABLED"] == "true"
    assert settings["CAPACITY_AWARE_NODE_FLEET"] == POOL
    assert "proxy" not in values
    proxy = {"http": {"url": "http://host.docker.internal:13660"}}
    values = runner_values("runner-image", proxy)
    values["proxy"]["http"]["url"] = "changed"
    assert proxy["http"]["url"] != "changed"


@pytest.mark.parametrize("arch", ["aarch64", "x86_64"])
def test_load_workflow_uses_native_arch_and_real_local_runner(arch):
    workflow = load_workflow("a" * 32, 12, arch, "runner-image", "main")
    job = workflow["jobs"]["load"]
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert job["runs-on"] == LABEL
    assert job["strategy"]["matrix"]["index"] == list(range(1, 13))
    assert job["strategy"]["max-parallel"] == 3
    assert arch in job["steps"][0]["run"]
    assert job["steps"][1]["with"]["repository"] == "pytorch/pytorch"


@pytest.mark.parametrize(
    ("key", "count", "arch"),
    [("bad", 12, "aarch64"), ("a" * 32, 0, "aarch64"), ("a" * 32, 101, "aarch64"), ("a" * 32, 12, "unknown")],
)
def test_invalid_load_workflows(key, count, arch):
    with pytest.raises(ValueError, match=r".+"):
        load_workflow(key, count, arch, "runner-image", "main")


@pytest.mark.parametrize(
    "damage", [None, "missing", "duplicate", "name", "failed", "unfinished", "foreign", "unassigned"]
)
def test_success_requires_all_expected_jobs_on_observed_local_runners(damage):
    jobs = [
        {"name": f"load ({index})", "status": "completed", "conclusion": "success", "runner_name": "observed"}
        for index in (1, 2)
    ]
    if damage == "missing":
        jobs.pop()
    elif damage == "duplicate":
        jobs[1] = jobs[0]
    elif damage == "name":
        jobs[1]["name"] = "other"
    elif damage == "failed":
        jobs[1]["conclusion"] = "failure"
    elif damage == "unfinished":
        jobs[1]["status"] = "queued"
    elif damage == "foreign":
        jobs[1]["runner_name"] = "github-hosted"
    elif damage == "unassigned":
        jobs[1]["runner_name"] = ""
    if damage:
        with pytest.raises(ValueError, match=r".+"):
            validate_load_jobs(jobs, 2, {"observed"})
    else:
        validate_load_jobs(jobs, 2, {"observed"})
