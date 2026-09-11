"""Tests for the optional local commit-quality model adapter."""

from __future__ import annotations

import pytest

from yaga.commits.models import CommitTarget
from yaga.commits.quality import (
    DEFAULT_MODEL_REVISION,
    _low_quality_probability,
    check_quality,
)
from yaga.errors import InputError


def test_quality_extracts_label_zero_probability() -> None:
    assert (
        _low_quality_probability(
            [{"label": "LABEL_1", "score": 0.2}, {"label": "LABEL_0", "score": 0.8}]
        )
        == 0.8
    )


@pytest.mark.parametrize(
    "value",
    [[], [{"label": "LABEL_1", "score": 1.2}], [{"label": "LABEL_1", "score": 1.0}]],
)
def test_quality_rejects_untrusted_model_output(value: object) -> None:
    with pytest.raises(ValueError):
        _low_quality_probability(value)


def test_quality_uses_injected_predictor_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: 0.81,
    )
    report = check_quality(
        [CommitTarget(label="message", message="feat: a message")],
        revision=DEFAULT_MODEL_REVISION,
    )
    assert report.flagged == 1
    assert report.results[0].prediction.low_quality_probability == 0.81


def test_quality_rejects_unpinned_revision() -> None:
    with pytest.raises(InputError, match="lowercase hexadecimal"):
        check_quality([], revision="main")
