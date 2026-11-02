"""Tests for the provider-neutral Agent review policy boundary."""

from pathlib import Path

import pytest

from yaga.agent_review.policy import (
    CLAUDE_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_APP_ID,
    CODEX_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_USER_ID,
    COPILOT_DEFAULT_BOT_LOGIN,
    BotIdentity,
    ReviewLens,
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

[agent-review.agents.security]
preset = "codex"

[agent-review.agents.docs]
preset = "other-agent"
outcome = "advisory"
publication = "comment"
""",
        )
    )
    assert policy.required == ("correctness", "security")
    assert [lens.name for lens in policy.lenses] == ["correctness", "security", "docs"]
    assert policy.lens("docs").outcome == "advisory"
    assert policy.lens("docs").publication == "comment"


def test_load_policy_allows_omitting_agents_table_for_builtin_presets(tmp_path: Path) -> None:
    policy = load_policy(
        write_policy(
            tmp_path,
            """
[agent-review]
version = 1
required = ["codex", "copilot", "claude"]
""",
        )
    )
    assert policy.required == ("codex", "copilot", "claude")
    assert [lens.name for lens in policy.lenses] == ["codex", "copilot", "claude"]
    assert policy.lens("codex").resolve_bot().bot_login == CODEX_DEFAULT_BOT_LOGIN
    assert policy.lens("copilot").resolve_bot().bot_login == COPILOT_DEFAULT_BOT_LOGIN
    assert policy.lens("claude").resolve_bot().bot_login == CLAUDE_DEFAULT_BOT_LOGIN


def test_load_policy_resolves_builtin_presets_and_explicit_bot_overrides(tmp_path: Path) -> None:
    policy = load_policy(
        write_policy(
            tmp_path,
            """
[agent-review]
version = 1
required = ["codex-default", "copilot-default", "claude-default"]

[agent-review.agents.codex-default]
preset = "codex"

[agent-review.agents.copilot-default]
preset = "copilot"

[agent-review.agents.claude-default]
preset = "claude"
check-runs = ["claude-review"]
delay-seconds = 15
wait-seconds = 120

[agent-review.agents.custom-bot]
outcome = "advisory"
bot-login = "acme-reviewer[bot]"
bot-user-id = 424242
bot-app-id = 99999
pass-reactions = ["+1", "rocket"]
pending-reactions = ["eyes"]
check-runs = ["acme-lint", "acme-security"]
delay-seconds = 30
wait-seconds = 300
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
    assert policy.lens("claude-default").resolve_bot() == BotIdentity(
        bot_login=CLAUDE_DEFAULT_BOT_LOGIN,
        bot_user_id=None,
        bot_app_id=None,
        pass_reactions=("+1",),
        pending_reactions=("eyes",),
        check_runs=("claude-review",),
        delay_seconds=15,
        wait_seconds=120,
    )
    assert policy.lens("custom-bot").resolve_bot() == BotIdentity(
        bot_login="acme-reviewer[bot]",
        bot_user_id=424242,
        bot_app_id=99999,
        pass_reactions=("+1", "rocket"),
        pending_reactions=("eyes",),
        check_runs=("acme-lint", "acme-security"),
        delay_seconds=30,
        wait_seconds=300,
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
""",
        )
    )
    with pytest.raises(ConfigurationError, match="requires explicit bot-login"):
        policy.lens("custom").resolve_bot()


def test_programmatic_lens_rejects_invalid_check_runs_or_delay() -> None:
    with pytest.raises(ConfigurationError, match="check-runs is invalid"):
        ReviewLens("codex", "codex", check_runs=()).resolve_bot()
    with pytest.raises(ConfigurationError, match="delay-seconds is invalid"):
        ReviewLens("codex", "codex", delay_seconds=301).resolve_bot()
    with pytest.raises(ConfigurationError, match="wait-seconds is invalid"):
        ReviewLens("codex", "codex", wait_seconds=601).resolve_bot()


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            "[agent-review]\nversion = 2\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'",
            "version",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\nextra = 1\n[agent-review.agents.x]\npreset='codex'",
            "unknown key",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ninstruction='x'",
            "unknown key instruction",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\noutcome='advisory'",
            "must have outcome review",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\npublication='emoji'",
            "publication is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['missing']\n[agent-review.agents.x]\npreset='codex'",
            "unknown lens",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\noutcome='review'",
            "requires preset or bot-login",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nbot-login='-bad'",
            "bot-login is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nbot-user-id=0",
            "bot-user-id is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nbot-app-id=true",
            "bot-app-id is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\npass-reactions=[]",
            "pass-reactions is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\npass-reactions=['eyes']",
            "overlap",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ncheck-runs=[]",
            "check-runs is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ncheck-runs=['dup', 'dup']",
            "check-runs is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ncheck-runs=[' leading']",
            "check-runs is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ndelay-seconds=-1",
            "delay-seconds is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ndelay-seconds=301",
            "delay-seconds is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\ndelay-seconds=true",
            "delay-seconds is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nwait-seconds=-1",
            "wait-seconds is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nwait-seconds=601",
            "wait-seconds is invalid",
        ),
        (
            "[agent-review]\nversion = 1\nrequired = ['x']\n[agent-review.agents.x]\npreset='codex'\nwait-seconds=true",
            "wait-seconds is invalid",
        ),
    ],
)
def test_load_policy_rejects_invalid_contract(tmp_path: Path, body: str, expected: str) -> None:
    with pytest.raises(ConfigurationError, match=expected):
        load_policy(write_policy(tmp_path, body))
