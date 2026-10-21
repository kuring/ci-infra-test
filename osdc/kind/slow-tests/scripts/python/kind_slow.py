"""Local-only slow-test operations; invoke through the kind justfile."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
    NOT_COVERED,
    OWNER,
    REPO,
    TEST_IMAGE,
    WORKERS,
    adapt_manifests,
    kind_config,
    load_workflow,
    nodepool_documents,
    runner_values,
    validate_load_jobs,
    validate_nodes,
)

LOCAL = Path(__file__).resolve().parents[3]
OSDC = Path(os.environ.get("OSDC_UPSTREAM", LOCAL.parents[2] / "ci-infra/osdc")).resolve()
STATE = LOCAL / ".state" / "slow-tests"
KUBE = STATE / "kubeconfig"
NAMESPACE = "kind-slow-test"
RUNNER_IMAGE = "mirror.aone.alibaba-inc.com/mirrors/ghcr.io/actions/actions-runner:2.338.0"
SYSCTL_CONFIG = Path("/etc/sysctl.d/99-osdc-kind-slow.conf")


def command(args: list[str], *, input: str | None = None, env: dict | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(args, input=input, text=True, capture_output=True, env=env, cwd=cwd, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def kube(*args: str, input: str | None = None) -> str:
    return command(
        ["kubectl", "--kubeconfig", str(KUBE), "--context", CONTEXT, "--request-timeout=20s", *args], input=input
    )


def get(*args: str) -> dict:
    return json.loads(kube("get", *args, "-o", "json"))


def guard() -> None:
    context = command(["kubectl", "--kubeconfig", str(KUBE), "config", "current-context"]).strip()
    validate_nodes(context, get("nodes")["items"])


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def wait_for(description: str, check, timeout: float = 120, interval: float = 2):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out: {description}")
        time.sleep(interval)


def apply(documents: list[dict]) -> None:
    print(kube("apply", "-f", "-", input=yaml.safe_dump_all(documents, sort_keys=False)).strip())


def render() -> None:
    (STATE / "kind.yaml").write_text(yaml.safe_dump(kind_config(), sort_keys=False))
    for component, source in (
        ("compactor", OSDC / "base/node-compactor/kubernetes"),
        ("janitor", OSDC / "base/kubernetes/image-cache-janitor"),
    ):
        manifests = list(yaml.safe_load_all(command(["kubectl", "kustomize", str(source)])))
        documents = adapt_manifests(manifests, component)
        (STATE / f"{component}.yaml").write_text(yaml.safe_dump_all(documents, sort_keys=False))
    (STATE / "nodepools.yaml").write_text(yaml.safe_dump_all(nodepool_documents(), sort_keys=False))
    print(f"Rendered local-only manifests in {STATE}")


def host_limits(*, persistent: bool = False) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Host-limit preparation requires root on Linux")
    key = "fs.inotify.max_user_instances"
    current = int(command(["sysctl", "-n", key]).strip())
    target = max(current, 512)
    config = f"# OSDC local kind slow-test preparation\n{key} = {target}\n"
    if persistent and SYSCTL_CONFIG.exists() and SYSCTL_CONFIG.read_text() != config:
        raise ValueError("Refusing to overwrite an existing sysctl configuration")
    backup = STATE / "host-limits-before.json"
    if not backup.exists():
        write_json(backup, {key: current, "captured_at": datetime.now(UTC).isoformat()})
    if persistent and not SYSCTL_CONFIG.exists():
        with SYSCTL_CONFIG.open("x") as out:
            out.write(config)
        SYSCTL_CONFIG.chmod(0o644)
    print(command(["sysctl", "-w", f"{key}={target}"]).strip())


def create() -> None:
    render()
    clusters = command(["kind", "get", "clusters"]).splitlines()
    if CLUSTER not in clusters:
        # Follow the existing local proxy profile without copying loopback into nodes.
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if env.get(key):
                env[key] = (
                    env[key].replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
                )
        bypass = ",".join(
            [
                env.get("NO_PROXY", ""),
                env.get("no_proxy", ""),
                "localhost,127.0.0.1,host.docker.internal,.svc,.cluster.local,.alibaba-inc.com,.aliyun-inc.com",
            ]
        )
        env.update(NO_PROXY=bypass, no_proxy=bypass)
        print(
            command(
                [
                    "kind",
                    "create",
                    "cluster",
                    "--name",
                    CLUSTER,
                    "--config",
                    str(STATE / "kind.yaml"),
                    "--kubeconfig",
                    str(KUBE),
                    "--wait",
                    "180s",
                    "--retain",
                ],
                env=env,
            )
        )
    elif not KUBE.exists():
        command(["kind", "export", "kubeconfig", "--name", CLUSTER, "--kubeconfig", str(KUBE)])
    KUBE.chmod(0o600)
    kube("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s")
    guard()
    print(f"Ready: {CONTEXT}, {len(NODES)} fixed nodes")


def cache_uv_public() -> None:
    """Explicit opt-in for the official same-version uv archive; preload validates it."""
    guard()
    architecture = get("node", WORKERS[0])["metadata"]["labels"]["kubernetes.io/arch"]
    archive = STATE / "uv-image.tar"
    if not archive.exists():
        temporary = archive.with_suffix(".download")
        command(
            ["crane", "pull", "ghcr.io/astral-sh/uv:0.10.12", str(temporary), "--platform", f"linux/{architecture}"]
        )
        temporary.replace(archive)
    preload()


def preload() -> None:
    guard()
    architecture = get("node", WORKERS[0])["metadata"]["labels"]["kubernetes.io/arch"]
    mirror_uv = "mirror.aone.alibaba-inc.com/mirrors/ghcr.io/astral-sh/uv:0.10.12"
    images = [
        ("python:3.12.9-slim", "python-image.tar", {"python:3.12.9-slim"}),
        (mirror_uv, "uv-image.tar", {mirror_uv, "ghcr.io/astral-sh/uv:0.10.12"}),
        (
            "public.ecr.aws/docker/library/python:3.12-alpine",
            "janitor-base-image.tar",
            {"public.ecr.aws/docker/library/python:3.12-alpine"},
        ),
        (TEST_IMAGE, "alpine-image.tar", {TEST_IMAGE}),
    ]
    for image, filename, allowed in images:
        try:
            cached_arch = command(["docker", "image", "inspect", "--format", "{{.Architecture}}", image]).strip()
        except RuntimeError:
            cached_arch = ""
        if cached_arch == architecture:
            continue
        archive = STATE / filename
        if not archive.exists():
            print(f"Host client downloading {image}", flush=True)
            command(["crane", "pull", image, str(archive), "--platform", f"linux/{architecture}"])
        with tarfile.open(archive) as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            if len(manifest) != 1 or not set(manifest[0]["RepoTags"]) <= allowed:
                raise ValueError(f"Cached archive must contain only the expected image: {archive}")
            config = json.load(tar.extractfile(manifest[0]["Config"]))
        source = next((tag for tag in manifest[0]["RepoTags"] if tag in allowed), None)
        if source is None or config["architecture"] != architecture:
            raise ValueError(f"Cached archive tag or architecture mismatch: {archive}")
        print(f"Importing {source} as local cache for {image}", flush=True)
        print(command(["docker", "load", "--input", str(archive)]), flush=True)
        if source != image:
            command(["docker", "tag", source, image])


def deploy() -> None:
    guard()
    render()
    preload()
    architecture = get("node", WORKERS[0])["metadata"]["labels"]["kubernetes.io/arch"]
    source = OSDC / "base/node-compactor"
    with tempfile.TemporaryDirectory(dir=STATE, prefix="build-") as tmp:
        build = Path(tmp)
        for file in (source / "docker").iterdir():
            if file.is_file():
                shutil.copyfile(file, build / file.name)
        shutil.copyfile(LOCAL / "slow-tests/docker/node-compactor.Dockerfile", build / "Dockerfile")
        for file in (source / "scripts/python").glob("*.py"):
            if not file.name.startswith("test_"):
                shutil.copyfile(file, build / file.name)
        # Private state uses umask 077; the container runs as uid 65534.
        for file in build.iterdir():
            file.chmod(0o644)
        print(command(["docker", "build", "--platform", f"linux/{architecture}", "-t", COMPACTOR_IMAGE, str(build)]))
    print(
        command(
            [
                "docker",
                "build",
                "--platform",
                f"linux/{architecture}",
                "--build-arg",
                f"TARGETARCH={architecture}",
                "-t",
                JANITOR_IMAGE,
                str(OSDC / "base/kubernetes/image-cache-janitor/docker"),
            ]
        )
    )
    command(["docker", "pull", "--platform", f"linux/{architecture}", TEST_IMAGE])
    print(command(["kind", "load", "docker-image", "--name", CLUSTER, COMPACTOR_IMAGE, JANITOR_IMAGE, TEST_IMAGE]))
    crd, pool = nodepool_documents()
    apply([crd])
    kube("wait", "--for=condition=Established", "crd/nodepools.karpenter.sh", "--timeout=60s")
    apply([pool])
    for component in ("compactor", "janitor"):
        kube("apply", "-f", str(STATE / f"{component}.yaml"))
    # Rebuilds retain local image tags: restart to consume the loaded image.
    kube("rollout", "restart", "deployment/node-compactor", "-n", "kube-system")
    kube("rollout", "restart", "daemonset/image-cache-janitor", "-n", "kube-system")
    kube("rollout", "status", "deployment/node-compactor", "-n", "kube-system", "--timeout=180s")
    kube("rollout", "status", "daemonset/image-cache-janitor", "-n", "kube-system", "--timeout=180s")
    print("Deployed real compactor (dry-run at rest) and janitor (eviction disabled at rest)")


def gh_env() -> dict:
    env = os.environ.copy()
    if not env.get("GH_TOKEN"):
        token = LOCAL / ".state/github-token"
        if not token.is_file() or token.is_symlink() or token.stat().st_mode & 0o077:
            raise ValueError("Set GH_TOKEN or provide .state/github-token with permissions 600")
        env["GH_TOKEN"] = token.read_text().strip()
    if not env["GH_TOKEN"]:
        raise ValueError("Empty GitHub token")
    return env


def api(path: str, payload: dict | None = None, *, method: str = "GET"):
    endpoint = f"repos/{REPO}" + (f"/{path}" if path else "")
    args = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        args += ["--input", "-"]
    output = command(args, input=json.dumps(payload) if payload is not None else None, env=gh_env())
    return json.loads(output) if output.strip() else None


def install_arc() -> None:
    guard()
    if not api("").get("permissions", {}).get("admin"):
        raise ValueError(f"Token must administer the runner registration in {REPO}")
    env = gh_env()
    apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "arc-runners"}}])
    apply(
        [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "osdc-slow-github", "namespace": "arc-runners"},
                "data": {"github_token": base64.b64encode(env["GH_TOKEN"].encode()).decode()},
            }
        ]
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "spec": {
            "nodeSelector": {"node-fleet": "kind-slow"},
            "containers": [
                {
                    "name": "$job",
                    "resources": {
                        "requests": {"cpu": "150m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            ],
        },
    }
    apply(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "osdc-slow-job-template", "namespace": "arc-runners"},
                "data": {"pod.yaml": yaml.safe_dump(pod)},
            }
        ]
    )
    for filename in ("capacity-monitor-rbac.yaml", "priority-classes.yaml"):
        kube("apply", "-f", str(OSDC / "modules/arc/kubernetes" / filename))
    controller = yaml.safe_load((LOCAL / "helm/controller.yaml").read_text())
    version = controller["image"]["tag"]
    runner_image = RUNNER_IMAGE
    if os.environ.get("OSDC_KIND_ARC_PUBLIC_IMAGES") == "true":
        controller["image"]["repository"] = "ghcr.io/jeanschmidt/gha-runner-scale-set-controller"
        runner_image = "ghcr.io/actions/actions-runner:2.338.0"
    proxy_path = LOCAL / ".state/proxy.yaml"
    proxy = yaml.safe_load(proxy_path.read_text())["proxy"] if proxy_path.exists() else None
    if proxy:
        controller["proxy"] = proxy
    (STATE / "controller-values.yaml").write_text(yaml.safe_dump(controller, sort_keys=False))
    values = runner_values(runner_image, proxy)
    (STATE / "runner-values.yaml").write_text(yaml.safe_dump(values, sort_keys=False))
    for chart_name, release, namespace, values_path in (
        ("gha-runner-scale-set-controller", "arc", "arc-systems", "controller-values.yaml"),
        ("gha-runner-scale-set", LABEL, "arc-runners", "runner-values.yaml"),
    ):
        chart = LOCAL / ".state/charts" / chart_name
        if not (chart / "Chart.yaml").exists():
            command(
                [
                    "helm",
                    "pull",
                    f"oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/{chart_name}",
                    "--version",
                    version,
                    "--untar",
                    "--untardir",
                    str(chart.parent),
                ]
            )
        if yaml.safe_load((chart / "Chart.yaml").read_text())["version"] != version:
            raise ValueError("Cached ARC chart version differs from the local controller profile")
        print(
            command(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    release,
                    str(chart),
                    "--namespace",
                    namespace,
                    "--create-namespace",
                    "--kubeconfig",
                    str(KUBE),
                    "--kube-context",
                    CONTEXT,
                    "-f",
                    str(STATE / values_path),
                    "--wait",
                    "--timeout",
                    "5m",
                ]
            )
        )
    selector = f"actions.github.com/scale-set-name={LABEL},app.kubernetes.io/component=runner-scale-set-listener"
    wait_for(
        "one owned ARC listener pod",
        lambda: len(get("pods", "-n", "arc-systems", "-l", selector)["items"]) == 1,
        timeout=180,
    )
    kube(
        "wait",
        "--for=condition=Ready",
        "pod",
        "-n",
        "arc-systems",
        "-l",
        selector,
        "--timeout=180s",
    )


def restore(*, reload_janitor: bool = False) -> None:
    guard()
    kube("set", "env", "deployment/node-compactor", "-n", "kube-system", *[f"{k}={v}" for k, v in BASE_ENV.items()])
    kube("scale", "deployment/node-compactor", "-n", "kube-system", "--replicas=1")
    kube("rollout", "status", "deployment/node-compactor", "-n", "kube-system", "--timeout=180s")
    kube(
        "set",
        "env",
        "daemonset/image-cache-janitor",
        "-n",
        "kube-system",
        *[f"{k}={v}" for k, v in JANITOR_ENV.items()],
    )
    if reload_janitor:
        # containerd 2.x may remove tags of in-use images. Stop all old GC
        # pods before reimporting locally built images, then resume safely.
        def select(value):
            kube(
                "patch",
                "daemonset/image-cache-janitor",
                "-n",
                "kube-system",
                "--type=merge",
                "-p",
                json.dumps({"spec": {"template": {"spec": {"nodeSelector": {OWNER: value}}}}}),
            )

        select("false")
        try:
            wait_for(
                "old janitor pods stopped before local image reimport",
                lambda: not get("pods", "-n", "kube-system", "-l", "app=image-cache-janitor")["items"],
                timeout=180,
            )
            command(["kind", "load", "docker-image", "--name", CLUSTER, JANITOR_IMAGE, TEST_IMAGE])
        finally:
            select("true")
    kube("rollout", "status", "daemonset/image-cache-janitor", "-n", "kube-system", "--timeout=180s")


@contextlib.contextmanager
def paused_arc():
    """Match upstream compactor isolation, but pause only our scale set."""
    guard()
    scales = get("autoscalingrunnersets", "-n", "arc-runners")["items"]
    owned = [s for s in scales if s["metadata"]["name"] == LABEL]
    if not owned:
        yield
        return
    original = {key: owned[0]["spec"].get(key) for key in ("minRunners", "maxRunners")}
    write_json(STATE / "arc-pause-before.json", original)

    def patch(values):
        kube(
            "patch",
            "autoscalingrunnerset",
            LABEL,
            "-n",
            "arc-runners",
            "--type=merge",
            "-p",
            json.dumps({"spec": values}),
        )

    try:
        patch({"minRunners": 0, "maxRunners": 0})
        prefixes = (f"{LABEL}-", f"ph-r-{LABEL}-", f"ph-w-{LABEL}-")
        wait_for(
            "owned ARC runners and placeholders drained",
            lambda: (
                not any(p["metadata"]["name"].startswith(prefixes) for p in get("pods", "-n", "arc-runners")["items"])
            ),
            timeout=180,
        )
        yield
    finally:
        patch(original)


def pytest_suite(component: str) -> None:
    guard()
    env = os.environ.copy()
    env.update(KUBECONFIG=str(KUBE), OSDC_KIND_SLOW_KUBECONFIG=str(KUBE), OSDC_ROOT=str(OSDC), OSDC_UPSTREAM=str(OSDC))
    if component == "compactor":
        directory = LOCAL / "slow-tests/tests/e2e"
        args = ["uv", "run", "--frozen", "pytest", "test_compactor_kind.py", "-v", "-x", "--tb=long", "-rA"]
    else:
        directory = OSDC / "base/kubernetes/image-cache-janitor/tests/e2e"
        env["PYTHONPATH"] = os.pathsep.join([str(LOCAL / "slow-tests/scripts/python"), env.get("PYTHONPATH", "")])
        args = [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "-p",
            "kind_slow_janitor",
            "test_e2e.py",
            "-v",
            "-x",
            "--tb=long",
            "-rA",
            f"--cluster-id={CLUSTER}",
        ]
    with paused_arc() if component == "compactor" else contextlib.nullcontext():
        try:
            result = subprocess.run(args, env=env, cwd=directory, text=True, capture_output=True, check=False)
            log = STATE / f"{component}-e2e.log"
            log.write_text(result.stdout + result.stderr)
            print(result.stdout + result.stderr)
            if result.returncode or " skipped" in result.stdout or "no tests ran" in result.stdout:
                raise RuntimeError(f"{component} did not pass completely; see {log}")
        finally:
            restore(reload_janitor=component == "janitor")


def prepare_pr(run_key: str, workflow: str, base: str, record: dict) -> int:
    head = api(f"git/ref/heads/{base}")["object"]["sha"]
    tree = api(f"git/commits/{head}")["tree"]["sha"]
    github_entry = next((e for e in api(f"git/trees/{tree}")["tree"] if e["path"] == ".github"), None)
    workflow_tree = api(
        "git/trees",
        {"tree": [{"path": "kind-load.yaml", "mode": "100644", "type": "blob", "content": workflow}]},
        method="POST",
    )["sha"]
    github_payload = {"tree": [{"path": "workflows", "mode": "040000", "type": "tree", "sha": workflow_tree}]}
    if github_entry:
        github_payload["base_tree"] = github_entry["sha"]
    github_tree = api("git/trees", github_payload, method="POST")["sha"]
    root_tree = api(
        "git/trees",
        {"base_tree": tree, "tree": [{"path": ".github", "mode": "040000", "type": "tree", "sha": github_tree}]},
        method="POST",
    )["sha"]
    commit = api(
        "git/commits",
        {"message": f"[NO REVIEW][NO MERGE] OSDC kind load {run_key}", "tree": root_tree, "parents": [head]},
        method="POST",
    )["sha"]
    record["branch"] = f"osdc-kind-load/{run_key}"
    write_json(STATE / "github-run.json", record)
    api("git/refs", {"ref": f"refs/heads/{record['branch']}", "sha": commit}, method="POST")
    pr = api(
        "pulls",
        {
            "title": f"[NO REVIEW][NO MERGE] OSDC kind load {run_key}",
            "head": record["branch"],
            "base": base,
            "body": "Temporary local kind slow-test run. Do not review or merge.",
        },
        method="POST",
    )
    record.update(pr=pr["number"], url=pr["html_url"])
    write_json(STATE / "github-run.json", record)
    print(f"Temporary load-test PR: {pr['html_url']}")
    return pr["number"]


def local_runner_names() -> set[str]:
    names = {
        p["metadata"]["name"]
        for p in get("pods", "-n", "arc-runners")["items"]
        if p.get("spec", {}).get("nodeName") in WORKERS and any(c["name"] == "runner" for c in p["spec"]["containers"])
    }
    for event in get("events", "-n", "arc-runners")["items"]:
        obj = event.get("involvedObject", {})
        if (
            obj.get("kind") == "Pod"
            and obj.get("name", "").startswith(f"{LABEL}-")
            and event.get("reason") == "Scheduled"
            and any(event.get("message", "").endswith(f"to {worker}") for worker in WORKERS)
        ):
            names.add(obj["name"])
    return names


def cleanup_github(record: dict) -> None:
    if record.get("repo") != REPO or not record.get("run_key"):
        raise ValueError("Recovery record does not belong to this local profile")
    errors = []
    for run_id in record.get("run_ids", []):
        try:
            run = api(f"actions/runs/{run_id}")
            if run["status"] != "completed":
                api(f"actions/runs/{run_id}/cancel", method="POST")
                wait_for(
                    "GitHub run cancellation",
                    lambda run_id=run_id: api(f"actions/runs/{run_id}")["status"] == "completed",
                    timeout=60,
                )
        except (RuntimeError, TimeoutError) as exc:
            errors.append(str(exc))
    if record.get("branch") and not record.get("pr"):
        try:
            prs = api(f"pulls?state=open&head={REPO.split('/')[0]}:{record['branch']}&per_page=100")
            for pr in prs:
                if pr["head"]["ref"] == record["branch"] and record["run_key"] in pr["title"]:
                    record["pr"] = pr["number"]
        except RuntimeError as exc:
            errors.append(str(exc))
    if record.get("pr"):
        try:
            api(f"pulls/{record['pr']}", {"state": "closed"}, method="PATCH")
        except RuntimeError as exc:
            errors.append(str(exc))
    if record.get("branch"):
        try:
            api(f"git/refs/heads/{record['branch']}", method="DELETE")
        except RuntimeError as exc:
            # A failed create or already-completed cleanup must be idempotent.
            if "HTTP 404" not in str(exc):
                errors.append(str(exc))
    record["cleanup_complete"] = not errors
    write_json(STATE / "github-run.json", record)
    if errors:
        raise RuntimeError("GitHub cleanup failed: " + "; ".join(errors))


def load_test(jobs: int, timeout: int, dry_run: bool) -> None:
    guard()
    architecture = get("node", WORKERS[0])["metadata"]["labels"]["kubernetes.io/arch"]
    architecture = {"arm64": "aarch64", "amd64": "x86_64"}[architecture]
    base = "main" if dry_run else api("")["default_branch"]
    key = uuid.uuid4().hex
    workflow = yaml.safe_dump(load_workflow(key, jobs, architecture, RUNNER_IMAGE, base), sort_keys=False)
    if dry_run:
        print(workflow)
        return
    if (STATE / "github-run.json").exists():
        previous = json.loads((STATE / "github-run.json").read_text())
        if not previous.get("cleanup_complete"):
            raise ValueError("Previous GitHub run requires 'just kind-slow-cleanup-github'")
    restore()
    scales = get("autoscalingrunnersets", "-n", "arc-runners")["items"]
    scale = [s for s in scales if s["spec"].get("runnerScaleSetName") == LABEL]
    if len(scale) != 1 or scale[0]["spec"].get("maxRunners") != 3:
        raise ValueError("Install the dedicated local ARC scale set before load testing")
    # Match the installed runner profile, including the ECS public-image override.
    image = next(c["image"] for c in scale[0]["spec"]["template"]["spec"]["containers"] if c["name"] == "runner")
    workflow = yaml.safe_dump(load_workflow(key, jobs, architecture, image, base), sort_keys=False)
    record = {
        "repo": REPO,
        "run_key": key,
        "run_ids": [],
        "cleanup_complete": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    write_json(STATE / "github-run.json", record)
    try:
        prepare_pr(key, workflow, base, record)
        deadline = time.monotonic() + timeout
        names = set()
        while time.monotonic() < deadline:
            names.update(local_runner_names())
            runs = [
                r
                for r in api(f"actions/runs?branch={record['branch']}&per_page=100")["workflow_runs"]
                if r["name"] == f"OSDC Kind Load {key}"
            ]
            if len(runs) > 1:
                raise ValueError("Unexpected duplicate load workflow runs")
            if runs:
                run = runs[0]
                record["run_ids"] = [run["id"]]
                write_json(STATE / "github-run.json", record)
                if run["status"] == "completed":
                    details = api(f"actions/runs/{run['id']}/jobs?per_page=100")
                    record.update(jobs=details["jobs"], local_runners=sorted(names), run_url=run["html_url"])
                    write_json(STATE / "github-run.json", record)
                    if run["conclusion"] != "success":
                        raise ValueError(f"Load workflow failed: {run['html_url']}")
                    validate_load_jobs(details["jobs"], jobs, names)
                    print(f"PASS: {jobs} real Actions jobs on local kind runners: {run['html_url']}")
                    return
            time.sleep(3)
        raise TimeoutError("Real GitHub load test timed out")
    finally:
        # Discover runs even if interruption occurred before the first polling cycle.
        if record.get("branch"):
            with contextlib.suppress(RuntimeError):
                record["run_ids"] = [
                    r["id"] for r in api(f"actions/runs?branch={record['branch']}&per_page=100")["workflow_runs"]
                ]
        cleanup_github(record)


def run_tests(args: argparse.Namespace) -> None:
    components = ["load", "compactor", "janitor"] if args.action == "test" else [args.action]
    report = {
        "cluster": CLUSTER,
        "not_covered": NOT_COVERED,
        "results": dict.fromkeys(components, "not_run"),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        for component in components:
            report["results"][component] = "running"
            write_json(STATE / "report.json", report)
            try:
                if component == "load":
                    load_test(args.jobs, args.timeout, args.dry_run)
                else:
                    pytest_suite(component)
            except BaseException:
                report["results"][component] = "failed"
                raise
            report["results"][component] = "preview" if args.dry_run else "passed"
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(STATE / "report.json", report)
        print(f"Report: {STATE / 'report.json'}")


def main() -> None:  # pragma: no cover - executable CLI, tested through recipes and live runs
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "render",
            "create",
            "guard",
            "deploy",
            "install-arc",
            "load",
            "compactor",
            "janitor",
            "test",
            "restore",
            "cleanup-github",
            "preload",
            "cache-uv-public",
            "host-limits",
        ],
    )
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=1800, help="GitHub load-test deadline in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Preview the load workflow without GitHub writes")
    parser.add_argument(
        "--reload-janitor", action="store_true", help="Stop janitor GC and reimport local images during restore"
    )
    parser.add_argument(
        "--persistent", action="store_true", help="Persist the explicitly requested Linux host-limit change"
    )
    args = parser.parse_args()
    if not 1 <= args.jobs <= 100 or args.timeout <= 0:
        parser.error("jobs must be 1..100 and timeout must be positive")
    if args.dry_run and args.action != "load":
        parser.error("--dry-run is supported only by the load action")
    if args.persistent and args.action != "host-limits":
        parser.error("--persistent is supported only by host-limits")
    if args.reload_janitor and args.action != "restore":
        parser.error("--reload-janitor is supported only by restore")
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    os.umask(0o077)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if args.action == "host-limits":
        with (STATE / "host-limits.lock").open("w") as host_lock:
            fcntl.flock(host_lock, fcntl.LOCK_EX)
            host_limits(persistent=args.persistent)
        return
    with (STATE / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another local slow-test operation is running")
        actions = {
            "render": render,
            "create": create,
            "guard": guard,
            "deploy": deploy,
            "preload": preload,
            "cache-uv-public": cache_uv_public,
            "host-limits": lambda: host_limits(persistent=args.persistent),
            "install-arc": install_arc,
            "restore": lambda: restore(reload_janitor=args.reload_janitor),
            "cleanup-github": lambda: cleanup_github(json.loads((STATE / "github-run.json").read_text())),
        }
        if args.action in actions:
            actions[args.action]()
        else:
            run_tests(args)


if __name__ == "__main__":
    main()
