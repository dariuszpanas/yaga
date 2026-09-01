"""Tests for lexical GitHub workflow ``uses`` policy."""

from __future__ import annotations

import pytest

from yaga.workflows.models import (
    ImageReferenceContext,
    ReferenceContext,
    WorkflowImageReference,
    WorkflowReference,
)
from yaga.workflows.references import MAX_USES_LENGTH, check_image_reference, check_reference

SHA = "a" * 40
DIGEST = "b" * 64
STRING_TAG = "tag:yaml.org,2002:str"


def reference(value: str, context: ReferenceContext = ReferenceContext.STEP) -> WorkflowReference:
    return WorkflowReference(value=value, context=context, line=17, column=9)


def image_reference(
    value: str,
    context: ImageReferenceContext = ImageReferenceContext.JOB,
    tag: str = STRING_TAG,
    style: str | None = None,
) -> WorkflowImageReference:
    return WorkflowImageReference(
        value=value,
        context=context,
        tag=tag,
        line=23,
        column=13,
        style=style,
    )


@pytest.mark.parametrize(
    "value",
    [
        "./",
        "$/",
        "./action",
        "$/actions/check",
        "./.github/actions/private-check",
        "actions/checkout@" + SHA,
        "owner/repository/subdirectory/action@" + SHA,
        "docker://a@sha256:" + DIGEST,
        "docker://alpine@sha256:" + DIGEST,
        "docker://ghcr.io/owner/image:latest@sha256:" + DIGEST,
        "docker://EXAMPLE.COM/owner/image:RC_1@sha256:" + DIGEST,
        "docker://Example/owner/image@sha256:" + DIGEST,
        "docker://[2001:db8::1]:5000/owner/image@sha256:" + DIGEST,
        "./" + "a" * (MAX_USES_LENGTH - 2),
    ],
)
def test_step_accepts_local_external_and_digest_pinned_docker_actions(value: str) -> None:
    assert check_reference(reference(value)) is None


@pytest.mark.parametrize(
    "value",
    [
        "./.github/workflows/reuse.yml",
        "$/.github/workflows/reuse.yaml",
        "owner/repository/.github/workflows/reuse.yml@" + SHA,
        "owner/repository/.github/workflows/reuse.yaml@" + SHA,
    ],
)
def test_job_accepts_only_direct_reusable_workflow_files(value: str) -> None:
    assert check_reference(reference(value, ReferenceContext.JOB)) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "./action path",
        "./action\tpath",
        "./action\npath",
        "./action\x00path",
        "./action\u202epath",
        "./action\ud800path",
        "${{github.repository}}/action@" + SHA,
        "./${{matrix.action}}",
        "./action\\windows",
        "owner/repository@" + SHA + "@other",
        "./action@" + SHA,
        "./action/",
        "./action//nested",
        "./action/./nested",
        "./action/../nested",
        "$/../action",
        "/absolute/action",
        "owner//repository@" + SHA,
        "owner/./repository@" + SHA,
        "owner/../repository@" + SHA,
        "owner+/repository@" + SHA,
        "owner/repository+invalid@" + SHA,
        "owner/repository?query@" + SHA,
        "owner@" + SHA,
        "owner/repository@",
        "owner/repository@refs//main",
        "owner/repository@refs/../main",
        "docker://",
        "docker:///image@sha256:" + DIGEST,
        "docker://owner//image@sha256:" + DIGEST,
        "docker://owner/../image@sha256:" + DIGEST,
        "docker://image@sha256:" + DIGEST + "/suffix",
        "docker://repo::tag@sha256:" + DIGEST,
        "docker://Owner/Image@sha256:" + DIGEST,
        "docker://owner/_image@sha256:" + DIGEST,
        "./" + "a" * (MAX_USES_LENGTH - 1),
    ],
)
def test_malformed_or_unsafe_references_have_one_syntax_diagnostic(value: str) -> None:
    diagnostic = check_reference(reference(value))

    assert diagnostic is not None
    assert diagnostic.code == "uses.syntax"
    assert diagnostic.message == "uses reference has invalid syntax"
    assert (diagnostic.line, diagnostic.column) == (17, 9)


