"""Pure lexical evaluation of committed paths against portability policy."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.errors import InputError
from yaga.paths.models import (
    MAX_PATH_DIAGNOSTICS,
    PATH_ASCII_CASE_COLLISION_CODE,
    PATH_RULES,
    PATH_WINDOWS_CHARACTER_CODE,
    PATH_WINDOWS_RESERVED_CODE,
    PATH_WINDOWS_TRAILING_CODE,
    PathDiagnostic,
    PathPolicy,
    PathReport,
    PathRuleCount,
    PathSelection,
)

MAX_PATH_CHECK_WORK = 10_000_000

_WINDOWS_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"com{number}" for number in ("¹", "²", "³")),
        *(f"lpt{number}" for number in ("¹", "²", "³")),
    }
)

_RULE_TO_CODE = {
    "windows-characters": PATH_WINDOWS_CHARACTER_CODE,
    "windows-trailing": PATH_WINDOWS_TRAILING_CODE,
    "windows-reserved": PATH_WINDOWS_RESERVED_CODE,
    "ascii-case-collision": PATH_ASCII_CASE_COLLISION_CODE,
}
_RULE_MESSAGES = {
    "windows-trailing": "path component ends in an ASCII space or period",
    "windows-reserved": "path component uses a reserved Windows device basename",
    "ascii-case-collision": "path has an ASCII case-insensitive checkout collision",
}


class _PathCheckWorkLimitError(RuntimeError):
    """Private control-flow signal for the fixed lexical work ceiling."""


@dataclass(slots=True)
class _PathCheckWork:
    """Account for attacker-controlled lexical scanning and alias indexing."""

    limit: int
    used: int = 0

    def spend(self, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("path-check work amount must be nonnegative")
        if amount > self.limit - self.used:
            raise _PathCheckWorkLimitError
        self.used += amount


@dataclass(frozen=True, slots=True)
class _LocalViolations:
    """First component for each component-local rule on one lexical path."""

    windows_character: tuple[int, str] | None
    windows_trailing: int | None
    windows_reserved: int | None


def check_paths(policy: PathPolicy, selection: PathSelection) -> PathReport:
    """Evaluate one exact committed path set in canonical lexical order."""
    if not isinstance(policy, PathPolicy):
        raise TypeError("policy must be a PathPolicy")
    if not isinstance(selection, PathSelection):
        raise TypeError("selection must be a PathSelection")

    work = _PathCheckWork(MAX_PATH_CHECK_WORK)
    try:
        return _check_paths(policy, selection, work=work)
    except _PathCheckWorkLimitError as error:
        raise InputError(
            f"committed-path evaluation exceeds the hard {MAX_PATH_CHECK_WORK}-unit work limit"
        ) from error


def _check_paths(
    policy: PathPolicy,
    selection: PathSelection,
    *,
    work: _PathCheckWork,
) -> PathReport:
    enabled_rules = policy.rules
    enabled = frozenset(enabled_rules)
    inspect_characters = bool(
        enabled
        & {
            "windows-characters",
            "windows-reserved",
            "ascii-case-collision",
        }
    )

    local_by_path: dict[str, _LocalViolations] = {}
    folded_by_path: dict[str, str] = {}
    leaves_by_folded_path: dict[str, list[str]] = {}
    for path in selection.paths:
        components = path.split("/")
        work.spend(len(components))
        first_character: tuple[int, str] | None = None
        first_trailing: int | None = None
        first_reserved: int | None = None
        folded_components: list[str] = []
        for component_number, component in enumerate(components, start=1):
            if (
                "windows-trailing" in enabled
                and first_trailing is None
                and component[-1] in {" ", "."}
            ):
                first_trailing = component_number

            folded_component: str | None = None
            if inspect_characters:
                work.spend(len(component))
                folded_characters: list[str] = []
                for character in component:
                    if (
                        "windows-characters" in enabled
                        and first_character is None
                        and _is_windows_incompatible_character(character)
                    ):
                        first_character = (component_number, character)
                    if "windows-reserved" in enabled or "ascii-case-collision" in enabled:
                        folded_characters.append(_ascii_lower_character(character))
                if folded_characters:
                    folded_component = "".join(folded_characters)
                elif "windows-reserved" in enabled or "ascii-case-collision" in enabled:
                    folded_component = ""

            if "windows-reserved" in enabled:
                assert folded_component is not None
                if (
                    first_reserved is None
                    and folded_component.partition(".")[0] in _WINDOWS_RESERVED_BASENAMES
                ):
                    first_reserved = component_number
            if "ascii-case-collision" in enabled:
                assert folded_component is not None
                folded_components.append(folded_component)

        local_by_path[path] = _LocalViolations(
            windows_character=first_character,
            windows_trailing=first_trailing,
            windows_reserved=first_reserved,
        )
        if "ascii-case-collision" in enabled:
            folded_path = "/".join(folded_components)
            folded_by_path[path] = folded_path
            leaves_by_folded_path.setdefault(folded_path, []).append(path)

    collision_related = _find_ascii_case_collisions(
        selection.paths,
        folded_by_path=folded_by_path,
        leaves_by_folded_path=leaves_by_folded_path,
        work=work,
    )

    counts = dict.fromkeys(enabled_rules, 0)
    diagnostics: list[PathDiagnostic] = []
    for path in selection.paths:
        local = local_by_path[path]
        components_by_rule = {
            "windows-characters": (
                local.windows_character[0] if local.windows_character is not None else None
            ),
            "windows-trailing": local.windows_trailing,
            "windows-reserved": local.windows_reserved,
        }
        for rule in PATH_RULES:
            if rule not in enabled:
                continue
            component = components_by_rule.get(rule)
            related_path = collision_related.get(path) if rule == "ascii-case-collision" else None
            if component is None and related_path is None:
                continue
            counts[rule] += 1
            if len(diagnostics) < MAX_PATH_DIAGNOSTICS:
                message = _RULE_MESSAGES.get(rule)
                if rule == "windows-characters":
                    assert local.windows_character is not None
                    code_point = ord(local.windows_character[1])
                    message = (
                        f"path component contains Windows-incompatible character U+{code_point:04X}"
                    )
                assert message is not None
                diagnostics.append(
                    PathDiagnostic(
                        code=_RULE_TO_CODE[rule],
                        message=message,
                        path=path,
                        component=component,
                        related_path=related_path,
                    )
                )

    return PathReport(
        policy=policy,
        selection=selection,
        diagnostics=tuple(diagnostics),
        rule_counts=tuple(PathRuleCount(rule=rule, count=counts[rule]) for rule in enabled_rules),
    )


def _find_ascii_case_collisions(
    paths: tuple[str, ...],
    *,
    folded_by_path: dict[str, str],
    leaves_by_folded_path: dict[str, list[str]],
    work: _PathCheckWork,
) -> dict[str, str]:
    if not folded_by_path:
        return {}

    work.spend(len(paths))
    lexical_rank = {path: index for index, path in enumerate(paths)}
    related_by_path: dict[str, str] = {}
    for aliases in leaves_by_folded_path.values():
        work.spend(len(aliases))
        if len(aliases) < 2:
            continue
        first, second = aliases[0], aliases[1]
        _keep_lexically_first(related_by_path, first, second, lexical_rank=lexical_rank)
        for alias in aliases[1:]:
            _keep_lexically_first(related_by_path, alias, first, lexical_rank=lexical_rank)

    first_descendant_by_prefix: dict[str, str] = {}
    for path in paths:
        folded_path = folded_by_path[path]
        work.spend(len(folded_path))
        for separator_index, character in enumerate(folded_path):
            if character != "/":
                continue
            # Slicing and hashing this attacker-controlled prefix are linear in
            # its length, so account for that full work rather than one lookup.
            work.spend(separator_index + 1)
            prefix = folded_path[:separator_index]
            prefix_leaves = leaves_by_folded_path.get(prefix)
            if not prefix_leaves:
                continue
            _keep_lexically_first(
                related_by_path,
                path,
                prefix_leaves[0],
                lexical_rank=lexical_rank,
            )
            first_descendant_by_prefix.setdefault(prefix, path)

    for prefix, descendant in first_descendant_by_prefix.items():
        prefix_leaves = leaves_by_folded_path[prefix]
        work.spend(len(prefix_leaves))
        for leaf in prefix_leaves:
            _keep_lexically_first(
                related_by_path,
                leaf,
                descendant,
                lexical_rank=lexical_rank,
            )
    return related_by_path


def _keep_lexically_first(
    mapping: dict[str, str],
    path: str,
    related_path: str,
    *,
    lexical_rank: dict[str, int],
) -> None:
    path_rank = lexical_rank[path]
    related_rank = lexical_rank[related_path]
    if path_rank == related_rank:
        raise ValueError("a colliding path cannot refer to itself")
    existing = mapping.get(path)
    if existing is None or related_rank < lexical_rank[existing]:
        mapping[path] = related_path


def _is_windows_incompatible_character(character: str) -> bool:
    return character in _WINDOWS_CHARACTERS or "\u0001" <= character <= "\u001f"


def _ascii_lower_character(character: str) -> str:
    if "A" <= character <= "Z":
        return chr(ord(character) + (ord("a") - ord("A")))
    return character
