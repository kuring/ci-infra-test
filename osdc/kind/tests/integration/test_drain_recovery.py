"""Linux integration: independent processes, real signals and real /proc fencing."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = Path(__file__).with_name("recovery_worker.py")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux /proc; use Docker recipe")


def wait(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("integration checkpoint timed out")


def events(root):
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize(
    ("sig", "mode"),
    [
        (signal.SIGINT, "normal"),
        (signal.SIGTERM, "normal"),
        (signal.SIGTERM, "ignore-term"),
        (signal.SIGKILL, "orphan"),
    ],
)
def test_cancel_then_independent_restore(tmp_path, sig, mode):
    with (tmp_path / "controller.log").open("w") as log:
        controller = subprocess.Popen(
            [sys.executable, str(WORKER), "cancel", str(tmp_path), mode],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        writer_pid = None
        try:
            wait(lambda: any(e["event"] == "ready" for e in events(tmp_path)))
            writer_pid = int((tmp_path / "writer.pid").read_text())
            assert alive(writer_pid)
            recovery = json.loads((tmp_path / "recovery.json").read_text())
            assert recovery["token"]
            controller.send_signal(sig)
            assert controller.wait(timeout=15) == (-sig if sig == signal.SIGKILL else 128 + sig)
            assert json.loads((tmp_path / "limits.json").read_text())["cpu"] == 0
            assert not any(e["event"] == "resumed" for e in events(tmp_path))
            if sig == signal.SIGKILL:
                assert alive(writer_pid), "orphan scenario did not retain its descendant"
            result = subprocess.run(
                [sys.executable, str(WORKER), "restore", str(tmp_path)], capture_output=True, text=True, timeout=20
            )
            assert result.returncode == 0, result.stdout + result.stderr
            wait(lambda: not alive(writer_pid))
            assert unrelated.poll() is None, "recovery killed an unrelated group"
            restored = [e for e in events(tmp_path) if e["event"] == "resumed"]
            assert len(restored) == 1
            assert restored[0]["pid"] != controller.pid
            time.sleep(0.3)  # A surviving writer would patch zero several times.
            assert json.loads((tmp_path / "limits.json").read_text()) == {"cpu": 3, "excluded": 0}
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)
            for e in events(tmp_path):
                if e["event"] == "writer":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(e["group"], signal.SIGKILL)
            unrelated.kill()
            unrelated.wait(timeout=5)


@pytest.mark.parametrize("has_pr", [False, True])
def test_fixed_branch_reclaimed_by_next_process(tmp_path, has_pr):
    branch = "osdc-drain-test-meta-staging-aws-uw1"
    ref = f"refs/heads/{branch}"
    old = {
        "refs": [ref, ref + "-other"],
        "pulls": [{"number": 10, "state": "open"}] if has_pr else [],
        "runs": [
            {"id": 1, "status": "queued"},
            {"id": 2, "status": "in_progress"},
            {"id": 3, "status": "completed", "conclusion": "success"},
        ],
    }
    (tmp_path / "github.json").write_text(json.dumps(old))
    # Fixture creation and reclamation are separate processes, with no shared objects.
    result = subprocess.run(
        [sys.executable, str(WORKER), "reclaim", str(tmp_path)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((tmp_path / "github.json").read_text())
    assert state["refs"] == [ref + "-other", ref]
    assert all(r["status"] == "completed" for r in state["runs"])
    assert state["runs"][2]["conclusion"] == "success"
    if has_pr:
        assert state["pulls"][0]["state"] == "closed"
    calls = [e for e in events(tmp_path) if e["event"] == "api"]
    cancel_indices = [i for i, e in enumerate(calls) if e["endpoint"].endswith("/cancel")]
    delete_index = next(i for i, e in enumerate(calls) if e["method"] == "DELETE")
    create_index = next(i for i, e in enumerate(calls) if e["endpoint"] == "git/refs")
    assert max(cancel_indices) < delete_index < create_index
    if has_pr:
        close_index = next(i for i, e in enumerate(calls) if e["endpoint"] == "pulls/10")
        assert max(cancel_indices) < close_index < delete_index
    assert not any(e["method"] == "DELETE" and e["endpoint"].endswith("-other") for e in calls)
