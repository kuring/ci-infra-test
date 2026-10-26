"""Subprocess fixture: real upstream recovery, durable external-service substitutes."""


# ruff: noqa: S101 -- executable assertion fixture, never run with python -O

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

UPSTREAM = Path(os.environ["OSDC_UPSTREAM"])
sys.path.insert(0, str(UPSTREAM / "integration-tests/drain-test/scripts/python"))
from drain_test_run import DRAIN_OWNER_ENV, DrainTest, run_controller  # noqa: E402


def save(path, value):
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def event(root, name, **data):
    with (root / "events.jsonl").open("a") as output:
        output.write(json.dumps({"event": name, "pid": os.getpid(), **data}) + "\n")


class FixtureDrain(DrainTest):
    def prepare(self):
        self.group = "meta-staging-aws-uw1"
        self.probe_label = "c-mt-l-x86iamx-8-16"
        self.desired = {"cpu": 3, "excluded": 0}

    def resume(self):
        # An independent restore process must fence ALL token-bearing writers.
        token = json.loads(self.recovery_file.read_text())["token"]
        assert not self.recorded_drain_groups(token), "resume raced a surviving writer"
        save(self.args.root_dir / "limits.json", self.desired)
        self.draining = False
        event(self.args.root_dir, "resumed")

    def run(self):
        self.prepare()
        owner = self.record_recovery()
        self.draining = True
        self.drain = subprocess.Popen(
            [sys.executable, __file__, "writer", str(self.args.root_dir), self.args.writer_mode],
            env={**self.env, **owner},
            start_new_session=True,
        )
        while not (self.args.root_dir / "writer-ready").exists():
            time.sleep(0.02)
        if self.args.writer_mode == "orphan":
            self.drain.wait(timeout=5)
        event(self.args.root_dir, "ready", group=self.drain.pid)
        while True:
            time.sleep(1)

    def api(self, endpoint, payload=None, method="GET", **_kwargs):
        """Persistent GitHub substitute shared across fresh controller processes."""
        path = self.args.root_dir / "github.json"
        state = json.loads(path.read_text())
        event(self.args.root_dir, "api", endpoint=endpoint, method=method)
        if endpoint.startswith("actions/runs?"):
            result = {"workflow_runs": state["runs"]}
        elif endpoint.startswith("actions/runs/"):
            run = next(r for r in state["runs"] if str(r["id"]) == endpoint.split("/")[2])
            if method == "POST":
                run.update(status="completed", conclusion="cancelled")
            result = run
        elif endpoint.startswith("pulls?"):
            result = [p for p in state["pulls"] if p["state"] == "open"]
        elif endpoint.startswith("pulls/"):
            result = next(p for p in state["pulls"] if str(p["number"]) == endpoint.split("/")[1])
            result.update(payload)
        elif endpoint.startswith("git/matching-refs/"):
            result = [{"ref": ref} for ref in state["refs"]]
        elif endpoint.startswith("git/refs/heads/") and method == "DELETE":
            state["refs"].remove("refs/heads/" + endpoint.removeprefix("git/refs/heads/"))
            result = {}
        elif endpoint == "git/ref/heads/main":
            result = {"object": {"sha": "main"}}
        elif endpoint.startswith("git/commits/"):
            result = {"tree": {"sha": "base-tree"}}
        elif endpoint in {"git/trees", "git/commits"}:
            result = {"sha": "new-object"}
        elif endpoint == "git/refs" and method == "POST":
            assert payload["ref"] not in state["refs"], "stale ref was not reclaimed"
            state["refs"].append(payload["ref"])
            result = {}
        elif endpoint == "pulls" and method == "POST":
            result = {"number": 20, "state": "open"}
            state["pulls"].append(result)
        else:
            raise AssertionError((endpoint, method))
        save(path, state)
        return result


def writer(root, mode):
    if mode == "orphan":
        subprocess.Popen([sys.executable, __file__, "writer", str(root), "ignore-term"])
        return  # Child retains the original group and ownership token.
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    assert os.environ.get(DRAIN_OWNER_ENV)
    event(root, "writer", group=os.getpgrp())
    (root / "writer.pid").write_text(str(os.getpid()))
    save(root / "limits.json", {"cpu": 0, "excluded": 0})
    (root / "writer-ready").touch()
    while True:
        save(root / "limits.json", {"cpu": 0, "excluded": 0})
        time.sleep(0.05)


def main():
    action, directory, *extra = sys.argv[1:]
    root = Path(directory)
    if action == "writer":
        writer(root, extra[0])
        return
    args = argparse.Namespace(
        cluster_id="meta-staging-aws-uw1",
        root_dir=root,
        upstream_dir=UPSTREAM,
        clusters_yaml=root / "clusters.yaml",
        run_timeout=30,
        cleanup_timeout=10,
        restore_timeout=10,
        recovery_file=root / "recovery.json",
        defer_recovery=action == "cancel",
        writer_mode=extra[0] if extra else "normal",
    )
    test = FixtureDrain(args, "integration")
    if action == "reclaim":
        test.prepare()
        test.cleanup_stale_canary()
        test.create_pr()
        event(root, "recreated", branch=test.branch, pr=test.pr)
    else:
        run_controller(test, restore_only=action == "restore")


if __name__ == "__main__":
    main()
