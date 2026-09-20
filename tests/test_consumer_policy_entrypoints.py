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
    _assert_entrypoint_agreement(tmp_path, text, expected, POLICY)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix: correct behavior", 1),
        ("fix: correct behavior\n\nBrief", 0),
        ("docs: correct wording", 0),
        ("docs: correct wording\n\nToo much prose", 1),
    ],
)
def test_custom_body_entrypoint_agreement(tmp_path: Path, text: str, expected: int) -> None:
    config = 'config-version=1\n[commit]\nbody-policy-by-type={fix="required"}\nbody-max-length=5\n'
    _assert_entrypoint_agreement(tmp_path, text, expected, config)


def _assert_entrypoint_agreement(tmp_path: Path, text: str, expected: int, config: str) -> None:
    repo, event, context = _action_fixture(
        tmp_path, head_message=text, pull_request_title=HEADER, config=config
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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix: correct behavior\n\nRefs: #12\nValidation: passed", 0),
        ("fix: correct behavior\n\nValidation: passed", 1),
        ("fix: correct behavior\n\nRefs: #12\nValidation: TODO", 1),
    ],
)
def test_content_policy_entrypoint_agreement(tmp_path: Path, text: str, expected: int) -> None:
    config = (
        '[commit]\nrequired-issue-prefixes=["#"]\n'
        'required-footer-tokens=["Validation"]\nfooter-values={Validation=["passed"]}\n'
    )
    _assert_entrypoint_agreement(tmp_path, text, expected, config)


@pytest.mark.parametrize(
    "text,expected", [("fix: correct behavior", 0), ("fix: x", 1), ("not conventional", 1)]
)
def test_warning_policy_entrypoint_agreement(tmp_path: Path, text: str, expected: int) -> None:
    config = (
        '[commit]\nbody-policy="required"\nwarning-rules=["body.required"]\n'
        "description-min-length=5\n"
    )
    _assert_entrypoint_agreement(tmp_path, text, expected, config)
