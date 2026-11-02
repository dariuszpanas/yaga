"""Tests for the provider-neutral Agent review policy boundary."""

from pathlib import Path

import pytest

from yaga.agent_review.policy import (
    CODEX_DEFAULT_BOT_APP_ID,
    CODEX_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_USER_ID,
    COPILOT_DEFAULT_BOT_LOGIN,
    BotIdentity,
    load_policy,
)
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
publication = "comment"
""",
        )
    )
    assert policy.required == ("correctness", "security")
    assert [lens.name for lens in policy.lenses] == ["correctness", "security", "docs"]
    assert policy.lens("docs").outcome == "advisory"
    assert policy.lens("docs").publication == "comment"


def test_load_policy_resolves_builtin_presets_and_explicit_bot_overrides(tmp_path: Path) -> None:
    policy = load_policy(
        write_policy(
            tmp_path,
            """
[agent-review]
version = 1
required = ["codex-default", "copilot-default"]

[agent-review.agents.codex-default]
preset = "codex"
instruction = "Review correctness."

[agent-review.agents.copilot-default]
preset = "copilot"
instruction = "Review style and structure."

[agent-review.agents.custom-bot]
preset = "custom"
instruction = "Review with a custom bot."
outcome = "advisory"
bot-login = "acme-reviewer[bot]"
bot-user-id = 424242
bot-app-id = 99999
pass-reactions = ["+1", "rocket"]
pending-reactions = ["eyes"]
""",
        )
    )

    assert policy.lens("codex-default").resolve_bot() == BotIdentity(
        bot_login=CODEX_DEFAULT_BOT_LOGIN,
        bot_user_id=CODEX_DEFAULT_BOT_USER_ID,
        bot_app_id=CODEX_DEFAULT_BOT_APP_ID,
        pass_reactions=("+1",),
        pending_reactions=("eyes",),
    )
    assert policy.lens("copilot-default").resolve_bot() == BotIdentity(
        bot_login=COPILOT_DEFAULT_BOT_LOGIN,
        bot_user_id=None,
        bot_app_id=None,
        pass_reactions=("+1",),
        pending_reactions=("eyes",),
    )
    assert policy.lens("custom-bot").resolve_bot() == BotIdentity(
        bot_login="acme-reviewer[bot]",
        bot_user_id=424242,
        bot_app_id=99999,
        pass_reactions=("+1", "rocket"),
        pending_reactions=("eyes",),
    )


def test_custom_preset_requires_explicit_bot_login_for_resolution(tmp_path: Path) -> None:
    policy = load_policy(
        write_policy(
            tmp_path,
            """
[agent-review]
version = 1
required = ["custom"]

[agent-review.agents.custom]
preset = "other-agent"
instruction = "Review changes."
""",
        )
    )
    with pytest.raises(ConfigurationError, match="requires explicit bot-login"):
        policy.lens("custom").resolve_bot()


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
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\npublication='emoji'",
            "publication is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['missing']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'",
            "unknown lens",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\nbot-login='-bad'",
            "bot-login is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\nbot-user-id=0",
            "bot-user-id is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\nbot-app-id=true",
            "bot-app-id is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\npass-reactions=[]",
            "pass-reactions is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'\npass-reactions=['eyes']",
            "overlap",
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
