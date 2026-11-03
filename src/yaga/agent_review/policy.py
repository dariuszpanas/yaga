"""Strict, bounded configuration for named Agent review lenses."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaga.errors import ConfigurationError
from yaga.files import read_file_prefix
from yaga.policies.schemas import (
    AGENT_REVIEW_POLICY_FAMILY,
    check_optional_required_version,
    validate_family_wire,
)

MAX_POLICY_BYTES = 1024 * 1024
MAX_LENSES = 32
MAX_NAME_LENGTH = 64
MAX_PRESET_LENGTH = 64
MAX_REACTIONS = 8
MAX_CHECK_RUNS = 16
MAX_CHECK_RUN_NAME_LENGTH = 128
MAX_DELAY_SECONDS = 300
MAX_WAIT_SECONDS = 600
MAX_PORTABLE_INT = (1 << 53) - 1

CODEX_DEFAULT_BOT_LOGIN = "chatgpt-codex-connector[bot]"
CODEX_DEFAULT_BOT_USER_ID = 199175422
CODEX_DEFAULT_BOT_APP_ID = 1144995
COPILOT_DEFAULT_BOT_LOGIN = "copilot-pull-request-reviewer[bot]"
CLAUDE_DEFAULT_BOT_LOGIN = "claude[bot]"
BUILTIN_PRESETS = frozenset({"codex", "copilot", "claude"})
DEFAULT_PASS_REACTIONS: tuple[str, ...] = ("+1",)
DEFAULT_PENDING_REACTIONS: tuple[str, ...] = ("eyes",)

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PRESET = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BOT_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$")
_ROOT_KEYS = frozenset({"version", "required-version", "required", "aggregation", "agents"})
_LENS_KEYS = frozenset(
    {
        "preset",
        "outcome",
        "publication",
        "bot-login",
        "bot-user-id",
        "bot-app-id",
        "pass-reactions",
        "pending-reactions",
        "check-runs",
        "delay-seconds",
        "wait-seconds",
    }
)
_AGGREGATIONS = frozenset({"all-required", "any-required"})
_OUTCOMES = frozenset({"review", "advisory"})
_PUBLICATIONS = frozenset({"comment", "inline", "reaction", "review", "check"})
_REACTIONS = frozenset({"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"})


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """Resolved review-bot identity, reaction markers, and check runs for one lens."""

    bot_login: str
    bot_user_id: int | None = None
    bot_app_id: int | None = None
    pass_reactions: tuple[str, ...] = DEFAULT_PASS_REACTIONS
    pending_reactions: tuple[str, ...] = DEFAULT_PENDING_REACTIONS
    check_runs: tuple[str, ...] = ()
    delay_seconds: int = 0
    wait_seconds: int = 0


@dataclass(frozen=True, slots=True)
class ReviewLens:
    """One named agent review lens."""

    name: str
    preset: str = "custom"
    outcome: str = "review"
    publication: str = "review"
    bot_login: str | None = None
    bot_user_id: int | None = None
    bot_app_id: int | None = None
    pass_reactions: tuple[str, ...] | None = None
    pending_reactions: tuple[str, ...] | None = None
    check_runs: tuple[str, ...] | None = None
    delay_seconds: int = 0
    wait_seconds: int = 0

    def resolve_bot(self) -> BotIdentity:
        """Resolve explicit bot identity or built-in preset defaults."""
        pass_reactions = (
            DEFAULT_PASS_REACTIONS if self.pass_reactions is None else self.pass_reactions
        )
        pending_reactions = (
            DEFAULT_PENDING_REACTIONS if self.pending_reactions is None else self.pending_reactions
        )
        if set(pass_reactions) & set(pending_reactions):
            raise ConfigurationError(
                f"Agent review lens {self.name} pass-reactions and pending-reactions overlap"
            )
        check_runs = () if self.check_runs is None else self.check_runs
        if self.check_runs is not None and not _valid_check_runs_tuple(self.check_runs):
            raise ConfigurationError(f"Agent review lens {self.name} check-runs is invalid")
        if type(self.delay_seconds) is not int or not 0 <= self.delay_seconds <= MAX_DELAY_SECONDS:
            raise ConfigurationError(f"Agent review lens {self.name} delay-seconds is invalid")
        if type(self.wait_seconds) is not int or not 0 <= self.wait_seconds <= MAX_WAIT_SECONDS:
            raise ConfigurationError(f"Agent review lens {self.name} wait-seconds is invalid")
        if self.preset == "codex":
            bot_login = CODEX_DEFAULT_BOT_LOGIN if self.bot_login is None else self.bot_login
            use_codex_defaults = bot_login.casefold() == CODEX_DEFAULT_BOT_LOGIN.casefold()
            bot_user_id = (
                CODEX_DEFAULT_BOT_USER_ID
                if self.bot_user_id is None and use_codex_defaults
                else self.bot_user_id
            )
            bot_app_id = (
                CODEX_DEFAULT_BOT_APP_ID
                if self.bot_app_id is None and use_codex_defaults
                else self.bot_app_id
            )
            return BotIdentity(
                bot_login=bot_login,
                bot_user_id=bot_user_id,
                bot_app_id=bot_app_id,
                pass_reactions=pass_reactions,
                pending_reactions=pending_reactions,
                check_runs=check_runs,
                delay_seconds=self.delay_seconds,
                wait_seconds=self.wait_seconds,
            )
        if self.preset == "copilot":
            bot_login = COPILOT_DEFAULT_BOT_LOGIN if self.bot_login is None else self.bot_login
            return BotIdentity(
                bot_login=bot_login,
                bot_user_id=self.bot_user_id,
                bot_app_id=self.bot_app_id,
                pass_reactions=pass_reactions,
                pending_reactions=pending_reactions,
                check_runs=check_runs,
                delay_seconds=self.delay_seconds,
                wait_seconds=self.wait_seconds,
            )
        if self.preset == "claude":
            bot_login = CLAUDE_DEFAULT_BOT_LOGIN if self.bot_login is None else self.bot_login
            return BotIdentity(
                bot_login=bot_login,
                bot_user_id=self.bot_user_id,
                bot_app_id=self.bot_app_id,
                pass_reactions=pass_reactions,
                pending_reactions=pending_reactions,
                check_runs=check_runs,
                delay_seconds=self.delay_seconds,
                wait_seconds=self.wait_seconds,
            )
        if self.bot_login is None:
            raise ConfigurationError(
                f"Agent review lens {self.name} with preset {self.preset!r} requires "
                "explicit bot-login for pull-request review checks"
            )
        return BotIdentity(
            bot_login=self.bot_login,
            bot_user_id=self.bot_user_id,
            bot_app_id=self.bot_app_id,
            pass_reactions=pass_reactions,
            pending_reactions=pending_reactions,
            check_runs=check_runs,
            delay_seconds=self.delay_seconds,
            wait_seconds=self.wait_seconds,
        )


@dataclass(frozen=True, slots=True)
class AgentReviewPolicy:
    """Validated policy for aggregating named review lenses."""

    version: int
    required: tuple[str, ...]
    aggregation: str
    lenses: tuple[ReviewLens, ...]

    def lens(self, name: str) -> ReviewLens:
        """Return one configured lens by its canonical name."""
        for lens in self.lenses:
            if lens.name == name:
                return lens
        raise KeyError(name)


def load_policy(path: Path) -> AgentReviewPolicy:
    """Load one explicit TOML file containing an ``agent-review`` table."""
    resolved = path.expanduser().resolve()
    try:
        raw = read_file_prefix(resolved, maximum=MAX_POLICY_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read Agent review policy {resolved}: {error}") from error
    if len(raw) > MAX_POLICY_BYTES:
        raise ConfigurationError(
            f"Agent review policy exceeds {MAX_POLICY_BYTES} bytes: {resolved}"
        )
    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"Agent review policy is not valid UTF-8: {resolved}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(
            f"invalid Agent review policy TOML in {resolved}: {error}"
        ) from error
    root: Any = document
    if resolved.name == "pyproject.toml":
        tool = document.get("tool")
        if not isinstance(tool, dict) or not isinstance(tool.get("yaga"), dict):
            raise ConfigurationError(f"{resolved} has no [tool.yaga] table")
        root = tool["yaga"]
    if not isinstance(root, dict) or not isinstance(root.get("agent-review"), dict):
        raise ConfigurationError(f"{resolved} has no [agent-review] table")
    check_optional_required_version(root)
    return _parse(root["agent-review"], resolved)


def _parse(root: dict[str, Any], path: Path) -> AgentReviewPolicy:
    check_optional_required_version(root)
    _reject_unknown(root, _ROOT_KEYS, "agent-review", path)
    version = root.get("version")
    if type(version) is int and version > int(AGENT_REVIEW_POLICY_FAMILY.current_version):
        raise ConfigurationError(
            f"agent-review.version must be integer 1 in {path} "
            f"(found newer schema version {version}; update YAGA using its installation manager)"
        )
    if type(version) is not int or version != 1:
        raise ConfigurationError(f"agent-review.version must be integer 1 in {path}")
    aggregation = root.get("aggregation", "all-required")
    if not isinstance(aggregation, str) or aggregation not in _AGGREGATIONS:
        raise ConfigurationError(f"agent-review.aggregation is invalid in {path}")
    required_names = _parse_required_names(root.get("required", []), path)
    raw_agents = root.get("agents")
    if raw_agents is None:
        raw_agents = {}
    elif not isinstance(raw_agents, dict) or not raw_agents:
        raise ConfigurationError(f"agent-review.agents must be a nonempty table in {path}")
    if len(raw_agents) > MAX_LENSES:
        raise ConfigurationError(f"agent-review.agents exceeds {MAX_LENSES} entries in {path}")

    lenses: list[ReviewLens] = []
    seen: set[str] = set()
    for raw_name, raw_lens in raw_agents.items():
        name = _name(raw_name, "agent-review lens", path)
        if name in seen:
            raise ConfigurationError(f"duplicate Agent review lens: {name}")
        seen.add(name)
        if not isinstance(raw_lens, dict):
            raise ConfigurationError(f"agent-review.agents.{name} must be a table in {path}")
        _reject_unknown(raw_lens, _LENS_KEYS, f"agent-review.agents.{name}", path)
        has_explicit_preset = "preset" in raw_lens
        if has_explicit_preset:
            preset = raw_lens.get("preset")
            if not isinstance(preset, str) or not _PRESET.fullmatch(preset):
                raise ConfigurationError(f"agent-review.agents.{name}.preset is invalid in {path}")
        else:
            preset = name if name in BUILTIN_PRESETS else "custom"
        outcome = raw_lens.get("outcome", "review")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ConfigurationError(f"agent-review.agents.{name}.outcome is invalid in {path}")
        publication = raw_lens.get("publication", "review")
        if not isinstance(publication, str) or publication not in _PUBLICATIONS:
            raise ConfigurationError(f"agent-review.agents.{name}.publication is invalid in {path}")
        bot_login = _optional_bot_login(raw_lens.get("bot-login"), name=name, path=path)
        if not has_explicit_preset and name not in BUILTIN_PRESETS and bot_login is None:
            raise ConfigurationError(
                f"agent-review.agents.{name} requires preset or bot-login in {path}"
            )
        bot_user_id = _optional_positive_int(
            raw_lens.get("bot-user-id"), key="bot-user-id", name=name, path=path
        )
        bot_app_id = _optional_positive_int(
            raw_lens.get("bot-app-id"), key="bot-app-id", name=name, path=path
        )
        pass_reactions = _optional_reactions(
            raw_lens.get("pass-reactions"),
            key="pass-reactions",
            name=name,
            path=path,
            allow_empty=False,
        )
        pending_reactions = _optional_reactions(
            raw_lens.get("pending-reactions"),
            key="pending-reactions",
            name=name,
            path=path,
            allow_empty=True,
        )
        effective_pass = DEFAULT_PASS_REACTIONS if pass_reactions is None else pass_reactions
        effective_pending = (
            DEFAULT_PENDING_REACTIONS if pending_reactions is None else pending_reactions
        )
        if set(effective_pass) & set(effective_pending):
            raise ConfigurationError(
                f"agent-review.agents.{name} pass-reactions and pending-reactions overlap in {path}"
            )
        check_runs = _optional_check_runs(raw_lens.get("check-runs"), name=name, path=path)
        delay_seconds = _optional_delay_seconds(raw_lens.get("delay-seconds"), name=name, path=path)
        wait_seconds = _optional_wait_seconds(raw_lens.get("wait-seconds"), name=name, path=path)
        lenses.append(
            ReviewLens(
                name=name,
                preset=preset,
                outcome=outcome,
                publication=publication,
                bot_login=bot_login,
                bot_user_id=bot_user_id,
                bot_app_id=bot_app_id,
                pass_reactions=pass_reactions,
                pending_reactions=pending_reactions,
                check_runs=check_runs,
                delay_seconds=delay_seconds,
                wait_seconds=wait_seconds,
            )
        )

    for req_name in required_names:
        if req_name not in seen:
            if req_name not in BUILTIN_PRESETS:
                raise ConfigurationError(
                    f"agent-review.required names unknown lens {req_name} in {path}"
                )
            seen.add(req_name)
            lenses.append(
                ReviewLens(
                    name=req_name,
                    preset=req_name,
                    outcome="review",
                    publication="review",
                )
            )
        elif next(lens for lens in lenses if lens.name == req_name).outcome != "review":
            raise ConfigurationError(
                f"agent-review.required lens {req_name} must have outcome review"
            )

    if len(lenses) > MAX_LENSES:
        raise ConfigurationError(f"agent-review.agents exceeds {MAX_LENSES} entries in {path}")
    validate_family_wire(
        AGENT_REVIEW_POLICY_FAMILY,
        root,
        version_key="version",
        version_error_message=f"agent-review.version must be integer 1 in {path}",
    )
    return AgentReviewPolicy(version, required_names, aggregation, tuple(lenses))


def _optional_bot_login(value: Any, *, name: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _BOT_LOGIN.fullmatch(value) is None:
        raise ConfigurationError(f"agent-review.agents.{name}.bot-login is invalid in {path}")
    return value


def _optional_positive_int(value: Any, *, key: str, name: str, path: Path) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_PORTABLE_INT:
        raise ConfigurationError(f"agent-review.agents.{name}.{key} is invalid in {path}")
    return value


def _optional_reactions(
    value: Any,
    *,
    key: str,
    name: str,
    path: Path,
    allow_empty: bool,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    minimum = 0 if allow_empty else 1
    if not isinstance(value, list) or not minimum <= len(value) <= MAX_REACTIONS:
        raise ConfigurationError(f"agent-review.agents.{name}.{key} is invalid in {path}")
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in _REACTIONS or item in seen:
            raise ConfigurationError(f"agent-review.agents.{name}.{key} is invalid in {path}")
        seen.append(item)
    return tuple(seen)


def _valid_check_run_name(item: Any) -> bool:
    return (
        isinstance(item, str)
        and 1 <= len(item) <= MAX_CHECK_RUN_NAME_LENGTH
        and item.strip() == item
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in item)
    )


def _valid_check_runs_tuple(items: tuple[str, ...]) -> bool:
    if not isinstance(items, tuple) or not 1 <= len(items) <= MAX_CHECK_RUNS:
        return False
    seen: set[str] = set()
    for item in items:
        if not _valid_check_run_name(item) or item in seen:
            return False
        seen.add(item)
    return True


def _optional_check_runs(value: Any, *, name: str, path: Path) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHECK_RUNS:
        raise ConfigurationError(f"agent-review.agents.{name}.check-runs is invalid in {path}")
    seen: list[str] = []
    for item in value:
        if not _valid_check_run_name(item) or item in seen:
            raise ConfigurationError(f"agent-review.agents.{name}.check-runs is invalid in {path}")
        seen.append(item)
    return tuple(seen)


def _optional_delay_seconds(value: Any, *, name: str, path: Path) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= MAX_DELAY_SECONDS:
        raise ConfigurationError(f"agent-review.agents.{name}.delay-seconds is invalid in {path}")
    return value


def _optional_wait_seconds(value: Any, *, name: str, path: Path) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= MAX_WAIT_SECONDS:
        raise ConfigurationError(f"agent-review.agents.{name}.wait-seconds is invalid in {path}")
    return value


def _parse_required_names(raw: Any, path: Path) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_LENSES:
        raise ConfigurationError(
            f"agent-review.required must be a nonempty bounded array in {path}"
        )
    result: list[str] = []
    for value in raw:
        name = _name(value, "required Agent review lens", path)
        if name in result:
            raise ConfigurationError(f"agent-review.required repeats {name} in {path}")
        result.append(name)
    return tuple(result)


def _name(value: Any, label: str, path: Path) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ConfigurationError(f"{label} name is invalid in {path}")
    return value


def _reject_unknown(
    mapping: dict[str, Any], allowed: frozenset[str], label: str, path: Path
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigurationError(f"{label} has unknown key {unknown[0]} in {path}")
