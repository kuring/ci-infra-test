"""Check generated Actions semantics before publishing a temporary workflow."""

import subprocess

import drain_service_cancel as module
import pytest
import yaml


def test_workflow_preserves_formal_cancel_and_recovery_conditions(tmp_path):
    source = module.formal_job()
    workflow = yaml.safe_load(module.generate_workflow("abc123", tmp_path, ["dedicated-drain", "dedicated-next"]))
    job = workflow["jobs"]["drain"]
    assert "!cancelled() && !failure()" in job["if"]
    assert "always()" not in job["if"]
    assert job["timeout-minutes"] == source["timeout-minutes"]
    for step_id in ("drain", "restore"):
        expected = next(s for s in source["steps"] if s.get("id") == step_id)
        actual = next(s for s in job["steps"] if s.get("id") == step_id)
        assert actual["if"] == expected["if"]
        assert actual["timeout-minutes"] == expected["timeout-minutes"]
        assert "${{ runner.temp }}" in actual["env"]["OSDC_DRAIN_RECOVERY_FILE"]
        assert actual["env"]["OSDC_DRAIN_RECOVERY_FILE"] == expected["env"]["OSDC_DRAIN_RECOVERY_FILE"]
    assert "env" not in job
    assert job["runs-on"] == ["dedicated-drain"]
    follow = workflow["jobs"]["successor"]
    assert follow["runs-on"] == ["dedicated-next"]
    assert follow["concurrency"] == job["concurrency"]
    assert follow["concurrency"]["cancel-in-progress"] is False
    assert "synchronize" in follow["if"]
    assert "opened" in job["if"]
    assert "meta-staging" not in job["concurrency"]["group"]


def test_generated_shell_handles_paths_with_spaces(tmp_path):
    workflow = yaml.safe_load(module.generate_workflow("abc123", tmp_path / "with space", ["drain", "next"]))
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
            assert "'" in step["run"]
    assert "secrets." not in module.generate_workflow("abc123", tmp_path, ["drain", "next"])


def test_successor_refuses_lock_release_without_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("OSDC_DRAIN_RECOVERY_FILE", str(tmp_path / "intent.json"))
    with pytest.raises(RuntimeError, match="lock released before recovery"):
        module.worker("successor", tmp_path, "abc123")
    assert (tmp_path / "successor-start.json").exists()
    assert not (tmp_path / "successor-finished.json").exists()
