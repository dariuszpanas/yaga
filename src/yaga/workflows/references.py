"""Lexical policy for immutable GitHub workflow ``uses`` references."""

from __future__ import annotations

import re
import unicodedata

from yaga.workflows.models import ReferenceContext, WorkflowDiagnostic, WorkflowReference

MAX_USES_LENGTH = 1024

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DOCKER_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_.+-]+\Z")
_REPOSITORY_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_MUTABLE_REF = re.compile(r"[A-Za-z0-9._+/-]+\Z")
_DOCKER_IMAGE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:/+-]*[A-Za-z0-9])?\Z")
_DOCKER_DIGEST_CANDIDATE = re.compile(r"[A-Za-z0-9:._+-]+\Z")
_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml\Z")


def check_reference(reference: WorkflowReference) -> WorkflowDiagnostic | None:
    """Return the first stable policy diagnostic for one ``uses`` reference."""
    value = reference.value
    if _invalid_common_syntax(value):
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")

    if value.startswith("docker://"):
        return _check_docker_reference(reference)
    if value.startswith(("./", "$/")):
        return _check_local_reference(reference)
    return _check_external_reference(reference)


def _check_local_reference(reference: WorkflowReference) -> WorkflowDiagnostic | None:
    value = reference.value
    if "@" in value:
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")

    relative = value[2:]
    if relative and not _safe_path(relative):
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")

    if reference.context is ReferenceContext.STEP:
        return None
    if relative and _is_direct_workflow_path(relative):
        return None
    return _diagnostic(
        reference,
        "uses.context",
        "uses reference is not permitted in this workflow context",
    )


def _check_external_reference(reference: WorkflowReference) -> WorkflowDiagnostic | None:
    value = reference.value
    if "@" in value:
        path, revision = value.split("@", 1)
        if not revision or not _safe_mutable_ref(revision):
            return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")
    else:
        path = value
        revision = None

    if not _safe_path(path):
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")
    segments = path.split("/")
    if len(segments) < 2 or any(
        _REPOSITORY_SEGMENT.fullmatch(segment) is None for segment in segments[:2]
    ):
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")

    reusable_workflow = _is_external_workflow_path(segments)
    workflow_namespace = len(segments) >= 4 and segments[2:4] == [".github", "workflows"]

    if reference.context is ReferenceContext.STEP and workflow_namespace:
        return _diagnostic(
            reference,
            "uses.context",
            "uses reference is not permitted in this workflow context",
        )
    if reference.context is ReferenceContext.JOB and not reusable_workflow:
        return _diagnostic(
            reference,
            "uses.context",
            "uses reference is not permitted in this workflow context",
        )
    if revision is None or _SHA.fullmatch(revision) is None:
        return _diagnostic(
            reference,
            "uses.pin",
            "uses reference must use an immutable lowercase digest",
        )
    return None


def _check_docker_reference(reference: WorkflowReference) -> WorkflowDiagnostic | None:
    payload = reference.value.removeprefix("docker://")
    if "@" in payload:
        image, digest = payload.split("@", 1)
        if not digest or _DOCKER_DIGEST_CANDIDATE.fullmatch(digest) is None:
            return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")
    else:
        image = payload
        digest = None

    if not _safe_docker_image(image):
        return _diagnostic(reference, "uses.syntax", "uses reference has invalid syntax")
    if reference.context is ReferenceContext.JOB:
        return _diagnostic(
            reference,
            "uses.context",
            "uses reference is not permitted in this workflow context",
        )
    if digest is None or _DOCKER_DIGEST.fullmatch(digest) is None:
        return _diagnostic(
            reference,
            "uses.pin",
            "uses reference must use an immutable lowercase digest",
        )
    return None


def _invalid_common_syntax(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return (
        not value
        or len(encoded) > MAX_USES_LENGTH
        or "\\" in value
        or "${{" in value
        or "}}" in value
        or value.count("@") > 1
        or any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )
    )


def _safe_path(value: str) -> bool:
    segments = value.split("/")
    return all(
        segment not in {"", ".", ".."} and _PATH_SEGMENT.fullmatch(segment) is not None
        for segment in segments
    )


def _safe_mutable_ref(value: str) -> bool:
    return _MUTABLE_REF.fullmatch(value) is not None and all(
        segment not in {"", ".", ".."} for segment in value.split("/")
    )


def _safe_docker_image(value: str) -> bool:
    return _DOCKER_IMAGE.fullmatch(value) is not None and all(
        segment not in {"", ".", ".."} for segment in value.split("/")
    )


def _is_direct_workflow_path(value: str) -> bool:
    segments = value.split("/")
    return (
        len(segments) == 3
        and segments[:2] == [".github", "workflows"]
        and _WORKFLOW_FILE.fullmatch(segments[2]) is not None
    )


def _is_external_workflow_path(segments: list[str]) -> bool:
    return (
        len(segments) == 5
        and segments[2:4] == [".github", "workflows"]
        and _WORKFLOW_FILE.fullmatch(segments[4]) is not None
    )


def _diagnostic(
    reference: WorkflowReference,
    code: str,
    message: str,
) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(
        code=code,
        message=message,
        line=reference.line,
        column=reference.column,
    )