@pytest.mark.parametrize(
    "value",
    [
        "actions/checkout",
        "actions/checkout@v4",
        "actions/checkout@" + "A" * 40,
        "actions/checkout@" + "a" * 39,
        "actions/checkout@" + "a" * 41,
        "owner/repository/action@feature/branch",
        "docker://alpine",
        "docker://alpine@latest",
        "docker://alpine@SHA256:" + DIGEST,
        "docker://alpine@sha256:" + "B" * 64,
        "docker://alpine@sha512:" + DIGEST,
    ],
)
def test_step_mutable_or_noncanonical_revisions_have_one_pin_diagnostic(value: str) -> None:
    diagnostic = check_reference(reference(value))

    assert diagnostic is not None
    assert diagnostic.code == "uses.pin"
    assert "immutable lowercase digest" in diagnostic.message
    assert (diagnostic.line, diagnostic.column) == (17, 9)


@pytest.mark.parametrize(
    "value",
    [
        "owner/repository/.github/workflows/reuse.yml",
        "owner/repository/.github/workflows/reuse.yml@main",
        "owner/repository/.github/workflows/reuse.yml@" + "A" * 40,
    ],
)
def test_job_reusable_workflows_require_a_lowercase_full_sha(value: str) -> None:
    diagnostic = check_reference(reference(value, ReferenceContext.JOB))

    assert diagnostic is not None
    assert diagnostic.code == "uses.pin"


@pytest.mark.parametrize(
    ("value", "context"),
    [
        ("./", ReferenceContext.JOB),
        ("$/", ReferenceContext.JOB),
        ("./action", ReferenceContext.JOB),
        ("$/actions/check", ReferenceContext.JOB),
        ("./.github/workflows/nested/reuse.yml", ReferenceContext.JOB),
        ("actions/checkout@" + SHA, ReferenceContext.JOB),
        ("actions/checkout@main", ReferenceContext.JOB),
        ("owner/repository/action@" + SHA, ReferenceContext.JOB),
        ("docker://alpine@sha256:" + DIGEST, ReferenceContext.JOB),
        ("docker://alpine@latest", ReferenceContext.JOB),
        ("owner/repository/.github/workflows/reuse.yml@" + SHA, ReferenceContext.STEP),
        ("owner/repository/.github/workflows/reuse.yml@main", ReferenceContext.STEP),
        (
            "owner/repository/.github/workflows/nested/reuse.yml@" + SHA,
            ReferenceContext.STEP,
        ),
    ],
)
def test_valid_references_in_the_wrong_context_have_one_context_diagnostic(
    value: str,
    context: ReferenceContext,
) -> None:
    diagnostic = check_reference(reference(value, context))

    assert diagnostic is not None
    assert diagnostic.code == "uses.context"
    assert diagnostic.message == "uses reference is not permitted in this workflow context"
    assert (diagnostic.line, diagnostic.column) == (17, 9)


def test_local_workflow_shaped_path_remains_a_valid_step_action_path() -> None:
    assert check_reference(reference("./.github/workflows/reuse.yml")) is None


def test_diagnostics_never_echo_the_raw_reference() -> None:
    raw = "secret-value@mutable"

    diagnostic = check_reference(reference(raw))

    assert diagnostic is not None
    assert raw not in diagnostic.message


