"""Guard the reserve capacity boundary and prohibit recycling during live jobs."""

import copy
import json
import subprocess
from unittest.mock import Mock

import kind_workers as module
import pytest
from drain_test_run import REFRESH_TAINT_EFFECT, REFRESH_TAINT_KEY, REFRESH_TAINT_VALUE


@pytest.fixture
def nodes():
    result = {}
    for name in (module.CONTROL, module.PRIMARY, module.RESERVE):
        fleet = "kind-reserve" if name == module.RESERVE else "kind-local"
        result[name] = {
            "metadata": {
                "name": name,
                "uid": name + "-uid",
                "labels": {
                    "osdc.io/drain-kind": "true",
                    "node-fleet": fleet,
                    "workload-type": "kind-reserve" if name == module.RESERVE else "github-runner",
                },
            },
            "spec": {},
        }
    return result


def taint(node):
    node["spec"]["taints"] = [{"key": REFRESH_TAINT_KEY, "value": REFRESH_TAINT_VALUE, "effect": REFRESH_TAINT_EFFECT}]


def test_guard_requires_owned_context_and_exact_node_set(nodes, monkeypatch):
    command = Mock(return_value=subprocess.CompletedProcess([], 0, module.kind.CONTEXT, ""))
    monkeypatch.setattr(module.kind, "command", command)
    kube = Mock(return_value=json.dumps({"items": list(nodes.values())}))
    monkeypatch.setattr(module.kind, "kube", kube)
    assert module.guard() == nodes
    command.return_value.stdout = "production"
    with pytest.raises(ValueError, match="owned Kind context"):
        module.guard()
    assert kube.call_count == 1


def test_guard_legacy_is_explicit_and_rejects_missing_ownership(nodes):
    legacy = [nodes[module.CONTROL], nodes[module.PRIMARY]]
    with pytest.raises(ValueError, match="Unexpected Kind nodes"):
        module.validate_nodes(legacy)
    assert set(module.validate_nodes(legacy, allow_legacy=True)) == {module.CONTROL, module.PRIMARY}
    with pytest.raises(ValueError, match="Unexpected Kind nodes"):
        module.validate_nodes([*nodes.values(), nodes[module.PRIMARY]])
    nodes[module.RESERVE]["metadata"]["labels"].pop("osdc.io/drain-kind")
    with pytest.raises(ValueError, match="ownership label"):
        module.validate_nodes(list(nodes.values()))


def test_reset_hides_reserve_before_removing_previous_taints(nodes, monkeypatch):
    for name in (module.PRIMARY, module.RESERVE):
        taint(nodes[name])
    monkeypatch.setattr(module, "guard", lambda: nodes)
    monkeypatch.setattr(module.kind, "wait_for", lambda *args, **kwargs: None)
    kube = Mock()
    monkeypatch.setattr(module.kind, "kube", kube)
    module.reset()
    assert kube.call_args_list[0].args == (
        "label",
        "node",
        module.RESERVE,
        "node-fleet=kind-reserve",
        "workload-type=kind-reserve",
        "--overwrite",
    )
    assert kube.call_args_list[1].args == ("taint", "node", module.RESERVE, REFRESH_TAINT_KEY + "-")
    assert kube.call_args_list[2].args[2] == module.PRIMARY
    assert kube.call_args_list[3].args == ("taint", "node", module.PRIMARY, REFRESH_TAINT_KEY + "-")


def test_reset_refuses_to_mutate_before_previous_workloads_are_gone(nodes, monkeypatch):
    monkeypatch.setattr(module, "guard", lambda: nodes)
    monkeypatch.setattr(module.kind, "wait_for", Mock(side_effect=TimeoutError("live workloads")))
    kube = Mock()
    monkeypatch.setattr(module.kind, "kube", kube)
    with pytest.raises(TimeoutError, match="live workloads"):
        module.reset()
    kube.assert_not_called()


@pytest.mark.parametrize("labels", [{"app.kubernetes.io/component": "runner"}, {"runner-pod": "runner"}, {}])
def test_workload_gate_includes_runner_and_workflow_pods(labels, monkeypatch):
    monkeypatch.setattr(module.kind, "kube", lambda *args: json.dumps({"items": [{"metadata": {"labels": labels}}]}))
    assert module.workloads_gone() == (not labels)


@pytest.mark.parametrize("invalid", ["no-taint", "wrong-value", "wrong-effect", "reserve-eligible", "reserve-tainted"])
def test_activation_requires_old_taint_and_fresh_ineligible_reserve(nodes, monkeypatch, invalid):
    taint(nodes[module.PRIMARY])
    if invalid == "no-taint":
        nodes[module.PRIMARY]["spec"]["taints"] = []
    elif invalid in {"wrong-value", "wrong-effect"}:
        key = "value" if invalid == "wrong-value" else "effect"
        nodes[module.PRIMARY]["spec"]["taints"][0][key] = "other"
    elif invalid == "reserve-eligible":
        nodes[module.RESERVE]["metadata"]["labels"]["node-fleet"] = "kind-local"
    else:
        taint(nodes[module.RESERVE])
    monkeypatch.setattr(module, "guard", lambda: nodes)
    kube = Mock()
    monkeypatch.setattr(module.kind, "kube", kube)
    with pytest.raises(ValueError, match=r"Primary worker|Reserve worker"):
        module.activate()
    kube.assert_not_called()


def test_activation_changes_only_reserve_labels_and_preserves_old_uid_taint(nodes, monkeypatch):
    taint(nodes[module.PRIMARY])
    old = copy.deepcopy(nodes[module.PRIMARY])
    monkeypatch.setattr(module, "guard", lambda: nodes)
    kube = Mock()
    monkeypatch.setattr(module.kind, "kube", kube)
    module.activate()
    kube.assert_called_once_with(
        "label", "node", module.RESERVE, "node-fleet=kind-local", "workload-type=github-runner", "--overwrite"
    )
    assert nodes[module.PRIMARY] == old
