"""Short-lived, uniquely labelled native runners outside the drained ARC set."""

import contextlib
import hashlib
import os
import signal
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from drain_test_run import wait_for


def runner_archive(test, cache):
    application = next(
        a for a in test.api("actions/runners/downloads") if a["os"] == "linux" and a["architecture"] == "x64"
    )
    url = application["download_url"]
    if not url.startswith("https://github.com/actions/runner/releases/download/"):
        raise ValueError("Unexpected official runner download URL")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / Path(application["filename"]).name
    if not path.exists():
        with (
            urllib.request.urlopen(url, timeout=120) as source,  # noqa: S310 -- official HTTPS prefix checked above
            path.with_suffix(".tmp").open("wb") as dest,
        ):
            while chunk := source.read(1024 * 1024):
                dest.write(chunk)
        path.with_suffix(".tmp").replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != application["sha256_checksum"]:
        raise ValueError("Official runner archive checksum mismatch")
    return path, application


class NativeRunner:
    def __init__(self, test, name, directory):
        self.test, self.name, self.directory = test, name, directory
        self.process = None

    def registrations(self):
        return [r for r in self.test.api("actions/runners?per_page=100")["runners"] if r["name"] == self.name]

    def start(self, archive):
        if self.registrations():
            raise RuntimeError(f"Runner name already exists: {self.name}")
        self.directory.mkdir(mode=0o700)
        with tarfile.open(archive) as source:
            source.extractall(self.directory, filter="data")
        registration = self.test.api("actions/runners/registration-token", {}, "POST")["token"]
        env = {**os.environ, "RUNNER_ALLOW_RUNASROOT": "1"}
        env.pop("GH_TOKEN", None)
        configure = subprocess.run(
            [
                "bash",
                "-c",
                './config.sh --unattended --url "$REG_URL" --token "$REG_TOKEN" '
                '--name "$REG_NAME" --no-default-labels --labels "$REG_NAME" --disableupdate --work _work',
            ],
            cwd=self.directory,
            env={
                **env,
                "REG_URL": f"https://github.com/{self.test.repository}",
                "REG_TOKEN": registration,
                "REG_NAME": self.name,
            },
            text=True,
            capture_output=True,
            timeout=120,
        )
        if configure.returncode:
            raise RuntimeError((configure.stdout + configure.stderr).replace(registration, "[REDACTED]"))
        with (self.directory.parent / (self.name + ".log")).open("w") as log:
            self.process = subprocess.Popen(
                ["./run.sh"], cwd=self.directory, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        wait_for(
            "native runner online: " + self.name,
            lambda: any(r["status"] == "online" for r in self.registrations()),
            120,
            poll=3,
        )

    def stop(self):
        if self.process and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        for runner in self.registrations():
            self.test.api(f"actions/runners/{runner['id']}", method="DELETE")
        if self.registrations():
            raise RuntimeError("Temporary runner registration survived cleanup")
        # Keep binaries cached, but remove runner authentication material.
        for name in (".credentials", ".credentials_rsaparams", ".runner"):
            (self.directory / name).unlink(missing_ok=True)
