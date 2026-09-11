"""Optional local Hugging Face advisory for commit-message quality."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from yaga.commits.models import CommitTarget
from yaga.errors import InputError

DEFAULT_MODEL_ID = "saridormi/commit-message-quality-codebert"
DEFAULT_MODEL_REVISION = "30c7895b3eb0270a3246ef3db7b43c837d8e553a"
DEFAULT_LOW_QUALITY_THRESHOLD = 0.70
MAX_MODEL_ID_LENGTH = 256
MAX_REVISION_LENGTH = 64
MAX_RESULTS = 256


@dataclass(frozen=True, slots=True)
class QualityPrediction:
    """One model prediction, normalized to the model-card label contract."""

    low_quality_probability: float


@dataclass(frozen=True, slots=True)
class QualityResult:
    """Advisory result for one selected commit message."""

    target: CommitTarget
    prediction: QualityPrediction
    threshold: float

    @property
    def flagged(self) -> bool:
        """Return whether the configured low-quality threshold was met."""
        return self.prediction.low_quality_probability >= self.threshold


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Complete model-quality report."""

    results: tuple[QualityResult, ...]
    model_id: str
    revision: str
    offline: bool

    @property
    def flagged(self) -> int:
        """Return the number of advisory findings."""
        return sum(result.flagged for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether no selected message crossed the advisory threshold."""
        return self.flagged == 0


def check_quality(
    targets: list[CommitTarget],
    *,
    model_id: str = DEFAULT_MODEL_ID,
    revision: str = DEFAULT_MODEL_REVISION,
    threshold: float = DEFAULT_LOW_QUALITY_THRESHOLD,
    offline: bool = False,
) -> QualityReport:
    """Run the optional local model against bounded commit messages."""
    _validate_options(model_id, revision, threshold)
    if len(targets) > MAX_RESULTS:
        raise InputError(f"quality check cannot inspect more than {MAX_RESULTS} messages")
    predictor = _load_predictor(model_id, revision, offline=offline)
    results = tuple(
        QualityResult(
            target=target,
            prediction=QualityPrediction(predictor(target.message)),
            threshold=threshold,
        )
        for target in targets
    )
    return QualityReport(
        results=results,
        model_id=model_id,
        revision=revision,
        offline=offline,
    )


def _load_predictor(model_id: str, revision: str, *, offline: bool) -> Any:
    try:
        pipeline = import_module("transformers").pipeline
    except ImportError as error:
        raise InputError(
            "commit quality requires the optional model dependencies; "
            "install them with `uv sync --extra quality`"
        ) from error
    try:
        classifier = pipeline(
            "text-classification",
            model=model_id,
            revision=revision,
            truncation=True,
            max_length=512,
            local_files_only=offline,
            top_k=None,
        )
    except Exception as error:  # noqa: BLE001 - provider failures become bounded CLI errors.
        detail = str(error).replace("\n", " ")[:500]
        raise InputError(f"could not load the quality model: {detail}") from error

    def predict(message: str) -> float:
        try:
            raw = classifier(message)
            return _low_quality_probability(raw)
        except Exception as error:  # noqa: BLE001 - provider output is untrusted.
            detail = str(error).replace("\n", " ")[:500]
            raise InputError(f"quality model returned invalid output: {detail}") from error

    return predict


def _low_quality_probability(raw: object) -> float:
    """Extract the LABEL_0 probability from a transformers pipeline result."""
    if not isinstance(raw, list) or not raw or not all(isinstance(item, dict) for item in raw):
        raise ValueError("expected a non-empty list of score objects")
    scores: dict[str, float] = {}
    for item in raw:
        label = item.get("label")
        score = item.get("score")
        if not isinstance(label, str) or not label or type(score) is not float:
            raise ValueError("score objects must contain a label and float score")
        if not 0.0 <= score <= 1.0:
            raise ValueError("scores must be between zero and one")
        scores[label.casefold()] = score
    try:
        return scores["label_0"]
    except KeyError as error:
        raise ValueError("model must expose the low-quality LABEL_0 score") from error


def _validate_options(model_id: str, revision: str, threshold: float) -> None:
    if not isinstance(model_id, str) or not model_id or len(model_id) > MAX_MODEL_ID_LENGTH:
        raise InputError(f"model identifier must be 1-{MAX_MODEL_ID_LENGTH} characters")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in model_id):
        raise InputError("model identifier must contain printable ASCII only")
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > MAX_REVISION_LENGTH
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise InputError(
            "model revision must be a lowercase hexadecimal SHA of at most "
            f"{MAX_REVISION_LENGTH} characters"
        )
    if type(threshold) is not float or not 0.0 < threshold <= 1.0:
        raise InputError("quality threshold must be a number greater than 0 and at most 1")
