"""Pure evaluation of committed blobs against size policy."""

from __future__ import annotations

from yaga.errors import InputError
from yaga.sizes.models import (
    MAX_SIZE_TOTAL_BYTES,
    SizePolicy,
    SizeReport,
    SizeSelection,
    SizeTotalBytesLimitError,
    _evaluate_size_policy,
)
from yaga.sizes.patterns import MAX_SIZE_MATCH_WORK, SizeMatchWorkLimitError


def check_sizes(policy: SizePolicy, selection: SizeSelection) -> SizeReport:
    """Evaluate per-path and aggregate committed blob limits deterministically."""
    if not isinstance(policy, SizePolicy):
        raise TypeError("policy must be a SizePolicy")
    if not isinstance(selection, SizeSelection):
        raise TypeError("selection must be a SizeSelection")

    try:
        evaluation = _evaluate_size_policy(
            policy,
            selection,
            match_work_limit=MAX_SIZE_MATCH_WORK,
        )
    except SizeMatchWorkLimitError as error:
        raise InputError(
            f"blob-size evaluation exceeds the hard {MAX_SIZE_MATCH_WORK}-unit match-work limit"
        ) from error
    except SizeTotalBytesLimitError as error:
        raise InputError(
            f"committed blob total exceeds the hard {MAX_SIZE_TOTAL_BYTES}-byte "
            "portable-integer limit"
        ) from error
    return SizeReport._from_evaluation(evaluation)
