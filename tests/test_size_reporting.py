"""Tests for bounded committed blob-size text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.sizes import patterns
from yaga.sizes.checker import check_sizes
from yaga.sizes.models import (
    SIZE_BLOB_CODE,
    SIZE_TOTAL_CODE,
    BlobEntry,
    SizePathLimit,
    SizePolicy,
    SizeSelection,
)
from yaga.sizes.reporting import (
    MAX_GITHUB_ANNOTATIONS,
    MAX_JSON_DIAGNOSTICS,
    MAX_JSON_LARGEST,
    MAX_TEXT_DIAGNOSTICS,
    SizeOutputFormat,
    render_size_error,
    render_size_report,
)
from yaga.sizes.service import CheckedSize


def _checked(
    tmp_path: Path,
    *,
    blobs: tuple[BlobEntry, ...],
    policy: SizePolicy,
    gitlinks: tuple[str, ...] = (),
) -> CheckedSize:
    selection = SizeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        blobs=tuple(sorted(blobs, key=lambda item: item.path)),
        gitlinks=gitlinks,
    )
    return CheckedSize(
        report=check_sizes(policy, selection),
        policy_path=(tmp_path / "size-policy.toml").resolve(),
    )


def test_json_pass_report_has_stable_sections_and_largest_override(
    tmp_path: Path,
) -> None:
    checked = _checked(
        tmp_path,
        policy=SizePolicy(1, 100, 1_000, (SizePathLimit("uv.lock", 500),)),
        blobs=(
            BlobEntry("README.md", "c" * 40, "100644", 20),
            BlobEntry("uv.lock", "d" * 40, "100644", 300),
        ),
        gitlinks=("vendor/submodule",),
    )

    document = json.loads(render_size_report(checked, SizeOutputFormat.JSON))

    assert list(document) == [
        "schema_version",
        "kind",
        "status",
        "valid",
        "identity",
        "policy",
        "counts",
        "total",
        "largest",
        "diagnostics",
        "diagnostics_omitted",
    ]
    assert document["schema_version"] == 1
    assert document["kind"] == "size_policy"
    assert document["status"] == "passed"
    assert document["valid"] is True
    assert document["identity"] == {
        "policy_path": str((tmp_path / "size-policy.toml").resolve()),
        "repository_path": str((tmp_path / "repository").resolve()),
        "revision": "release-candidate",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    assert document["policy"] == {
        "size_policy_version": 1,
        "default_max_blob_bytes": 100,
        "max_total_blob_bytes": 1_000,
        "path_limits": [{"pattern": "uv.lock", "max_blob_bytes": 500}],
    }
    assert document["counts"] == {"blobs": 2, "gitlinks": 1, "oversized_blobs": 0}
    assert document["total"] == {
        "blob_bytes": 320,
        "max_total_blob_bytes": 1_000,
        "exceeded": False,
    }
    assert document["largest"] == {
        "blobs": [
            {
                "path": "uv.lock",
                "oid": "d" * 40,
                "mode": "100644",
                "size_bytes": 300,
                "max_blob_bytes": 500,
                "pattern": "uv.lock",
            },
            {
                "path": "README.md",
                "oid": "c" * 40,
                "mode": "100644",
                "size_bytes": 20,
                "max_blob_bytes": 100,
                "pattern": None,
            },
        ],
        "omitted": 0,
    }
    assert document["diagnostics"] == []
    assert document["diagnostics_omitted"] == 0


def test_json_preserves_exact_counts_while_bounding_previews(tmp_path: Path) -> None:
    blobs = tuple(
        BlobEntry(f"file-{index:03}.bin", f"{index + 1:040x}", "100644", index + 1)
        for index in range(300)
    )
    checked = _checked(tmp_path, policy=SizePolicy(1, 0), blobs=blobs)

    document = json.loads(render_size_report(checked, SizeOutputFormat.JSON))

    assert document["counts"]["blobs"] == 300
    assert document["counts"]["oversized_blobs"] == 300
    assert document["total"]["blob_bytes"] == sum(range(1, 301))
    assert len(document["largest"]["blobs"]) == MAX_JSON_LARGEST
    assert document["largest"]["omitted"] == 300 - MAX_JSON_LARGEST
    assert document["largest"]["blobs"][0]["path"] == "file-299.bin"
    assert len(document["diagnostics"]) == MAX_JSON_DIAGNOSTICS
    assert document["diagnostics_omitted"] == 300 - MAX_JSON_DIAGNOSTICS


def test_json_truncation_reserves_trailing_total_diagnostic(tmp_path: Path) -> None:
    blobs = tuple(
        BlobEntry(f"file-{index:03}.bin", f"{index + 1:040x}", "100644", 1) for index in range(300)
    )
    checked = _checked(tmp_path, policy=SizePolicy(1, 0, 0), blobs=blobs)

    document = json.loads(render_size_report(checked, SizeOutputFormat.JSON))

    assert len(document["diagnostics"]) == MAX_JSON_DIAGNOSTICS
    assert document["diagnostics"][-1]["code"] == SIZE_TOTAL_CODE
    assert document["diagnostics_omitted"] == 301 - MAX_JSON_DIAGNOSTICS


def test_reporting_uses_cached_first_match_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(
        tmp_path,
        policy=SizePolicy(1, 100, None, (SizePathLimit("assets/**", 500),)),
        blobs=(BlobEntry("assets/app.bin", "c" * 40, "100644", 300),),
    )
    monkeypatch.setattr(
        patterns,
        "_match_compiled_size_components",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rematched")),
    )

    document = json.loads(render_size_report(checked, SizeOutputFormat.JSON))

    assert document["largest"]["blobs"][0]["max_blob_bytes"] == 500
    assert document["largest"]["blobs"][0]["pattern"] == "assets/**"


def test_text_report_bounds_findings_and_preserves_exact_summary(tmp_path: Path) -> None:
    blobs = tuple(
        BlobEntry(f"file-{index:02}.bin", f"{index + 1:040x}", "100644", 1) for index in range(10)
    )
    checked = _checked(tmp_path, policy=SizePolicy(1, 0), blobs=blobs)

    rendered = render_size_report(checked, SizeOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'size-policy.toml').resolve()}\n"
        f"Repository: {(tmp_path / 'repository').resolve()}\n"
        "Revision: release-candidate\n"
    )
    assert rendered.count("FAILED  [size.blob]") == MAX_TEXT_DIAGNOSTICS
    assert "file-07.bin" in rendered
    assert "file-08.bin" not in rendered
    assert "... 2 additional diagnostic(s) omitted" in rendered
    assert rendered.endswith(
        "Size policy: 10 blob(s), 0 gitlink(s); 10 byte(s); "
        "10 oversized blob(s); total limit not configured."
    )


def test_github_blob_uses_file_and_total_has_no_file_with_escaping(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        policy=SizePolicy(1, 1, 1),
        blobs=(BlobEntry("bad%,:file.bin", "c" * 40, "100644", 2),),
    )

    blob, total, summary = render_size_report(checked, SizeOutputFormat.GITHUB).splitlines()

    assert blob.startswith("::error file=bad%25%2C%3Afile.bin,title=YAGA size.blob::")
    assert total.startswith("::error title=YAGA size.total::")
    assert "file=" not in total
    assert summary == (
        "YAGA size policy: 1 blob(s), 0 gitlink(s); 2 byte(s); "
        "1 oversized blob(s); total limit exceeded."
    )


def test_github_report_reserves_aggregate_and_omission_annotations(
    tmp_path: Path,
) -> None:
    blobs = tuple(
        BlobEntry(f"file-{index:02}.bin", f"{index + 1:040x}", "100644", 1) for index in range(40)
    )
    checked = _checked(tmp_path, policy=SizePolicy(1, 0, 1), blobs=blobs)

    annotations = [
        line
        for line in render_size_report(checked, SizeOutputFormat.GITHUB).splitlines()
        if line.startswith("::")
    ]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert annotations[-2].startswith("::error title=YAGA size.total::")
    assert annotations[-1].startswith("::error title=YAGA size policy::")
    assert "10 additional" in annotations[-1]


@pytest.mark.parametrize("output_format", list(SizeOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: SizeOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1_000)

    rendered = render_size_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is SizeOutputFormat.JSON:
        document = json.loads(rendered)
        assert document["kind"] == "size_policy"
        assert document["status"] == "error"
        assert document["error"]["kind"] == "configuration"
        assert len(document["error"]["message"]) <= 500
    elif output_format is SizeOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA size policy::")
        assert "%25" in rendered
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")


def test_diagnostics_report_selected_override_and_aggregate_shapes(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        policy=SizePolicy(1, 100, 10, (SizePathLimit("assets/**", 5),)),
        blobs=(BlobEntry("assets/app.bin", "c" * 40, "100644", 11),),
    )

    diagnostics = json.loads(render_size_report(checked, SizeOutputFormat.JSON))["diagnostics"]

    assert diagnostics == [
        {
            "code": SIZE_BLOB_CODE,
            "message": "committed blob exceeds its configured byte limit",
            "path": "assets/app.bin",
            "size_bytes": 11,
            "max_bytes": 5,
            "pattern": "assets/**",
        },
        {
            "code": SIZE_TOTAL_CODE,
            "message": "total stored blob bytes exceed the configured limit",
            "path": None,
            "size_bytes": 11,
            "max_bytes": 10,
            "pattern": None,
        },
    ]
