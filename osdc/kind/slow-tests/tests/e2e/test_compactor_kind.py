"""Real-controller tests on fixed kind workers; no node provisioning simulation."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/python"))
from kind_slow import KUBE, NAMESPACE, apply, get, guard, kube, restore, wait_for
from kind_slow_config import (
    BASE_ENV,
    OWNER,
    PROTECTED,
    RESERVED,
    TAINT,
    TEST_IMAGE,
    WORKERS,
)


def workers() -> list[dict]:
    return [n for n in get("nodes")["items"] if n["metadata"]["name"] in WORKERS]


def tainted() -> set[str]:
    return {
        n["metadata"]["name"] for n in workers() if any(t["key"] == TAINT for t in n.get("spec", {}).get("taints", []))
    }


def annotations(key: str) -> set[str]:
    return {n["metadata"]["name"] for n in workers() if n["metadata"].get("annotations", {}).get(key) == "true"}


def stop() -> None:
    kube("scale", "deployment/node-compactor", "-n", "kube-system", "--replicas=0")
    wait_for(
        "compactor termination",
        lambda: not get("pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=node-compactor")["items"],
        timeout=180,
    )


def configure(**overrides: str) -> None:
    stop()
    env = BASE_ENV | {"COMPACTOR_DRY_RUN": "false"} | overrides
    kube(
        "set",
        "env",
        "deployment/node-compactor",
        "-n",
        "kube-system",
        *[f"{key}={value}" for key, value in env.items()],
    )
    kube("scale", "deployment/node-compactor", "-n", "kube-system", "--replicas=1")
    kube("rollout", "status", "deployment/node-compactor", "-n", "kube-system", "--timeout=180s")


def metric(name: str) -> float:
    pods = get("pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=node-compactor")["items"]
    pod = next(p for p in pods if not p["metadata"].get("deletionTimestamp"))
    data = kube("get", "--raw", f"/api/v1/namespaces/kube-system/pods/{pod['metadata']['name']}:8080/proxy/metrics")
    return sum(float(line.split()[-1]) for line in data.splitlines() if re.match(rf"^{re.escape(name)}(?:\{{| )", line))


def observe_cycles(count: int = 3) -> None:
    initial = metric("node_compactor_reconcile_cycles_total")
    wait_for(
        "successful reconciliation cycles", lambda: metric("node_compactor_reconcile_cycles_total") >= initial + count
    )
    assert metric("node_compactor_reconcile_cycles_total") >= initial + count
    assert metric('node_compactor_reconcile_cycles_total{status="error"}') == 0


def pod(name: str, node: str, *, bind: bool = True) -> None:
    spec = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 0,
        "containers": [
            {
                "name": "workload",
                "image": TEST_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep 600"],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi"},
                    "limits": {"cpu": "100m", "memory": "32Mi"},
                },
            }
        ],
    }
    if bind:
        spec["nodeName"] = node
    else:
        spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    apply([{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE}, "spec": spec}])


@pytest.fixture(autouse=True)
def owned_cluster():
    if os.environ.get("OSDC_KIND_SLOW_KUBECONFIG") != str(KUBE):
        pytest.fail("Run through 'just kind-slow-compactor' with the isolated kubeconfig")
    guard()
    original_uids = {n["metadata"]["name"]: n["metadata"]["uid"] for n in workers()}
    apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": {OWNER: "true"}}}])
    kube("delete", "pods", "--all", "-n", NAMESPACE, "--wait=true")
    restore()
    try:
        yield
        assert {n["metadata"]["name"]: n["metadata"]["uid"] for n in workers()} == original_uids
    finally:
        kube("delete", "pods", "--all", "-n", NAMESPACE, "--wait=true")
        restore()


def test_taints_do_not_evict_running_workloads():
    for index, node in enumerate(WORKERS):
        pod(f"resident-{index}", node)
    wait_for(
        "three running workload pods",
        lambda: all(p["status"]["phase"] == "Running" for p in get("pods", "-n", NAMESPACE)["items"]),
    )
    before = {p["metadata"]["name"]: p["metadata"]["uid"] for p in get("pods", "-n", NAMESPACE)["items"]}
    assert len(before) == 3
    configure()
    wait_for("two surplus workers tainted", lambda: len(tainted()) == 2)
    observe_cycles()
    after = get("pods", "-n", NAMESPACE)["items"]
    assert {p["metadata"]["name"]: p["metadata"]["uid"] for p in after} == before
    assert all(p["status"]["phase"] == "Running" for p in after)
    assert len(tainted()) == 2


def test_minimum_node_age_blocks_then_allows_taints():
    configure(COMPACTOR_MIN_NODE_AGE="2147483647")
    observe_cycles()
    assert not tainted()
    configure(COMPACTOR_MIN_NODE_AGE="0")
    wait_for("age gate released", lambda: len(tainted()) == 2)


def test_rate_limit_is_applied():
    configure(COMPACTOR_TAINT_RATE="0.34")
    wait_for("rate-limit metric increments", lambda: metric("compactor_rate_limit_blocks_total") > 0)
    wait_for("eventual consolidation", lambda: len(tainted()) == 2)


def test_burst_untaint_honors_fleet_cooldown():
    configure(COMPACTOR_FLEET_COOLDOWN="40")
    wait_for("initial consolidation", lambda: len(tainted()) == 2)
    target = sorted(tainted())[0]
    pod("burst", target, bind=False)
    wait_for(
        "pending pod causes a real burst untaint",
        lambda: get("pod", "burst", "-n", NAMESPACE).get("status", {}).get("phase") == "Running",
    )
    wait_for("fleet cooldown metric increments", lambda: metric("compactor_fleet_cooldown_blocks_total") > 0)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        assert len(tainted()) < 2
        time.sleep(1)
    # The controller halves the window when more than half the fleet is surplus.
    wait_for("consolidation resumes after cooldown", lambda: len(tainted()) == 2, timeout=90)


def test_capacity_reservation_and_shutdown_cleanup():
    configure(COMPACTOR_CAPACITY_RESERVATION_NODES="1")
    wait_for("one reserved worker", lambda: len(annotations(RESERVED)) == 1)
    wait_for(
        "reserved worker protected and untainted",
        lambda: (
            annotations(RESERVED) == annotations(PROTECTED)
            and not annotations(RESERVED) & tainted()
            and len(tainted()) == 2
        ),
    )
    assert metric("compactor_reserved_nodes") == 1
    stop()
    assert not tainted()
    assert not annotations(RESERVED)
    assert not annotations(PROTECTED)
