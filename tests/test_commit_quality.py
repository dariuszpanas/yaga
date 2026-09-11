"""Tests for the optional local commit-quality model adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yaga.commits.models import CommitTarget, OutputFormat
from yaga.commits.quality import (
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MODEL_REVISION,
    MAX_MODEL_INPUT_CHARS,
    QualityAssessment,
    QualityPrediction,
    QualityReport,
    QualityResult,
    _input_truncation,
    _load_huggingface,
    _low_quality_probability,
    _model_input,
    _parse_decision,
    check_quality,
)
from yaga.commits.reporting import (
    QualityOutputFormat,
    quality_report_document,
    render_quality_error,
    render_quality_report,
)
from yaga.errors import InputError


def test_quality_extracts_label_zero_probability() -> None:
    assert (
        _low_quality_probability(
            [{"label": "LABEL_1", "score": 0.2}, {"label": "LABEL_0", "score": 0.8}]
        )
        == 0.8
    )
    assert (
        _low_quality_probability(
            [[{"label": "LABEL_1", "score": 0.2}, {"label": "LABEL_0", "score": 0.8}]]
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
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("flag", 0.81),
    )
    report = check_quality(
        [CommitTarget(label="message", message="feat: a message")],
        revision=DEFAULT_MODEL_REVISION,
    )
    assert report.flagged == 1
    assert report.results[0].assessment.score == 0.81
    assert report.max_input_tokens == DEFAULT_MAX_INPUT_TOKENS


@pytest.mark.parametrize(
    ("text", "decision", "reason"),
    [("PASS clear change", "pass", "clear change"), ("FLAG vague", "flag", "vague")],
)
def test_quality_parses_bounded_provider_decisions(text: str, decision: str, reason: str) -> None:
    assessment = _parse_decision(text)
    assert assessment.decision == decision
    assert assessment.reason == reason


def test_quality_rejects_untrusted_provider_decision() -> None:
    with pytest.raises(ValueError, match="expected PASS"):
        _parse_decision("maybe")


def test_unknown_model_input_coverage_is_not_reported_as_complete() -> None:
    assert _input_truncation(None, 512) is None
    assert _input_truncation(512, 512) is False
    assert _input_truncation(513, 512) is True


def test_quality_input_mode_selects_title_or_complete_message() -> None:
    message = "fix: parser\n\nExplain the parser behavior."

    assert _model_input(message, "message") == message
    assert _model_input(message, "title") == "fix: parser"


def test_quality_model_input_is_bounded_for_both_modes() -> None:
    message = "fix: parser\n\n" + ("x" * (MAX_MODEL_INPUT_CHARS + 100))

    assert len(_model_input(message, "message")) == MAX_MODEL_INPUT_CHARS
    assert len(_model_input(message, "title")) == len("fix: parser")


def test_classifier_applies_offline_only_during_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Loader:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            calls.append(("load", dict(kwargs)))
            return object()

    def pipeline(*args: object, **kwargs: object):
        calls.append(("pipeline", dict(kwargs)))
        return lambda _message: [
            {"label": "LABEL_0", "score": 0.8},
            {"label": "LABEL_1", "score": 0.2},
        ]

    monkeypatch.setattr(
        "yaga.commits.quality.import_module",
        lambda _name: SimpleNamespace(
            AutoTokenizer=Loader,
            AutoModelForSequenceClassification=Loader,
            pipeline=pipeline,
        ),
    )

    predictor = _load_huggingface(
        "classification",
        "model",
        DEFAULT_MODEL_REVISION,
        0.7,
        True,
        32,
    )

    assert predictor("feat: message").flagged
    assert [kwargs["local_files_only"] for kind, kwargs in calls if kind == "load"] == [True, True]
    assert "local_files_only" not in next(kwargs for kind, kwargs in calls if kind == "pipeline")


def test_classifier_receives_the_configured_input_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class Loader:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            return object()

    def pipeline(*args: object, **kwargs: object):
        calls.append(dict(kwargs))
        return lambda _message: [{"label": "LABEL_0", "score": 0.1}]

    monkeypatch.setattr(
        "yaga.commits.quality.import_module",
        lambda _name: SimpleNamespace(
            AutoTokenizer=Loader,
            AutoModelForSequenceClassification=Loader,
            pipeline=pipeline,
        ),
    )

    predictor = _load_huggingface(
        "classification",
        "model",
        DEFAULT_MODEL_REVISION,
        0.7,
        True,
        32,
        1024,
    )

    assert predictor("feat: message").decision == "pass"
    assert calls[0]["max_length"] == 1024


def test_classifier_reports_when_the_input_exceeds_the_configured_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tokenizer:
        def __call__(self, text: str, **kwargs: object) -> dict[str, list[int]]:
            if kwargs.get("truncation") is False:
                return {"input_ids": list(range(9))}
            return {"input_ids": list(range(4))}

    tokenizer = Tokenizer()

    class Loader:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            return tokenizer

    def pipeline(*args: object, **kwargs: object):
        return lambda _message: [{"label": "LABEL_0", "score": 0.1}]

    monkeypatch.setattr(
        "yaga.commits.quality.import_module",
        lambda _name: SimpleNamespace(
            AutoTokenizer=Loader,
            AutoModelForSequenceClassification=Loader,
            pipeline=pipeline,
        ),
    )

    predictor = _load_huggingface(
        "classification",
        "model",
        DEFAULT_MODEL_REVISION,
        0.7,
        True,
        32,
        8,
    )

    prediction = predictor("feat: message")
    assert isinstance(prediction, QualityPrediction)
    assert prediction.input_truncated is True
    assert prediction.input_tokens == 9


def test_classifier_receives_title_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []

    class Loader:
        @staticmethod
        def from_pretrained(*args: object, **kwargs: object) -> object:
            return SimpleNamespace()

    def pipeline(*args: object, **kwargs: object):
        def classify(message: str) -> list[dict[str, object]]:
            received.append(message)
            return [{"label": "LABEL_0", "score": 0.1}]

        return classify

    monkeypatch.setattr(
        "yaga.commits.quality.import_module",
        lambda _name: SimpleNamespace(
            AutoTokenizer=Loader,
            AutoModelForSequenceClassification=Loader,
            pipeline=pipeline,
        ),
    )

    predictor = _load_huggingface(
        "classification",
        "model",
        DEFAULT_MODEL_REVISION,
        0.7,
        True,
        32,
        input_mode="title",
    )

    assert predictor("fix: parser\n\nExplain the parser behavior.").decision == "pass"
    assert received == ["fix: parser"]


def test_quality_rejects_unpinned_revision() -> None:
    with pytest.raises(InputError, match="lowercase hexadecimal"):
        check_quality([], revision="main")


def test_bedrock_rejects_the_unsupported_seq2seq_task() -> None:
    with pytest.raises(InputError, match="Bedrock quality supports only the classification task"):
        check_quality(
            [],
            provider="bedrock",
            task="seq2seq",
            model_id="amazon.nova-micro-v1:0",
            revision=None,
        )


def test_bedrock_does_not_inherit_a_huggingface_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("pass", 0.1),
    )
    report = check_quality(
        [],
        provider="bedrock",
        model_id="amazon.nova-micro-v1:0",
    )
    assert report.revision is None


def test_bedrock_rejects_an_explicit_model_revision() -> None:
    with pytest.raises(InputError, match="does not use model revisions"):
        check_quality(
            [],
            provider="bedrock",
            model_id="amazon.nova-micro-v1:0",
            revision=DEFAULT_MODEL_REVISION,
        )


def test_quality_reports_that_the_complete_multiline_message_was_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("pass", 0.1),
    )
    report = check_quality(
        [
            CommitTarget(
                label="commit abc123",
                sha="abc123",
                message="fix: parser\n\nExplain the parser behavior.",
            )
        ],
        revision=DEFAULT_MODEL_REVISION,
    )
    document = quality_report_document(report)
    assert document["max_input_tokens"] == DEFAULT_MAX_INPUT_TOKENS
    assert document["commits"][0]["model_input_truncated"] is None
    assert document["commits"][0]["model_input_tokens"] is None
    assert document["commits"][0]["message_lines"] == 3
    assert document["commits"][0]["message_body_lines"] == 1
    assert document["commits"][0]["model_input_characters"] == len(
        "fix: parser\n\nExplain the parser behavior."
    )
    assert document["commits"][0]["model_input_character_truncated"] is False
    rendered = render_quality_report(report, OutputFormat.TEXT)
    assert "(3 lines checked; 1 body line included)" in rendered


def test_quality_report_identifies_title_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("pass", 0.1),
    )
    report = check_quality(
        [CommitTarget(label="message", message="fix: parser\n\nExplain the parser behavior.")],
        revision=DEFAULT_MODEL_REVISION,
        input_mode="title",
    )

    document = quality_report_document(report)
    assert document["input_mode"] == "title"
    assert "[model input: title only]" in render_quality_report(report, OutputFormat.TEXT)


def test_quality_report_makes_title_only_input_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("pass", 0.1),
    )
    report = check_quality(
        [CommitTarget(label="message", message="fix: parser")],
        revision=DEFAULT_MODEL_REVISION,
    )

    document = quality_report_document(report)
    assert document["commits"][0]["message_body_lines"] == 0
    assert "no body included" in render_quality_report(report, OutputFormat.TEXT)


def test_quality_github_report_uses_bounded_warning_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: (
            lambda _message: QualityAssessment("flag", 0.91, "Needs 100%\nmore detail")
        ),
    )
    report = check_quality(
        [CommitTarget(label="message", message="fix: vague change")],
        revision=DEFAULT_MODEL_REVISION,
    )

    rendered = render_quality_report(report, QualityOutputFormat.GITHUB)

    assert (
        "::warning title=YAGA commit quality::message: fix: vague change; score 0.910; Needs 100%25?more detail"
        in rendered
    )
    assert "YAGA quality checked 1 commit(s): 0 passed, 1 flagged." in rendered
    assert "Input: message" in rendered
    assert (
        "::notice title=YAGA commit quality::YAGA quality checked 1 commit(s): 0 passed, 1 flagged."
        " Provider: huggingface; Task: classification; Model: saridormi/commit-message-quality-codebert"
        in rendered
    )


def test_quality_github_error_is_an_error_annotation() -> None:
    rendered = render_quality_error(InputError("bad\nprovider"), QualityOutputFormat.GITHUB)

    assert rendered == "::error title=YAGA commit quality::bad?provider"


def test_quality_github_report_bounds_warning_annotations() -> None:
    report = QualityReport(
        results=tuple(
            QualityResult(
                CommitTarget(label=f"message-{index}", message="fix: vague change"),
                QualityAssessment("flag", 0.9),
                0.7,
            )
            for index in range(51)
        ),
        provider="huggingface",
        task="classification",
        model_id="model",
        revision=None,
        offline=False,
        region=None,
        max_input_tokens=512,
    )

    rendered = render_quality_report(report, QualityOutputFormat.GITHUB)

    assert rendered.count("::warning title=YAGA commit quality::") == 51
    assert "1 additional finding(s) omitted" in rendered


def test_quality_report_exposes_input_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: (
            lambda _message: QualityPrediction(QualityAssessment("pass", 0.1), True, 513)
        ),
    )
    report = check_quality(
        [CommitTarget(label="message", message="fix: parser")],
        revision=DEFAULT_MODEL_REVISION,
    )

    document = quality_report_document(report)
    rendered = render_quality_report(report, OutputFormat.TEXT)
    assert document["commits"][0]["model_input_truncated"] is True
    assert document["commits"][0]["model_input_tokens"] == 513
    assert "model input 513/512 tokens, truncated" in rendered


def test_quality_report_exposes_character_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "yaga.commits.quality._load_predictor",
        lambda *_args, **_kwargs: lambda _message: QualityAssessment("pass", 0.1),
    )
    report = check_quality(
        [
            CommitTarget(
                label="message",
                message="fix: parser\n\n" + ("x" * (MAX_MODEL_INPUT_CHARS + 10)),
            )
        ],
        revision=DEFAULT_MODEL_REVISION,
    )

    result = quality_report_document(report)["commits"][0]
    assert result["model_input_characters"] == MAX_MODEL_INPUT_CHARS
    assert result["model_input_character_truncated"] is True
    assert "character-truncated" in render_quality_report(report, OutputFormat.TEXT)
