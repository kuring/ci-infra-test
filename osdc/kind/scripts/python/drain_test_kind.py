"""An isolated kind backend for the same drain/undrain controller and recipes.

Only the kubeconfig recipe is overridden. Drain, taint, resume and limit-map
calculation are imported unchanged from OSDC. Credentials stay in .state/.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import yaml

# This local-only adapter reuses the shared controller without adding kind
# imports or dependencies to the code that will be merged into ci-infra.
LOCAL = Path(__file__).resolve().parents[2]
UPSTREAM = Path(os.environ.get("OSDC_UPSTREAM", LOCAL.parents[2] / "ci-infra/osdc")).resolve()
if not (UPSTREAM / "integration-tests/drain-test/scripts/python/drain_test_run.py").is_file():
    raise RuntimeError(f"Missing upstream drain-test under {UPSTREAM}; set OSDC_UPSTREAM to ci-infra/osdc")
sys.path.insert(0, str(UPSTREAM / "integration-tests/drain-test/scripts/python"))
from drain_test_checks import probe_scale_set, require_limits, runner_limits  # noqa: E402
from drain_test_run import (  # noqa: E402
    CLEANUP_TIMEOUT,
    RESTORE_TIMEOUT,
    RUN_TIMEOUT,
    DrainTest,
    api,
    command,
    generate_workflow,
    positive,
    run_controller,
    wait_for,
)

ROOT = LOCAL / ".state"
LOCAL_CLUSTER = "kind-local"
CONTEXT = "kind-osdc-drain-local"
REPOSITORY = "kuring/ci-infra"
LABEL = "osdc-kind-drain"
VERSION = "0.14.1-jeanschmidt.20"


def generate_kind_workflow(key):
    # Adapt shared group targeting locally to the repository-scoped scale set.
    return re.sub(
        r"(?m)^    runs-on:\n(?:      .*\n)+",
        f"    runs-on: {LABEL}\n",
        generate_workflow(key),
    )


def read_token(root: Path) -> str:
    path = root / "github-token"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Dedicated PAT file must be a regular owner-only file (chmod 600)")
    token = path.read_text().strip()
    if not token.startswith("github_pat_"):
        raise ValueError("Expected the dedicated fine-grained PAT file")
    return token


def guard() -> None:
    command(["just", "--justfile", str(LOCAL / "justfile"), "kind-drain-guard"], cwd=UPSTREAM)


def kube(*args: str, **kwargs) -> str:
    return command(
        ["kubectl", "--kubeconfig", str(ROOT / "kubeconfig"), "--context", CONTEXT, "--request-timeout=15s", *args],
        **kwargs,
    ).stdout


def runner_values() -> dict:
    settings = {
        "CAPACITY_AWARE_ENABLED": "true",
        "CAPACITY_AWARE_PROACTIVE_CAPACITY": "3",
        "CAPACITY_AWARE_MAX_BURST_CAPACITY": "3",
        "CAPACITY_AWARE_HUD_API_URL": "",
        "CAPACITY_AWARE_HUD_FAILURE_MULTIPLIER": "1",
        "CAPACITY_AWARE_HUD_FAILURE_BASE_CAPACITY": "1",
        "CAPACITY_AWARE_RECALCULATE_INTERVAL": "5s",
        "CAPACITY_AWARE_REPORT_INTERVAL": "2s",
        "CAPACITY_AWARE_NODE_FLEET": "kind-local",
        "CAPACITY_AWARE_RUNNER_NODE_FLEET": "kind-local",
        "CAPACITY_AWARE_WORKFLOW_CPU": "150m",
        "CAPACITY_AWARE_WORKFLOW_MEMORY": "128Mi",
        "CAPACITY_AWARE_RUNNER_CPU": "200m",
        "CAPACITY_AWARE_RUNNER_MEMORY": "512Mi",
    }
    values = {
        "githubConfigUrl": f"https://github.com/{REPOSITORY}",
        "githubConfigSecret": "osdc-kind-github",
        "runnerScaleSetName": LABEL,
        "runnerGroup": "default",
        "maxRunners": 3,
        "minRunners": 0,
        "controllerServiceAccount": {"namespace": "arc-systems", "name": "arc-controller"},
        "containerMode": {"type": "kubernetes-novolume"},
        "listenerMetrics": {
            "gauges": {
                "gha_capacity_advertised_max_runners": {
                    "labels": ["name", "namespace", "repository", "organization", "enterprise"]
                }
            }
        },
        "listenerTemplate": {
            "spec": {
                "nodeSelector": {"role": "base-infrastructure"},
                "tolerations": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
                ],
                "containers": [
                    {
                        "name": "listener",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"cpu": "500m", "memory": "512Mi"},
                        },
                        "env": [{"name": name, "value": value} for name, value in settings.items()],
                    }
                ],
            }
        },
        "template": {
            "spec": {
                "nodeSelector": {"node-fleet": "kind-local"},
                "containers": [
                    {
                        "name": "runner",
                        "image": (
                            "ghcr.io/actions/actions-runner:2.338.0"
                            if os.environ.get("OSDC_KIND_ARC_PUBLIC_IMAGES") == "true"
                            else "mirror.aone.alibaba-inc.com/mirrors/ghcr.io/actions/actions-runner:2.338.0"
                        ),
                        "command": ["/home/runner/run.sh"],
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "512Mi"},
                            "limits": {"cpu": "1", "memory": "768Mi"},
                        },
                        "env": [{"name": "ACTIONS_RUNNER_CONTAINER_HOOK_TEMPLATE", "value": "/etc/osdc-kind/pod.yaml"}],
                        "volumeMounts": [{"name": "job-template", "mountPath": "/etc/osdc-kind", "readOnly": True}],
                    }
                ],
                "volumes": [{"name": "job-template", "configMap": {"name": "osdc-kind-job-template"}}],
            }
        },
    }
    # Per-machine outbound configuration stays outside tracked files. The chart
    # propagates this proxy to GitHub clients in the controller/listener/runner.
    proxy_file = ROOT / "proxy.yaml"
    if proxy_file.exists() and os.environ.get("OSDC_KIND_DIRECT_NETWORK") != "true":
        values["proxy"] = yaml.safe_load(proxy_file.read_text())["proxy"]
    return values


def render() -> dict[Path, str]:
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o700)
    defs = ROOT / "modules/arc-runners/defs"
    defs.mkdir(parents=True, exist_ok=True)
    config = {
        "clusters": {
            LOCAL_CLUSTER: {
                "cluster_name": "osdc-drain-local",
                "region": "kind",
                "modules": ["arc-runners"],
                "arc-runners": {"runner_name_prefix": "", "runner_group": "default"},
            }
        }
    }
    consumer = f'''set allow-duplicate-recipes
set allow-duplicate-variables
import "{UPSTREAM}/justfile"
ROOT := "{ROOT}"
UPSTREAM := "{UPSTREAM}"
CLUSTERS_YAML := ROOT / "clusters.yaml"

kubeconfig cluster:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ "{{{{cluster}}}}" == "{LOCAL_CLUSTER}" ]] || {{ echo "Local cluster only" >&2; exit 1; }}
    [[ "${{KUBECONFIG:-}}" == "{{{{ROOT}}}}/kubeconfig" ]] || {{ echo "Isolated kubeconfig required" >&2; exit 1; }}
    just --justfile "{LOCAL}/justfile" kind-drain-guard
'''
    files = {
        defs / "local.yaml": yaml.safe_dump({"runner": {"name": LABEL, "max_runners": 3}}),
        ROOT / "clusters.yaml": yaml.safe_dump(config),
        ROOT / "runner-values.yaml": yaml.safe_dump(runner_values(), sort_keys=False),
        ROOT / "justfile": consumer,
        # Imported recipes activate mise from OSDC_ROOT (the consumer directory).
        ROOT / "mise.toml": (UPSTREAM / "mise.toml").read_text(),
    }
    for path, content in files.items():
        path.write_text(content)
    return files


def install() -> None:
    guard()
    logging.info("Guard passed; rendering local config into %s", ROOT)
    render()
    token = read_token(ROOT)
    # Check authorization before submitting the credential to the local cluster.
    os.environ["GH_TOKEN"] = token
    logging.info("Verifying the dedicated PAT has Administration access on %s", REPOSITORY)
    repo = api("", repository=REPOSITORY)
    if not repo.get("permissions", {}).get("admin"):
        raise ValueError("The dedicated PAT needs Administration read/write on kuring/ci-infra")
    logging.info("PAT verified; applying arc-runners base resources")

    def apply(description: str, *args: str, **kwargs) -> None:
        logging.info("Applied %s: %s", description, kube(*args, **kwargs).strip())

    apply(
        "namespace arc-runners",
        "apply",
        "-f",
        "-",
        input=json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "arc-runners"}}),
    )
    apply(
        "secret osdc-kind-github",
        "apply",
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "osdc-kind-github", "namespace": "arc-runners"},
                "data": {"github_token": base64.b64encode(token.encode()).decode()},
            }
        ),
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "spec": {
            "nodeSelector": {"node-fleet": "kind-local"},
            "containers": [
                {
                    "name": "$job",
                    "resources": {
                        "requests": {"cpu": "150m", "memory": "128Mi"},
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                    },
                }
            ],
        },
    }
    apply(
        "configmap osdc-kind-job-template",
        "apply",
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "osdc-kind-job-template", "namespace": "arc-runners"},
                "data": {"pod.yaml": yaml.safe_dump(pod)},
            }
        ),
    )
    apply(
        "capacity-monitor RBAC",
        "apply",
        "-f",
        str(UPSTREAM / "modules/arc/kubernetes/capacity-monitor-rbac.yaml"),
    )
    apply("priority classes", "apply", "-f", str(UPSTREAM / "modules/arc/kubernetes/priority-classes.yaml"))
    chart = ROOT / "charts/gha-runner-scale-set"
    if not chart.exists():
        logging.info("Pulling runner chart %s", VERSION)
        command(
            [
                "helm",
                "pull",
                "oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set",
                "--version",
                VERSION,
                "--untar",
                "--untardir",
                str(ROOT / "charts"),
            ],
            timeout=180,
        )
    if yaml.safe_load((chart / "Chart.yaml").read_text())["version"] != VERSION:
        raise ValueError("Unexpected cached ARC runner chart version")
    logging.info("Installing runner scale set with helm (waits for readiness, up to 5m)")
    command(
        [
            "helm",
            "upgrade",
            "--install",
            "osdc-kind-drain",
            str(chart),
            "--kubeconfig",
            str(ROOT / "kubeconfig"),
            "--kube-context",
            CONTEXT,
            "--namespace",
            "arc-runners",
            "-f",
            str(ROOT / "runner-values.yaml"),
            "--wait",
            "--timeout",
            "5m",
        ],
        timeout=330,
    )
    logging.info("Runner scale set osdc-kind-drain installed")


class KindDrainTest(DrainTest):
    def __init__(self, args, key):
        super().__init__(args, key)
        self.cluster = LOCAL_CLUSTER
        self.group = "default"
        self.repository = REPOSITORY
        self.branch = f"codex/osdc-drain-test-kind-{key}"
        self.env.update(
            {
                "KUBECONFIG": str(ROOT / "kubeconfig"),
                "MISE_TRUSTED_CONFIG_PATHS": os.pathsep.join(
                    filter(None, [os.environ.get("MISE_TRUSTED_CONFIG_PATHS"), str(ROOT)])
                ),
            }
        )

    def api(self, endpoint, payload=None, method="GET", **kwargs):
        return api(endpoint, payload, method, repository=self.repository, **kwargs)

    def kube(self, *args):
        return kube(*args)

    def prepare(self):
        guard()
        self.recipe("kubeconfig")
        self.desired = json.loads(
            command(
                ["uv", "run", str(UPSTREAM / "modules/arc-runners/scripts/python/runner_max_map.py"), self.cluster],
                cwd=ROOT,
                env=self.env,
            ).stdout
        )

    def check_probe(self, items):
        return probe_scale_set(items, self.desired, label=LABEL, cluster=self.cluster)

    def workflow(self):
        return generate_kind_workflow(self.run_key)

    def jobs(self):
        jobs = super().jobs()
        for job in jobs:
            if (job.get("runner_group_name") or "").lower() == "default":
                job["runner_group_name"] = "default"
        return jobs

    def run(self):
        if getattr(self.args, "action", "test") != "probe":
            super().run()
            return
        self.prepare()
        require_limits(runner_limits(self.scale_sets()), self.desired)
        wait_for("listener metrics available", lambda: bool(self.capacities()), self.args.drain_timeout)
        self.draining = True
        self.recipe("drain-runners")
        wait_for("zero runner limits and advertised capacity", self.drained, self.args.drain_timeout)
        self.resume()

        def restored():
            require_limits(runner_limits(self.scale_sets()), self.desired)
            if not any(self.capacities().values()):
                raise ValueError("Waiting for the local warm reservation to advertise capacity")
            return True

        wait_for("configured limits and local advertised capacity restored", restored, self.args.drain_timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "install", "test", "restore", "probe"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-timeout", type=positive, default=RUN_TIMEOUT)
    parser.add_argument("--cleanup-timeout", type=positive, default=CLEANUP_TIMEOUT)
    parser.add_argument("--restore-timeout", type=positive, default=RESTORE_TIMEOUT)
    parser.add_argument("--start-timeout", type=positive, default=600)
    parser.add_argument("--finish-timeout", type=positive, default=600)
    parser.add_argument("--drain-timeout", type=positive, default=180)
    parser.add_argument("--observe-seconds", type=positive, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.action == "render":
        for path, content in render().items():
            print(f"# {path}\n{content}")
        return
    if args.action == "install":
        install()
        return
    if args.dry_run:
        print(generate_kind_workflow(uuid4().hex))
        return
    render()
    if args.action == "test":
        os.environ["GH_TOKEN"] = read_token(ROOT)
    args.root_dir, args.upstream_dir, args.clusters_yaml = ROOT, UPSTREAM, ROOT / "clusters.yaml"
    run_controller(KindDrainTest(args, uuid4().hex), restore_only=args.action == "restore")


if __name__ == "__main__":
    main()
