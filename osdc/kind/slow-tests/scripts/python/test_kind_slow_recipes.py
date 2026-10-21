"""Check standalone/imported recipe dispatch without invoking any test operations."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("imported", [False, True])
@pytest.mark.parametrize(
    ("recipe", "action", "arguments"),
    [
        ("kind-slow", "guard", []),
        ("kind-slow-load", "load", []),
        ("kind-slow-load", "load", ["--jobs", "12", "--timeout", "900"]),
        ("kind-slow-load", "load", ["--dry-run", "--jobs", "6"]),
        ("kind-slow-host-limits", "host-limits", ["--persistent"]),
        ("kind-slow-restore", "restore", ["--reload-janitor"]),
        ("kind-slow-test", "test", ["--jobs", "12", "--timeout", "900"]),
        ("kind-slow-compactor", "compactor", []),
        ("kind-slow-janitor", "janitor", []),
        ("kind-slow-render", "render", []),
        ("kind-slow-create", "create", []),
        ("kind-slow-deploy", "deploy", []),
        ("kind-slow-preload", "preload", []),
        ("kind-slow-cache-uv-public", "cache-uv-public", []),
        ("kind-slow-install-arc", "install-arc", []),
        ("kind-slow-cleanup-github", "cleanup-github", []),
    ],
)
def test_recipes_resolve_their_source_and_forward_arguments(tmp_path, imported, recipe, action, arguments):
    slow = Path(__file__).resolve().parents[2]
    osdc = tmp_path / "checkout with spaces" / "osdc"
    kind = osdc / "kind"
    profile = kind / "slow-tests"
    profile.mkdir(parents=True)
    shutil.copyfile(slow / "recipes.just", profile / "recipes.just")
    main_header = (slow.parent / "justfile").read_text().split("# Create the isolated", 1)[0]
    (kind / "justfile").write_text(main_header)
    executable = tmp_path / "bin" / "mise"
    executable.parent.mkdir()
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "if args[:2]==['exec','--']:\n"
        "    os.execvpe(args[2],args[2:],os.environ)\n"
        "Path(os.environ['OSDC_RECIPE_ARGS_PATH']).write_text(json.dumps(args))\n"
    )
    executable.chmod(0o755)
    capture = tmp_path / "arguments.json"
    env = os.environ | {
        "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"],
        "OSDC_RECIPE_ARGS_PATH": str(capture),
        "OSDC_UPSTREAM": str(tmp_path / "upstream with spaces" / "osdc"),
    }
    entrypoint = kind / "justfile" if imported else profile / "recipes.just"
    subprocess.run(
        ["just", "--justfile", str(entrypoint), recipe, *arguments],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(capture.read_text()) == [
        "--cd",
        env["OSDC_UPSTREAM"],
        "exec",
        "--",
        "uv",
        "run",
        "--frozen",
        "--project",
        env["OSDC_UPSTREAM"],
        str(profile / "scripts/python/kind_slow.py"),
        action,
        *arguments,
    ]