@pytest.mark.parametrize(
    "value",
    [
        "alpine@sha256:" + DIGEST,
        "alpine:3.20@sha256:" + DIGEST,
        "ghcr.io/owner/image:stable@sha256:" + DIGEST,
        "localhost:5000/owner/image@sha256:" + DIGEST,
        "docker.io/library/image_name:RC_1@sha256:" + DIGEST,
        "registry.example.com/owner/image__part@sha256:" + DIGEST,
        "EXAMPLE.COM/owner/image:RC_1@sha256:" + DIGEST,
        "Example/owner/image@sha256:" + DIGEST,
        "LOCALHOST:5000/owner/image@sha256:" + DIGEST,
        "[2001:db8::1]:5000/owner/image@sha256:" + DIGEST,
    ],
)
@pytest.mark.parametrize("context", list(ImageReferenceContext))
def test_container_images_accept_exact_lowercase_sha256_digests(
    value: str,
    context: ImageReferenceContext,
) -> None:
    assert check_image_reference(image_reference(value, context)) is None


def test_service_container_accepts_a_literal_empty_string() -> None:
    assert (
        check_image_reference(
            image_reference("", ImageReferenceContext.SERVICE, STRING_TAG, style='"')
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "context", "tag"),
    [
        ("", ImageReferenceContext.JOB, STRING_TAG),
        ("", ImageReferenceContext.SERVICE, "tag:yaml.org,2002:null"),
        ("", ImageReferenceContext.SERVICE, STRING_TAG),
        ("null", ImageReferenceContext.SERVICE, "tag:yaml.org,2002:null"),
        (
            "123@sha256:" + DIGEST,
            ImageReferenceContext.JOB,
            "tag:yaml.org,2002:int",
        ),
        ("${{ matrix.image }}", ImageReferenceContext.JOB, STRING_TAG),
        ("image path@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("owner//image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("owner/../image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("image@sha256:" + DIGEST + "/suffix", ImageReferenceContext.JOB, STRING_TAG),
        ("image@@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("docker://image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("repo::tag@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("Owner/Image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("owner/_image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        (
            "owner/image:" + "a" * 129 + "@sha256:" + DIGEST,
            ImageReferenceContext.JOB,
            STRING_TAG,
        ),
        ("registry:port/image@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("owner/image+part@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("a" * 256 + "@sha256:" + DIGEST, ImageReferenceContext.JOB, STRING_TAG),
        ("a" * (MAX_USES_LENGTH + 1), ImageReferenceContext.JOB, STRING_TAG),
    ],
)
def test_malformed_or_nonliteral_container_images_have_one_syntax_diagnostic(
    value: str,
    context: ImageReferenceContext,
    tag: str,
) -> None:
    diagnostic = check_image_reference(image_reference(value, context, tag))

    assert diagnostic is not None
    assert diagnostic.code == "image.syntax"
    assert diagnostic.message == "container image has invalid syntax"
    assert (diagnostic.line, diagnostic.column) == (23, 13)


@pytest.mark.parametrize("style", ["|", ">"])
def test_service_container_rejects_empty_block_scalars(style: str) -> None:
    diagnostic = check_image_reference(
        image_reference("", ImageReferenceContext.SERVICE, STRING_TAG, style=style)
    )

    assert diagnostic is not None
    assert diagnostic.code == "image.syntax"


@pytest.mark.parametrize(
    "value",
    [
        "alpine",
        "alpine:latest",
        "alpine@latest",
        "alpine@SHA256:" + DIGEST,
        "alpine@sha256:" + "B" * 64,
        "alpine@sha256:" + "b" * 63,
        "alpine@sha256:" + "b" * 65,
        "alpine@sha512:" + DIGEST,
    ],
)
def test_mutable_or_noncanonical_container_images_have_one_pin_diagnostic(
    value: str,
) -> None:
    diagnostic = check_image_reference(image_reference(value))

    assert diagnostic is not None
    assert diagnostic.code == "image.pin"
    assert "immutable lowercase digest" in diagnostic.message
    assert (diagnostic.line, diagnostic.column) == (23, 13)


def test_container_image_diagnostics_never_echo_the_raw_reference() -> None:
    raw = "secret-value@mutable"

    diagnostic = check_image_reference(image_reference(raw))

    assert diagnostic is not None
    assert raw not in diagnostic.message
