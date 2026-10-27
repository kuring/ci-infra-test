"""Opt-in Linux/Kind/GitHub recovery acceptance; never targets AWS staging.

The parent sends cancellation to a real controller subprocess, then starts a
fresh restore process. GitHub Actions' server-side cancellation is not emulated.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import drain_test_kind as kind
from drain_test_checks import require_limits, runner_limits
from drain_test_kind import wait_for
from drain_test_run import CLEANUP_TIMEOUT, RESTORE_TIMEOUT, RUN_TIMEOUT, run_controller

BRANCH = "codex/osdc-drain-recovery-kind"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def controller_args(directory, *, defer=False, recovery=True):
    return argparse.Namespace(
        action="test",
        root_dir=kind.ROOT,
        upstream_dir=kind.UPSTREAM,
        clusters_yaml=kind.ROOT / "clusters.yaml",
        run_timeout=RUN_TIMEOUT,
        cleanup_timeout=CLEANUP_TIMEOUT,
        restore_timeout=RESTORE_TIMEOUT,
        start_timeout=600,
        finish_timeout=600,
        drain_timeout=180,
        observe_seconds=60,
        defer_recovery=defer,
        recovery_file=directory / "recovery.json" if recovery else None,
    )


class RecoveryKind(kind.KindDrainTest):
    def __init__(self, args, key, directory, checkpoint=False):
        super().__init__(args, key)
        self.branch = BRANCH
        self.directory = directory
        self.checkpoint = checkpoint

    def drained(self, **kwargs):
        result = super().drained(**kwargs)
        if result and self.checkpoint:
            save(
                self.directory / "drained.json",
                {
                    "pid": os.getpid(),
                    "branch": self.branch,
                    "pr": self.pr,
                    "run_id": self.run_id,
                    "desired": self.desired,
                    "group": self.drain.pid,
                    "probe_ids": {phase: sorted(ids) for phase, ids in self.probe_ids.items()},
                },
            )
            # Deliberately pause before releasing inflight, until the parent sends
            # the same signal a cancelling runner would send to this process.
            while True:
                time.sleep(0.1)
        return result


def worker(action, directory, key):
    args = controller_args(directory, defer=action == "cancel", recovery=action != "reclaim")
    test = RecoveryKind(args, key, directory, checkpoint=action == "cancel")
    if action == "cancel":
        run_controller(test)
    elif action == "restore":
        run_controller(test, restore_only=True)
        require_limits(runner_limits(test.scale_sets()), test.desired)
        wait_for("advertised capacity restored", lambda: test.capacities().get(kind.LABEL, 0) > 0, 180)
        save(directory / "restored.json", {"pid": os.getpid(), "limits": test.desired, "capacities": test.capacities()})
    else:
        test.prepare()
        test.cleanup_stale_canary()
        require(not exact_ref(test, BRANCH), "fixed branch survived upstream startup cleanup")
        require(not open_pulls(test), "stale PR survived upstream startup cleanup")
        try:
            test.create_pr()
            wait_for("new workflow on reclaimed branch", test.find_run, 180)
            save(
                directory / "recreated.json",
                {"pid": os.getpid(), "pr": test.pr, "run_id": test.run_id, "branch": test.branch},
            )
        finally:
            if not test.cleanup():
                raise RuntimeError("Recreated canary cleanup failed")


def exact_ref(test, branch):
    ref = f"refs/heads/{branch}"
    return next((r for r in test.api(f"git/matching-refs/heads/{branch}") if r["ref"] == ref), None)


def open_pulls(test):
    return test.api(f"pulls?head={test.repository.split('/')[0]}:{BRANCH}&state=open&per_page=100")


def all_runs(test):
    result = []
    page = 1
    while True:
        rows = test.api(f"actions/runs?event=pull_request&branch={BRANCH}&per_page=100&page={page}")["workflow_runs"]
        result.extend(rows)
        if len(rows) < 100:
            return result
        page += 1


def launch(action, directory, key, env):
    log = (directory / f"{action}.log").open("w")
    try:
        return subprocess.Popen(
            [sys.executable, __file__, "--worker", action, "--output", str(directory), "--key", key],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()


def joined(action, directory, key, env, timeout):
    process = launch(action, directory, key, env)
    try:
        status = process.wait(timeout=timeout)
    except BaseException:
        process.kill()
        process.wait(timeout=10)
        raise
    if status:
        raise RuntimeError(f"{action} process exited {status}; see {directory / (action + '.log')}")
    return process


def verify_restored(test, expected):
    require_limits(runner_limits(test.scale_sets()), expected)
    require(test.capacities().get(kind.LABEL, 0) > 0, "Acceptance failed: test.capacities().get(kind.LABEL, 0) > 0")


def cancellation_case(test, directory, sig, env):
    directory.mkdir()
    key = uuid4().hex
    child = launch("cancel", directory, key, env)
    recovery = RecoveryKind(controller_args(directory), key, directory)
    restored = False
    try:

        def ready():
            if child.poll() is not None:
                raise RuntimeError(f"Controller exited {child.returncode} before drain; see {directory / 'cancel.log'}")
            return (directory / "drained.json").exists()

        wait_for("controller reached drained checkpoint", ready, 1000, poll=2)
        before = json.loads((directory / "drained.json").read_text())
        save(
            directory / "before-cancel.json",
            {"limits": runner_limits(test.scale_sets()), "capacities": test.capacities()},
        )
        require(
            all(value == 0 for value in runner_limits(test.scale_sets()).values()),
            "Acceptance failed: all((value == 0 for value in runner_limits(test.scale_sets()).values()))",
        )
        require(
            all(value == 0 for value in test.capacities().values()),
            "Acceptance failed: all((value == 0 for value in test.capacities().values()))",
        )
        token = json.loads(recovery.recovery_file.read_text())["token"]
        require(recovery.recorded_drain_groups(token), "no real tagged recipe process was present")
        child.send_signal(sig)
        status = child.wait(timeout=45)
        require(status == (-sig if sig == signal.SIGKILL else 128 + sig), status)
        if sig == signal.SIGKILL:
            require(recovery.recorded_drain_groups(token), "SIGKILL did not leave an orphan recipe")
        require(
            test.api(f"pulls/{before['pr']}")["state"] == "open",
            "Acceptance failed: test.api(f\"pulls/{before['pr']}\")['state'] == 'open'",
        )
        # No inline resume: the independent recovery process must perform it.
        require_limits(runner_limits(test.scale_sets()), dict.fromkeys(before["desired"], 0))
        restore = joined("restore", directory, key, env, 240)
        require(restore.pid != child.pid, "Acceptance failed: restore.pid != child.pid")
        require(
            not recovery.recorded_drain_groups(token), "Acceptance failed: not recovery.recorded_drain_groups(token)"
        )
        verify_restored(test, before["desired"])
        time.sleep(3)
        verify_restored(test, before["desired"])
        restored = True
        # Restore-only must leave canary objects for the next invocation.
        require(exact_ref(test, BRANCH), "Acceptance failed: exact_ref(test, BRANCH)")
        require(
            test.api(f"pulls/{before['pr']}")["state"] == "open",
            "Acceptance failed: test.api(f\"pulls/{before['pr']}\")['state'] == 'open'",
        )
        old_runs = all_runs(test)
        require(
            any(r["id"] == before["run_id"] for r in old_runs),
            "Acceptance failed: any((r['id'] == before['run_id'] for r in old_runs))",
        )
        require(
            any(r["status"] != "completed" for r in old_runs),
            "Acceptance failed: any((r['status'] != 'completed' for r in old_runs))",
        )
        joined("reclaim", directory, uuid4().hex, env, 300)
        recreated = json.loads((directory / "recreated.json").read_text())
        require(
            recreated["pr"] != before["pr"] and recreated["run_id"] != before["run_id"],
            "Acceptance failed: recreated['pr'] != before['pr'] and recreated['run_id'] != before['run_id']",
        )
        require(
            test.api(f"pulls/{before['pr']}")["state"] == "closed",
            "Acceptance failed: test.api(f\"pulls/{before['pr']}\")['state'] == 'closed'",
        )
        wait_for(
            "all old and new owned runs completed",
            lambda: all(r["status"] == "completed" for r in all_runs(test)),
            180,
            poll=5,
        )
        require(
            not exact_ref(test, BRANCH) and (not open_pulls(test)),
            "Acceptance failed: not exact_ref(test, BRANCH) and (not open_pulls(test))",
        )
        save(
            directory / "result.json",
            {
                "status": "passed",
                "signal": sig.name,
                "original": before,
                "recreated": recreated,
                "restore_pid": restore.pid,
                "runs": [{k: r.get(k) for k in ("id", "status", "conclusion")} for r in all_runs(test)],
            },
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        # Always fence token-bearing recipes before restoring; do not mask a
        # fencing failure with a blind resume that could race a zero writer.
        if recovery.recovery_file.exists() and not restored:
            joined("restore", directory, key, env, 240)
        test.cleanup_stale_canary()


def run_suite(output):
    if sys.platform != "linux":
        raise RuntimeError("Live recovery acceptance requires Linux /proc")
    kind.guard()
    kind.render()
    os.environ["GH_TOKEN"] = kind.read_token(kind.ROOT)
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    test = RecoveryKind(controller_args(output, recovery=False), uuid4().hex, output)
    test.prepare()
    require_limits(runner_limits(test.scale_sets()), test.desired)
    sentinel = BRANCH + "-other"
    if exact_ref(test, BRANCH) or exact_ref(test, sentinel) or open_pulls(test):
        raise RuntimeError("Dedicated acceptance branches already exist; inspect and recover the previous run first")
    baseline = test.scale_sets()
    save(output / "baseline.json", baseline)
    head = test.api("git/ref/heads/main")["object"]["sha"]
    sentinel_created = False
    try:
        sentinel_created = True
        test.api("git/refs", {"ref": f"refs/heads/{sentinel}", "sha": head}, "POST")
        sentinel_before = exact_ref(test, sentinel)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            logging.info("START cancellation case %s", sig.name)
            cancellation_case(test, output / sig.name.lower(), sig, env)
            require(exact_ref(test, sentinel) == sentinel_before, "similarly named ref was changed")
        # Previous process died after creating its branch but before creating PR.
        orphan = output / "branch-only"
        orphan.mkdir()
        test.api("git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": head}, "POST")
        require(
            exact_ref(test, BRANCH) and (not open_pulls(test)),
            "Acceptance failed: exact_ref(test, BRANCH) and (not open_pulls(test))",
        )
        joined("reclaim", orphan, uuid4().hex, env, 300)
        require(
            not exact_ref(test, BRANCH) and (not open_pulls(test)),
            "Acceptance failed: not exact_ref(test, BRANCH) and (not open_pulls(test))",
        )
        require(
            exact_ref(test, sentinel) == sentinel_before,
            "Acceptance failed: exact_ref(test, sentinel) == sentinel_before",
        )
        wait_for("orphan case runs completed", lambda: all(r["status"] == "completed" for r in all_runs(test)), 180)
        verify_restored(test, test.desired)
        current = {s["metadata"]["name"]: s for s in test.scale_sets()}
        for original in baseline:
            actual = current[original["metadata"]["name"]]
            require(
                actual["spec"].get("listenerTemplate") == original["spec"].get("listenerTemplate"),
                "Acceptance failed: actual['spec'].get('listenerTemplate') == original['spec'].get('listenerTemplate')",
            )
            require(
                actual["spec"].get("minRunners", 0) == original["spec"].get("minRunners", 0),
                "Acceptance failed: actual['spec'].get('minRunners', 0) == original['spec'].get('minRunners', 0)",
            )
    finally:
        test.cleanup_stale_canary()
        if sentinel_created and exact_ref(test, sentinel):
            test.api(f"git/refs/heads/{sentinel}", method="DELETE")
    require(not exact_ref(test, sentinel), "sentinel cleanup failed")
    require(not exact_ref(test, BRANCH), "fixed branch cleanup failed")
    require(not open_pulls(test), "PR cleanup failed")

    def runner_pods_gone():
        pods = json.loads(test.kube("get", "pods", "-n", "arc-runners", "-o", "json"))["items"]
        return not any(p["metadata"]["name"].startswith(kind.LABEL + "-") for p in pods)

    wait_for("owned runner/job pods removed", runner_pods_gone, 180)
    save(
        output / "result.json",
        {
            "status": "passed",
            "cases": ["SIGTERM", "SIGKILL-orphan", "branch-only"],
            "sentinel_preserved": True,
            "github_cleanup_verified": True,
            "runner_pods_removed": True,
            "upstream": str(kind.UPSTREAM),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=["cancel", "restore", "reclaim"], help=argparse.SUPPRESS)
    parser.add_argument("--key", help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.worker:
        worker(args.worker, args.output, args.key)
        return
    output = args.output or kind.ROOT / ("recovery-" + time.strftime("%Y%m%d-%H%M%S"))
    kind.ROOT.mkdir(parents=True, exist_ok=True)
    with (kind.ROOT / "drain-recovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_suite(output.resolve())
    print(f"PASS: {output / 'result.json'}")


if __name__ == "__main__":
    main()
