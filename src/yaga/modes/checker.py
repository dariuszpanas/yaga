"""Pure evaluation of committed entries against Git-mode policy."""

from __future__ import annotations

from yaga.errors import InputError
from yaga.modes.models import (
    ModePolicy,
    ModeReport,
    ModeSelection,
    _evaluate_mode_policy,
)
from yaga.modes.patterns import MAX_MODE_MATCH_WORK, ModeMatchWorkLimitError


def check_modes(policy: ModePolicy, selection: ModeSelection) -> ModeReport:
    """Evaluate committed entry kinds against first-match mode policy."""
    if not isinstance(policy, ModePolicy):
        raise TypeError("policy must be a ModePolicy")
    if not isinstance(selection, ModeSelection):
        raise TypeError("selection must be a ModeSelection")
    try:
        evaluation = _evaluate_mode_policy(
            policy,
            selection,
            match_work_limit=MAX_MODE_MATCH_WORK,
        )
    except ModeMatchWorkLimitError as error:
        raise InputError(
            f"mode evaluation exceeds the hard {MAX_MODE_MATCH_WORK}-unit match-work limit"
        ) from error
    return ModeReport._from_evaluation(evaluation)
