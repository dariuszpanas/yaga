"""Strict, bounded configuration for named Agent review lenses."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaga.errors import ConfigurationError
from yaga.files import read_file_prefix

MAX_POLICY_BYTES = 1024 * 1024
MAX_LENSES = 32
MAX_NAME_LENGTH = 64
MAX_PRESET_LENGTH = 64
MAX_INSTRUCTION_BYTES = 4096

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PRESET = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ROOT_KEYS = frozenset({"version", "required", "aggregation", "agents"})
_LENS_KEYS = frozenset({"preset", "instruction", "outcome", "publication"})
_AGGREGATIONS = frozenset({"all-required", "any-required"})
_OUTCOMES = frozenset({"review", "advisory"})
_PUBLICATIONS = frozenset({"comment", "inline", "reaction", "review", "check"})


@dataclass(frozen=True, slots=True)
class ReviewLens:
    """One named agent review lens."""

    name: str
    preset: str
    instruction: str
    outcome: str
    publication: str = "review"


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
    return _parse(root["agent-review"], resolved)


def _parse(root: dict[str, Any], path: Path) -> AgentReviewPolicy:
    _reject_unknown(root, _ROOT_KEYS, "agent-review", path)
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ConfigurationError(f"agent-review.version must be integer 1 in {path}")
    aggregation = root.get("aggregation", "all-required")
    if not isinstance(aggregation, str) or aggregation not in _AGGREGATIONS:
        raise ConfigurationError(f"agent-review.aggregation is invalid in {path}")
    raw_agents = root.get("agents")
    if not isinstance(raw_agents, dict) or not raw_agents:
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
        preset = raw_lens.get("preset")
        if not isinstance(preset, str) or not _PRESET.fullmatch(preset):
            raise ConfigurationError(f"agent-review.agents.{name}.preset is invalid in {path}")
        instruction = raw_lens.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ConfigurationError(
                f"agent-review.agents.{name}.instruction is required in {path}"
            )
        if len(instruction.encode("utf-8")) > MAX_INSTRUCTION_BYTES or "\x00" in instruction:
            raise ConfigurationError(
                f"agent-review.agents.{name}.instruction is oversized or unsafe"
            )
        outcome = raw_lens.get("outcome", "review")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ConfigurationError(f"agent-review.agents.{name}.outcome is invalid in {path}")
        publication = raw_lens.get("publication", "review")
        if not isinstance(publication, str) or publication not in _PUBLICATIONS:
            raise ConfigurationError(f"agent-review.agents.{name}.publication is invalid in {path}")
        lenses.append(ReviewLens(name, preset, instruction, outcome, publication))
    required = _required(root.get("required", []), seen, lenses, path)
    if not required:
        raise ConfigurationError(f"agent-review.required must name at least one lens in {path}")
    return AgentReviewPolicy(version, required, aggregation, tuple(lenses))


def _required(raw: Any, names: set[str], lenses: list[ReviewLens], path: Path) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_LENSES:
        raise ConfigurationError(
            f"agent-review.required must be a nonempty bounded array in {path}"
        )
    result: list[str] = []
    for value in raw:
        name = _name(value, "required Agent review lens", path)
        if name in result:
            raise ConfigurationError(f"agent-review.required repeats {name} in {path}")
        if name not in names:
            raise ConfigurationError(f"agent-review.required names unknown lens {name} in {path}")
        if next(lens for lens in lenses if lens.name == name).outcome != "review":
            raise ConfigurationError(f"agent-review.required lens {name} must have outcome review")
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
