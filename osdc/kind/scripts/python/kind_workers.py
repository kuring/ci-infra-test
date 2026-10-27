"""Stage an owned Kind worker as local replacement capacity without changing OSDC."""

import argparse
import json
import logging

import drain_test_kind as kind
from drain_test_run import REFRESH_TAINT_KEY

PRIMARY = "osdc-drain-local-worker"
RESERVE = "osdc-drain-local-worker2"
CONTROL = "osdc-drain-local-control-plane"


def validate_nodes(nodes, *, allow_legacy=False):
    names = {node["metadata"]["name"] for node in nodes}
    expected = {CONTROL, PRIMARY, RESERVE}
    legacy = allow_legacy and names == {CONTROL, PRIMARY}
    if (names != expected and not legacy) or len(nodes) != len(names):
        raise ValueError(f"Unexpected Kind nodes: {sorted(names)}")
    if any(node["metadata"].get("labels", {}).get("osdc.io/drain-kind") != "true" for node in nodes):
        raise ValueError("Missing Kind ownership label")
    return {node["metadata"]["name"]: node for node in nodes}


def guard(*, allow_legacy=False):
    context = kind.command(
        ["kubectl", "--kubeconfig", str(kind.ROOT / "kubeconfig"), "config", "current-context"]
    ).stdout
    if context.strip() != kind.CONTEXT:
        raise ValueError(f"Not the owned Kind context: {context.strip()}")
    nodes = validate_nodes(json.loads(kind.kube("get", "nodes", "-o", "json"))["items"], allow_legacy=allow_legacy)
    logging.info("guard: context=%s, nodes=%s", kind.CONTEXT, sorted(nodes))
    return nodes


def workloads_gone():
    pods = json.loads(kind.kube("get", "pods", "-n", "arc-runners", "-o", "json"))["items"]
    return not any(
        pod["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "runner"
        or pod["metadata"].get("labels", {}).get("runner-pod")
        for pod in pods
    )


def reset():
    nodes = guard()
    kind.wait_for("previous runner and workflow Pods removed before worker reset", workloads_gone, 180, poll=3)
    # Hide reserve capacity before clearing prior test taints. This runs only at
    # the start of a new test, never during upstream resume or its final audit.
    for name, fleet in ((RESERVE, "kind-reserve"), (PRIMARY, "kind-local")):
        workload = "github-runner" if name == PRIMARY else "kind-reserve"
        kind.kube("label", "node", name, f"node-fleet={fleet}", f"workload-type={workload}", "--overwrite")
        if any(taint.get("key") == REFRESH_TAINT_KEY for taint in nodes[name]["spec"].get("taints", [])):
            kind.kube("taint", "node", name, f"{REFRESH_TAINT_KEY}-")
    logging.info("Primary worker enabled; reserve worker held outside the runner fleet")


def activate():
    nodes = guard()
    if not kind.DrainTest.has_refresh_taint(nodes[PRIMARY]):
        raise ValueError("Primary worker must retain its exact refresh taint before activating reserve capacity")
    reserve = nodes[RESERVE]
    labels = reserve["metadata"].get("labels", {})
    if labels.get("node-fleet") != "kind-reserve" or labels.get("workload-type") != "kind-reserve":
        raise ValueError("Reserve worker was already eligible before the drain refresh checkpoint")
    if kind.DrainTest.has_refresh_taint(reserve):
        raise ValueError("Reserve worker is refresh tainted")
    kind.kube("label", "node", RESERVE, "node-fleet=kind-local", "workload-type=github-runner", "--overwrite")
    logging.info("Activated reserve worker %s (UID %s)", RESERVE, reserve["metadata"]["uid"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["guard", "reset", "activate"])
    parser.add_argument("--allow-legacy", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.allow_legacy and args.action != "guard":
        parser.error("--allow-legacy is only allowed for the migration guard")
    if args.action == "guard":
        guard(allow_legacy=args.allow_legacy)
    elif args.action == "reset":
        reset()
    else:
        activate()


if __name__ == "__main__":
    main()
