"""Pure configuration and result validation for the local slow-test profile."""

from __future__ import annotations

import copy
import re

CLUSTER = "osdc-slow-local"
CONTEXT = f"kind-{CLUSTER}"
POOL = "kind-slow"
LABEL = "osdc-kind-slow"
REPO = "kuring/ci-infra"
OWNER = "osdc.io/slow-kind"
TAINT = "node-compactor.osdc.io/consolidating"
RESERVED = "node-compactor.osdc.io/capacity-reserved"
PROTECTED = "karpenter.sh/do-not-disrupt"
WORKERS = [f"{CLUSTER}-worker{suffix}" for suffix in ("", "2", "3")]
NODES = [f"{CLUSTER}-control-plane", *WORKERS]
TEST_IMAGE = "public.ecr.aws/docker/library/alpine:3.21"
COMPACTOR_IMAGE = "osdc-local/node-compactor:slow"
JANITOR_IMAGE = "osdc-local/image-cache-janitor:slow"
BASE_ENV = {
    "COMPACTOR_DRY_RUN": "true",
    "COMPACTOR_INTERVAL": "2",
    "COMPACTOR_MAX_UPTIME_HOURS": "48",
    "COMPACTOR_MIN_NODES": "1",
    "COMPACTOR_MIN_NODE_AGE": "0",
    "COMPACTOR_TAINT_COOLDOWN": "0",
    "COMPACTOR_FLEET_COOLDOWN": "0",
    "COMPACTOR_TAINT_RATE": "1.0",
    "COMPACTOR_SPARE_CAPACITY_NODES": "0",
    "COMPACTOR_SPARE_CAPACITY_RATIO": "0",
    "COMPACTOR_CAPACITY_RESERVATION_NODES": "0",
    "COMPACTOR_PEAK_WINDOW_SECONDS": "0",
    "COMPACTOR_PENDING_POD_MIN_AGE_SECONDS": "0",
}
JANITOR_ENV = {"IMAGE_CACHE_LIMIT_GI": "9999", "IMAGE_CACHE_TARGET_GI": "9998", "CHECK_INTERVAL_SECONDS": "5"}
CONTROL_TOLERATION = {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
NOT_COVERED = [
    "AWS/EKS/IAM/VPC infrastructure",
    "Karpenter node provisioning and WhenEmpty deletion",
    "GPU and other CPU architectures",
    "Production fleet scale and performance",
]


def kind_config() -> dict:
    nodes = [{"role": "control-plane", "labels": {OWNER: "true", "role": "base-infrastructure"}}]
    for _ in WORKERS:
        nodes.append(
            {
                "role": "worker",
                "labels": {
                    OWNER: "true",
                    "karpenter.sh/nodepool": POOL,
                    "node-fleet": POOL,
                    "workload-type": "github-runner",
                },
            }
        )
    return {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "nodes": nodes}


def validate_nodes(context: str, nodes: list[dict]) -> None:
    if context != CONTEXT or sorted(n["metadata"]["name"] for n in nodes) != sorted(NODES):
        raise ValueError("Refusing to operate outside the dedicated slow-test kind cluster")
    for node in nodes:
        labels = node["metadata"].get("labels", {})
        if labels.get(OWNER) != "true":
            raise ValueError("Node ownership label missing")
        if node["metadata"].get("deletionTimestamp"):
            raise ValueError("A test node is being deleted")
        if not any(
            c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", [])
        ):
            raise ValueError("A test node is not Ready")
        if node["metadata"]["name"] in WORKERS and labels.get("karpenter.sh/nodepool") != POOL:
            raise ValueError("Worker NodePool label differs from the local profile")


def merge_env(container: dict, values: dict[str, str]) -> None:
    env = {e["name"]: e for e in container.get("env", [])}
    env.update({key: {"name": key, "value": value} for key, value in values.items()})
    container["env"] = list(env.values())


def adapt_manifests(documents: list[dict], component: str) -> list[dict]:
    if component not in {"compactor", "janitor"}:
        raise ValueError("Unknown component")
    result = copy.deepcopy(documents)
    for doc in result:
        if doc["kind"] not in {"Deployment", "DaemonSet"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        container = spec["containers"][0]
        container["image"] = COMPACTOR_IMAGE if component == "compactor" else JANITOR_IMAGE
        container["imagePullPolicy"] = "IfNotPresent"
        merge_env(container, BASE_ENV if component == "compactor" else JANITOR_ENV)
        if any("_PLACEHOLDER" in env.get("value", "") for env in container["env"]):
            raise ValueError("Unresolved upstream environment placeholder")
        if component == "compactor":
            spec.setdefault("tolerations", []).append(CONTROL_TOLERATION)
            container["resources"] = {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            }
            for probe in ("livenessProbe", "readinessProbe"):
                container[probe]["initialDelaySeconds"] = 2
                container[probe]["periodSeconds"] = 2
        else:
            spec["nodeSelector"] = {OWNER: "true", "karpenter.sh/nodepool": POOL}
    return result


def nodepool_documents() -> list[dict]:
    # Discovery metadata only: this CRD does not provision or delete nodes.
    return [
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "nodepools.karpenter.sh"},
            "spec": {
                "group": "karpenter.sh",
                "scope": "Cluster",
                "names": {"plural": "nodepools", "singular": "nodepool", "kind": "NodePool"},
                "versions": [
                    {
                        "name": "v1",
                        "served": True,
                        "storage": True,
                        "schema": {
                            "openAPIV3Schema": {
                                "type": "object",
                                "properties": {
                                    "spec": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}
                                },
                            }
                        },
                    }
                ],
            },
        },
        {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": POOL, "labels": {"osdc.io/node-compactor": "true", OWNER: "true"}},
            "spec": {"template": {"metadata": {"labels": {"node-fleet": POOL, "workload-type": "github-runner"}}}},
        },
    ]


