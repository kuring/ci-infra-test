"""Cancel a real GitHub workflow; verify same-job recovery and concurrency release."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path
from uuid import uuid4

import drain_test_kind as kind
import yaml
from drain_recovery_kind import RecoveryKind, controller_args, require, save, verify_restored
from drain_test_checks import require_limits, runner_limits
from drain_test_kind import wait_for
from drain_test_run import WORKFLOW_NAME_PREFIX, run_controller
from service_runner import NativeRunner, runner_archive


def stamp(directory, name, **fields):
    save(
        directory / (name + ".json"), {"pid": os.getpid(), "time": time.time(), "monotonic": time.monotonic(), **fields}
    )


def read(directory, name):
    return json.loads((directory / (name + ".json")).read_text())


def formal_job():
    return yaml.safe_load((kind.UPSTREAM.parent / ".github/workflows/_osdc-slow-tests.yml").read_text())["jobs"][
        "drain-test"
    ]


def generate_workflow(key, output, runners):
    source = formal_job()
    job = {k: copy.deepcopy(source[k]) for k in ("if", "timeout-minutes", "concurrency")}
    job["name"] = "Cancel drain and recover"
    job["runs-on"] = [runners[0]]
    job["concurrency"]["group"] = "osdc-service-cancel-" + key
    # The local worker's guard replaces the staging input boundary; a PR event
    # has no reusable-workflow inputs.cluster value.
    job["if"] = job["if"].replace("inputs.cluster == 'meta-staging-aws-uw1' && ", "")
    job["if"] = job["if"].replace(" }}", " && github.event.action == 'opened' }}")
    prefix = " ".join(
        shlex.quote(str(p)) for p in ["just", "--justfile", kind.LOCAL / "justfile", "kind-drain-service-step"]
    )

    def run(action):
        return prefix + " " + action + " --output " + shlex.quote(str(output)) + " --key " + key

    prepare = {
        "name": "Check dedicated Kind connection",
        "id": "aws",
        "if": "${{ success() && !cancelled() }}",
        "run": run("prepare"),
    }
    drain = copy.deepcopy(next(s for s in source["steps"] if s.get("id") == "drain"))
    recovery_file = re.search(r'--recovery-file "([^"]+)"', drain["run"])
    if recovery_file is None:
        raise ValueError("Formal drain step is missing its recovery-file argument")
    recovery_path = recovery_file.group(1)
    drain["run"] = run("cancel")
    # runner.temp is only available in step env, as in the formal workflow.
    # The worker reads the owner-only PAT locally instead of a YAML secret.
    drain["env"].pop("GH_TOKEN")
    drain["env"]["OSDC_DRAIN_RECOVERY_FILE"] = recovery_path
    restore = copy.deepcopy(next(s for s in source["steps"] if s.get("id") == "restore"))
    restore["run"] = run("restore")
    restore.setdefault("env", {})["OSDC_DRAIN_RECOVERY_FILE"] = recovery_path
    job["steps"] = [prepare, drain, restore]
    successor = {
        "name": "Acquire released lock",
        "if": "${{ !cancelled() && github.event.action == 'synchronize' }}",
        "runs-on": [runners[1]],
        "timeout-minutes": 5,
        "concurrency": copy.deepcopy(job["concurrency"]),
        "steps": [{"name": "Verify recovery precedes lock release", "run": run("successor")}],
    }
    return yaml.safe_dump(
        {
            "name": WORKFLOW_NAME_PREFIX + "service" + key,
            "on": {"pull_request": {"types": ["opened", "synchronize"]}},
            "permissions": {"contents": "read"},
            "jobs": {"drain": job, "successor": successor},
        },
        sort_keys=False,
    )


class ServiceRecovery(RecoveryKind):
    def record_recovery(self):
        owner = super().record_recovery()
        # Preserve the non-secret ownership intent before runner.temp is cleaned.
        save(self.directory / "recovery-intent.json", json.loads(self.recovery_file.read_text()))
        return owner


def worker(action, output, key):
    args = controller_args(output, defer=action == "cancel")
    args.recovery_file = Path(os.environ["OSDC_DRAIN_RECOVERY_FILE"]) if action in {"cancel", "restore"} else None
    test = ServiceRecovery(args, key, output, checkpoint=action == "cancel")
    if action == "prepare":
        test.prepare()
        stamp(output, "prepared", runner=os.environ.get("RUNNER_NAME"), limits=runner_limits(test.scale_sets()))
    elif action == "cancel":
        os.environ["GH_TOKEN"] = kind.read_token(kind.ROOT)
        stamp(output, "controller-start", runner=os.environ.get("RUNNER_NAME"), recovery_file=str(args.recovery_file))
        try:
            run_controller(test)
        except SystemExit as exc:
            stamp(output, "controller-exit", exit_code=exc.code)
            raise
    elif action == "restore":
        stamp(output, "restore-entered")
        require(args.recovery_file.is_file(), "Recovery handoff file is missing")
        run_controller(test, restore_only=True)
        wait_for(
            "positive advertised capacity after CI cancellation", lambda: test.capacities().get(kind.LABEL, 0) > 0, 120
        )
        require(
            not test.recorded_drain_groups(json.loads(args.recovery_file.read_text())["token"]),
            "Writer survived CI recovery",
        )
        stamp(output, "restore-capacity", limits=runner_limits(test.scale_sets()), capacities=test.capacities())
        # Hold the restored job briefly, to independently test its concurrency lock.
        time.sleep(30)
        stamp(output, "restore-finished")
        print("SERVICE_RESTORE_COMPLETED")
    else:
        stamp(output, "successor-start", runner=os.environ.get("RUNNER_NAME"))
        require((output / "restore-finished.json").is_file(), "Concurrency lock released before recovery finished")
        test.prepare()
        verify_restored(test, test.desired)
        stamp(output, "successor-finished")


class ServiceCanary(kind.KindDrainTest):
    def __init__(self, args, key, output, runners):
        super().__init__(args, key)
        self.branch = "codex/osdc-service-cancel-" + key
        self.workflow_name = WORKFLOW_NAME_PREFIX + "service" + key
        self.output, self.runners = output, runners

    def workflow(self):
        return generate_workflow(self.run_key, self.output, self.runners)


def jobs(test, run_id):
    return test.api(f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]


def queue_successor(test):
    head = test.api("git/ref/heads/" + test.branch)["object"]["sha"]
    tree = test.api("git/commits/" + head)["tree"]["sha"]
    tree = test.api(
        "git/trees",
        {
            "base_tree": tree,
            "tree": [{"path": ".osdc-service-successor", "mode": "100644", "type": "blob", "content": test.run_key}],
        },
        "POST",
    )["sha"]
    commit = test.api(
        "git/commits", {"message": "Queue cancellation lock successor", "tree": tree, "parents": [head]}, "POST"
    )["sha"]
    test.api("git/refs/heads/" + test.branch, {"sha": commit}, "PATCH")
    result = []

    def found():
        result[:] = [r for r in test.runs() if r["id"] != test.run_id]
        return bool(result)

    wait_for("successor workflow queued", found, 120, poll=3)
    require(len(result) == 1, "Unexpected additional service workflow")
    return result[0]["id"]


def exercise(test, output, runners):
    test.create_pr()
    wait_for("service workflow created", test.find_run, 120, poll=3)
    original = test.run_id

    def drained():
        state = test.api(f"actions/runs/{original}")
        if state["status"] == "completed":
            raise RuntimeError("Service workflow ended before drained checkpoint")
        return (output / "drained.json").exists()

    wait_for("actual CI job drained Kind", drained, 1000, poll=3)
    require_limits(runner_limits(test.scale_sets()), {kind.LABEL: 0})
    require(all(v == 0 for v in test.capacities().values()), "Kind still advertises nonzero capacity")
    successor = queue_successor(test)
    for _ in range(3):
        available = runners[1].registrations()
        require(
            len(available) == 1 and available[0]["status"] == "online" and not available[0]["busy"],
            "Successor runner is unavailable; cannot attribute queueing to concurrency",
        )
        require(not (output / "successor-start.json").exists(), "Successor escaped the held lock")
        time.sleep(3)
    stamp(output, "cancel-request", run_id=original, successor_run=successor)
    test.api(
        f"actions/runs/{original}/cancel", method="POST"
    )  # Standard server cancellation; never send local signals.
    samples = []

    def finished():
        state = test.api(f"actions/runs/{original}")
        follow = jobs(test, successor)
        samples.append(
            {
                "time": time.time(),
                "original_status": state["status"],
                "jobs": follow,
                "restore_entered": (output / "restore-entered.json").exists(),
                "restore_finished": (output / "restore-finished.json").exists(),
            }
        )
        if not (output / "restore-finished.json").exists():
            require(not (output / "successor-start.json").exists(), "Successor ran before restore completed")
        return state["status"] == "completed"

    try:
        wait_for("cancelled workflow reached terminal state", finished, 360, poll=3)
    finally:
        save(output / "schedule-samples.json", samples)
    require(test.api(f"actions/runs/{original}")["conclusion"] == "cancelled", "Original workflow was not cancelled")
    original_jobs = jobs(test, original)
    target = next(j for j in original_jobs if j["name"] == "Cancel drain and recover")
    steps = {s["name"]: s for s in target["steps"]}
    restore_name = next(s["name"] for s in formal_job()["steps"] if s.get("id") == "restore")
    drain_name = next(s["name"] for s in formal_job()["steps"] if s.get("id") == "drain")
    require(steps[drain_name]["conclusion"] == "cancelled", "Drain step did not receive server cancellation")
    require(steps[restore_name]["conclusion"] == "success", "always() recovery step did not succeed")
    require((output / "restore-finished.json").exists(), "Recovery did not complete")
    wait_for(
        "successor acquired released lock",
        lambda: test.api(f"actions/runs/{successor}")["status"] == "completed",
        180,
        poll=3,
    )
    require(test.api(f"actions/runs/{successor}")["conclusion"] == "success", "Lock successor failed")
    started, ended = read(output, "successor-start"), read(output, "restore-finished")
    require(started["monotonic"] >= ended["monotonic"], "Successor started before restore finished")
    require(
        any(s["restore_entered"] and not s["restore_finished"] for s in samples),
        "Recovery lock window was not observed",
    )
    require(target["runner_name"] == runners[0].name, "Controller used an unexpected runner")
    require(started["runner"] == runners[1].name, "Successor used an unexpected runner")
    verify_restored(test, {kind.LABEL: 3})
    save(
        output / "acceptance.json",
        {
            "status": "passed",
            "original_run": original,
            "successor_run": successor,
            "original_jobs": original_jobs,
            "successor_jobs": jobs(test, successor),
            "restore_before_successor": True,
            "server_cancel_only": True,
        },
    )


def run_suite(output):
    require(sys.platform == "linux" and os.uname().machine == "x86_64", "Requires the dedicated Linux x64 Kind host")
    kind.guard()
    kind.render()
    os.environ["GH_TOKEN"] = kind.read_token(kind.ROOT)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = uuid4().hex
    names = ["osdc-service-" + key + suffix for suffix in ("-drain", "-next")]
    test = ServiceCanary(controller_args(output, recovery=False), key, output, names)
    test.prepare()
    verify_restored(test, test.desired)
    runners = [NativeRunner(test, name, output / name) for name in names]
    archive, application = runner_archive(
        test, Path(os.environ.get("OSDC_SERVICE_RUNNER_CACHE", kind.ROOT / "service-runner-cache"))
    )
    save(output / "runner-application.json", application)
    baseline = test.scale_sets()
    save(output / "baseline.json", baseline)
    passed = False
    cleanup_errors = []

    def teardown(name, action):
        try:
            action()
        except Exception as exc:
            logging.exception("Cleanup failed: %s", name)
            cleanup_errors.append(f"{name}: {exc}")

    try:
        for runner in runners:
            runner.start(archive)
        save(output / "workflow.yaml.json", {"yaml": test.workflow()})
        exercise(test, output, runners)
        passed = True
    finally:
        # Capture the service result before any external fallback recovery.
        teardown(
            "capture observed state",
            lambda: save(
                output / "observed-before-fallback.json",
                {
                    "passed": passed,
                    "limits": runner_limits(test.scale_sets()),
                    "capacities": test.capacities(),
                    "restore_entered": (output / "restore-entered.json").exists(),
                    "restore_finished": (output / "restore-finished.json").exists(),
                },
            ),
        )

        def cancel_owned():
            for run in test.runs():
                if run["status"] != "completed":
                    test.cancel_run(run)
            wait_for(
                "service workflows stopped before fallback/runner cleanup",
                lambda: all(r["status"] == "completed" for r in test.runs()),
                360,
                poll=5,
            )

        teardown("cancel owned service runs", cancel_owned)
        for runner in runners:
            teardown("stop " + runner.name, runner.stop)
        # External fallback is teardown only and never makes the acceptance pass.
        recovery = read(output, "controller-start") if (output / "controller-start.json").exists() else None
        if recovery and not passed:
            recovery_file = Path(recovery["recovery_file"])
            if not recovery_file.exists():
                recovery_file = output / "recovery-intent.json"
            if recovery_file.exists():
                args = controller_args(output)
                args.recovery_file = recovery_file
                teardown(
                    "external fallback recovery",
                    lambda: run_controller(RecoveryKind(args, key, output), restore_only=True),
                )
        cleanup = RecoveryKind(controller_args(output, recovery=False), key, output)
        teardown("probe canary cleanup", cleanup.cleanup_stale_canary)
        teardown("service canary cleanup", lambda: require(test.cleanup(), "service canary cleanup failed"))
        save(
            output / "cleanup.json",
            {"errors": cleanup_errors, "runners_absent": all(not r.registrations() for r in runners)},
        )
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
    require(passed, "Server cancellation acceptance failed")
    verify_restored(test, test.desired)
    wait_for(
        "probe runs completed after service cancellation",
        lambda: all(
            r["status"] == "completed"
            for r in cleanup.api(f"actions/runs?event=pull_request&branch={cleanup.branch}&per_page=100")[
                "workflow_runs"
            ]
        ),
        180,
    )

    def pods_gone():
        return not any(
            p["metadata"]["name"].startswith(kind.LABEL + "-")
            for p in json.loads(test.kube("get", "pods", "-n", "arc-runners", "-o", "json"))["items"]
        )

    wait_for("owned runner/job Pods removed", pods_gone, 180)
    save(
        output / "result.json",
        {
            "status": "passed",
            "pr": test.pr,
            "service_branch": test.branch,
            "runner_names": names,
            "limits": runner_limits(test.scale_sets()),
            "capacities": test.capacities(),
            "temporary_runners_removed": True,
            "source": str(kind.UPSTREAM),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key")
    parser.add_argument("--worker", choices=["prepare", "cancel", "restore", "successor"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.worker:
        worker(args.worker, args.output, args.key)
    else:
        with (kind.ROOT / "drain-recovery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_suite(args.output.resolve())


if __name__ == "__main__":
    main()
