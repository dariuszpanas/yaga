"""The installed CLI and dependency-free Action agree on the consumer regression corpus."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_commit_action import ROOT, _action_fixture
from tests.test_commit_consumer_policy import BODY, HEADER, POLICY, message


@pytest.mark.parametrize(
    "text,expected",
    [
        (message(), 0),
        (message(body=BODY + "\nhttps://example.com/" + "x" * 100), 0),
        (message(header="fix: Preserve bounded lease ownership"), 1),
        (message(header=HEADER + "."), 1),
        (message(header=HEADER + "?"), 0),
        (message(footer="validation: checked."), 1),
        (message(footer="Validation #123"), 1),
        (message(footer="Validation: " + "x" * 101), 1),
        (message(footer="Validation:\tchecked."), 0),
    ],
)
def test_consumer_entrypoint_agreement(tmp_path: Path, text: str, expected: int) -> None:
    repo, event, context = _action_fixture(
        tmp_path, head_message=text, pull_request_title=HEADER, config=POLICY
    )
    executable = shutil.which("yaga")
    assert executable is not None
    environment = dict(os.environ)
    for key in ("YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME", "YAGA_COMMIT_TRUSTED_CONFIG"):
        environment.pop(key, None)
    cli = subprocess.run(
        [
            executable,
            "commit",
            "check",
            "--stdin",
            "--config",
            str(repo / ".yaga.toml"),
        ],
        input=text,
        env=environment,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert cli.returncode == expected, cli.stdout + cli.stderr
    environment.update(context, PYTHONPATH=str(ROOT / "src"), YAGA_COMMIT_ACTION_RUNTIME="1")
    action = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga",
            "github",
            "pull-request",
            "check",
            "--event-file",
            str(event),
            "--repo",
            str(repo),
            "--format",
            "github",
        ],
        env=environment,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert action.returncode == expected, action.stdout + action.stderr
