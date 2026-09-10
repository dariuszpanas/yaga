"""Tests for the provider-neutral Agent review policy boundary."""

from pathlib import Path

import pytest

from yaga.agent_review.policy import load_policy
from yaga.errors import ConfigurationError


def write_policy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".yaga.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_policy_preserves_order_and_required_lenses(tmp_path: Path) -> None:
    policy = load_policy(
        write_policy(
            tmp_path,
            """
[agent-review]
version = 1
required = ["correctness", "security"]
aggregation = "all-required"

[agent-review.agents.correctness]
preset = "codex"
instruction = "Review behavior and tests."

[agent-review.agents.security]
preset = "codex"
instruction = "Review trust boundaries."

[agent-review.agents.docs]
preset = "other-agent"
instruction = "Review documentation."
outcome = "advisory"
""",
        )
    )
    assert policy.required == ("correctness", "security")
    assert [lens.name for lens in policy.lenses] == ["correctness", "security", "docs"]
    assert policy.lens("docs").outcome == "advisory"


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            "[agent-review]\nversion = 2\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'",
            "version",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\nextra = 1\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'",
            "unknown key",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\noutcome='advisory'",
            "must have outcome review",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['missing']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'",
            "unknown lens",
        ),
    ],
)
def test_load_policy_rejects_invalid_contract(tmp_path: Path, body: str, expected: str) -> None:
    with pytest.raises(ConfigurationError, match=expected):
        load_policy(write_policy(tmp_path, body))


def test_load_policy_rejects_unsafe_and_oversized_instruction(tmp_path: Path) -> None:
    body = "[agent-review]\nversion=1\nrequired=['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction="
    with pytest.raises(ConfigurationError, match="unsafe"):
        load_policy(write_policy(tmp_path, body + '"bad\\u0000"'))
    with pytest.raises(ConfigurationError, match="oversized"):
        load_policy(write_policy(tmp_path, body + repr("x" * 4097)))