def runner_values(image: str, proxy: dict | None = None) -> dict:
    values = {
        "githubConfigUrl": f"https://github.com/{REPO}",
        "githubConfigSecret": "osdc-slow-github",
        "runnerScaleSetName": LABEL,
        "runnerGroup": "default",
        "minRunners": 0,
        "maxRunners": 3,
        "controllerServiceAccount": {"namespace": "arc-systems", "name": "arc-controller"},
        "containerMode": {"type": "kubernetes-novolume"},
        "listenerTemplate": {
            "spec": {
                "nodeSelector": {"role": "base-infrastructure"},
                "tolerations": [CONTROL_TOLERATION],
                "containers": [
                    {
                        "name": "listener",
                        "env": [
                            {"name": key, "value": value}
                            for key, value in {
                                "CAPACITY_AWARE_ENABLED": "true",
                                "CAPACITY_AWARE_PROACTIVE_CAPACITY": "3",
                                "CAPACITY_AWARE_MAX_BURST_CAPACITY": "3",
                                "CAPACITY_AWARE_NODE_FLEET": POOL,
                                "CAPACITY_AWARE_RUNNER_NODE_FLEET": POOL,
                                "CAPACITY_AWARE_WORKFLOW_CPU": "150m",
                                "CAPACITY_AWARE_WORKFLOW_MEMORY": "128Mi",
                                "CAPACITY_AWARE_RUNNER_CPU": "200m",
                                "CAPACITY_AWARE_RUNNER_MEMORY": "512Mi",
                                "CAPACITY_AWARE_HUD_API_URL": "",
                                "CAPACITY_AWARE_HUD_FAILURE_MULTIPLIER": "1",
                                "CAPACITY_AWARE_HUD_FAILURE_BASE_CAPACITY": "1",
                                "CAPACITY_AWARE_RECALCULATE_INTERVAL": "5s",
                                "CAPACITY_AWARE_REPORT_INTERVAL": "2s",
                            }.items()
                        ],
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "1", "memory": "512Mi"},
                        },
                    }
                ],
            }
        },
        "template": {
            "spec": {
                "nodeSelector": {"node-fleet": POOL},
                "containers": [
                    {
                        "name": "runner",
                        "image": image,
                        "command": ["/home/runner/run.sh"],
                        "env": [{"name": "ACTIONS_RUNNER_CONTAINER_HOOK_TEMPLATE", "value": "/etc/osdc-slow/pod.yaml"}],
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "512Mi"},
                            "limits": {"cpu": "1", "memory": "768Mi"},
                        },
                        "volumeMounts": [{"name": "job-template", "mountPath": "/etc/osdc-slow", "readOnly": True}],
                    }
                ],
                "volumes": [{"name": "job-template", "configMap": {"name": "osdc-slow-job-template"}}],
            }
        },
    }
    if proxy:
        values["proxy"] = copy.deepcopy(proxy)
    return values


def load_workflow(run_key: str, jobs: int, architecture: str, image: str, base: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", run_key):
        raise ValueError("Invalid run key")
    if not 1 <= jobs <= 100:
        raise ValueError("Local load tests support 1 to 100 jobs")
    if architecture not in {"aarch64", "x86_64"}:
        raise ValueError("Unsupported worker architecture")
    return {
        "name": f"OSDC Kind Load {run_key}",
        "on": {"pull_request": {"branches": [base]}},
        "permissions": {"contents": "read"},
        "concurrency": {"group": f"osdc-kind-load-{run_key}", "cancel-in-progress": False},
        "jobs": {
            "load": {
                "runs-on": LABEL,
                "timeout-minutes": 10,
                "container": {"image": image},
                "strategy": {"fail-fast": False, "max-parallel": 3, "matrix": {"index": list(range(1, jobs + 1))}},
                "steps": [
                    {"name": "Verify architecture", "run": f'test "$(uname -m)" = "{architecture}"'},
                    {
                        "name": "Checkout pytorch",
                        "uses": "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",
                        "with": {"repository": "pytorch/pytorch", "fetch-depth": 1},
                    },
                    {
                        "name": "Report metadata",
                        "run": f'echo "runner_type={LABEL}"\necho "runner_name=${{{{ runner.name }}}}"\necho "matrix_index=${{{{ matrix.index }}}}"',
                    },
                ],
            }
        },
    }


def validate_load_jobs(jobs: list[dict], expected: int, runner_names: set[str]) -> None:
    expected_names = {f"load ({i})" for i in range(1, expected + 1)}
    if len(jobs) != expected or {job["name"] for job in jobs} != expected_names:
        raise ValueError("Missing, duplicate or unexpected load jobs")
    for job in jobs:
        if job.get("conclusion") != "success" or job.get("status") != "completed":
            raise ValueError(f"Load job did not succeed: {job['name']}")
        if not job.get("runner_name") or job["runner_name"] not in runner_names:
            raise ValueError(f"Load job did not run on an observed local runner: {job['name']}")
