"""Pure evaluation of one exact tracked tree against tree policy."""

from __future__ import annotations

from yaga.errors import InputError
from yaga.trees.models import (
    TREE_FORBIDDEN_CODE,
    TREE_REQUIRED_CODE,
    TreeDiagnostic,
    TreePolicy,
    TreeReport,
    TreeSelection,
)
from yaga.trees.patterns import (
    MAX_TREE_MATCH_WORK,
    TreeMatchWorkLimitError,
    _compile_tree_components,
    _match_compiled_tree_components,
    _MatchWork,
    validate_tree_pattern,
)


def check_tree(policy: TreePolicy, selection: TreeSelection) -> TreeReport:
    """Evaluate exact required paths and forbidden patterns deterministically."""
    if not isinstance(policy, TreePolicy):
        raise TypeError("policy must be a TreePolicy")
    if not isinstance(selection, TreeSelection):
        raise TypeError("selection must be a TreeSelection")

    selected = set(selection.paths)
    diagnostics = [
        TreeDiagnostic(
            code=TREE_REQUIRED_CODE,
            message="required tracked path is missing",
            path=path,
        )
        for path in policy.required_paths
        if path not in selected
    ]

    compiled_patterns = tuple(
        (
            pattern,
            _compile_tree_components(validate_tree_pattern(pattern)),
        )
        for pattern in policy.forbidden_patterns
    )
    match_work = _MatchWork(MAX_TREE_MATCH_WORK)
    try:
        for path in selection.paths:
            components = tuple(path.split("/"))
            for pattern, compiled_pattern in compiled_patterns:
                if _match_compiled_tree_components(
                    compiled_pattern,
                    components,
                    spend_work=match_work.spend,
                ):
                    diagnostics.append(
                        TreeDiagnostic(
                            code=TREE_FORBIDDEN_CODE,
                            message="tracked path matches a forbidden pattern",
                            path=path,
                            pattern=pattern,
                        )
                    )
                    break
    except TreeMatchWorkLimitError as error:
        raise InputError(
            f"tracked-tree evaluation exceeds the hard {MAX_TREE_MATCH_WORK}-unit match-work limit"
        ) from error

    return TreeReport(
        policy=policy,
        selection=selection,
        diagnostics=tuple(diagnostics),
    )
