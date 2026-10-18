"""Historical replay retains failures and never turns selections into speedup claims."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yaga.errors import GitError, InputError
from yaga.git import ProcessResult

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/test-planning/replay_django_ray.py"
ADAPTER_SOURCE = """\
from pathlib import Path

def generate(**inputs):
    return {
        "comparison.json": {
            "missing": [], "additional": [], "planned_selected": [],
            "planning_seconds": 0.5,
        },
        "proposed-plan.json": {"plan_digest": "d" * 64},
    }
"""


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def commit(repository: Path) -> tuple[str, str]:
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "--allow-empty", "-m", "test: update adapter")
    return git(repository, "rev-parse", "HEAD"), git(repository, "rev-parse", "HEAD^{tree}")


@pytest.fixture
def consumer(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "consumer"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "YAGA Tests")
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "scripts").mkdir()
    (repository / "scripts/test_planning_shadow.py").write_text(ADAPTER_SOURCE, encoding="utf-8")
    (repository / "README.md").write_text("consumer\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
    head, tree = commit(repository)
    return repository, head, tree


def invoke_main(
    consumer: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path]:
    repository, head, tree = consumer
    cohort = repository.parent / "cohort.json"
    cohort.write_text(json.dumps([row()]), encoding="utf-8")
    output = repository.parent / "replay.json"
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(EXAMPLE),
            "--consumer",
            str(repository),
            "--adapter-commit",
            head,
            "--adapter-tree",
            tree,
            "--cohort",
            str(cohort),
            "--output",
            str(output),
        ],
    )
    namespace = runpy.run_path(str(EXAMPLE))
    return namespace["main"].__globals__, output


def row(number: int = 1) -> dict:
    return {
        "number": number,
        "merged_at": "2026-10-17T19:35:49Z",
        "base": "a" * 40,
        "head": str(number) * 40,
        "tree": "c" * 40,
    }


def test_replay_preserves_omissions_exact_ranges_and_no_runtime_claim() -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    received = []

    def generate(**inputs):
        received.append(inputs)
        return {
            "comparison.json": {
                "missing": ["native"],
                "additional": [],
                "planned_selected": ["application"],
                "planning_seconds": 0.5,
            },
            "proposed-plan.json": {"plan_digest": "d" * 64, "plan": {"reasons": []}},
        }

    document = namespace["replay"](SimpleNamespace(generate=generate), [row()])
    assert received == [
        {
            "revision_range": "a" * 40 + ".." + "1" * 40,
            "execution_commit": "1" * 40,
            "execution_tree": "c" * 40,
        }
    ]
    assert document["observations"][0]["comparison"]["missing"] == ["native"]
    assert document["summary"]["omissions"] == 1
    assert document["summary"]["test_runtime_observations"] == 0
    assert document["summary"]["feedback_time_reduction"] is None
    assert document["activation"] == "disabled"


@pytest.mark.parametrize(
    "cohort",
    [
        [],
        [row(), row()],
        [dict(row(), head="HEAD")],
        [dict(row(), number=True)],
        [dict(row(), base="1" * 40)],
        [dict(row(), unexpected=True)],
        [dict(row(), merged_at="today")],
        [dict(row(), tree="x" * 40)],
    ],
)
def test_replay_rejects_incomplete_ambiguous_or_inferred_cohorts(cohort) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    with pytest.raises(ValueError):
        namespace["validate_cohort"](cohort)


def test_replay_does_not_replace_a_failed_plan_with_an_empty_selection() -> None:
    namespace = runpy.run_path(str(EXAMPLE))

    def generate(**_inputs):
        raise RuntimeError("source collection failed")

    with pytest.raises(RuntimeError, match="source collection failed"):
        namespace["replay"](SimpleNamespace(generate=generate), [row()])


def test_replay_requires_exact_clean_adapter_checkout(consumer: tuple[Path, str, str]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    assert namespace["verify_consumer_checkout"](repository, head, tree) == repository.resolve()


@pytest.mark.parametrize("field", ["commit", "tree"])
def test_replay_rejects_nonexistent_adapter_identity(
    consumer: tuple[Path, str, str], field: str
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    if field == "commit":
        head = "0" * 40
    else:
        tree = "0" * 40
    with pytest.raises((InputError, GitError)):
        namespace["verify_consumer_checkout"](repository, head, tree)


def test_replay_rejects_other_checked_out_commit(consumer: tuple[Path, str, str]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    new_head, _ = commit(repository)
    assert new_head != head
    with pytest.raises(InputError, match="checkout does not match"):
        namespace["verify_consumer_checkout"](repository, head, tree)


def test_replay_rejects_consumer_subdirectory(consumer: tuple[Path, str, str]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    with pytest.raises(InputError, match="root"):
        namespace["verify_consumer_checkout"](repository / "scripts", head, tree)


def test_replay_rejects_bare_repository(consumer: tuple[Path, str, str]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    bare = repository.parent / "bare"
    git(repository, "clone", "--quiet", "--bare", str(repository), str(bare))
    with pytest.raises(InputError, match="root"):
        namespace["verify_consumer_checkout"](bare, head, tree)


@pytest.mark.parametrize("change", ["modified", "staged", "deleted", "untracked"])
def test_replay_rejects_tracked_and_untracked_changes(
    consumer: tuple[Path, str, str], change: str
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    if change in {"modified", "staged"}:
        (repository / "README.md").write_text("changed\n", encoding="utf-8")
        if change == "staged":
            git(repository, "add", "README.md")
    elif change == "deleted":
        (repository / "README.md").unlink()
    else:
        git(repository, "config", "status.showUntrackedFiles", "no")
        (repository / "untracked.py").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(InputError, match="tracked or untracked"):
        namespace["verify_consumer_checkout"](repository, head, tree)


def test_replay_allows_ignored_environments_and_bytecode(consumer: tuple[Path, str, str]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    repository, head, tree = consumer
    for name in (".venv/python", "scripts/__pycache__/module.pyc"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ignored local input")
    assert namespace["verify_consumer_checkout"](repository, head, tree) == repository.resolve()


@pytest.mark.parametrize("bad_head", [b"", b"HEAD\n", b"a" * 40 + b"\nb" * 40 + b"\n"])
def test_replay_rejects_malformed_checkout_head_output(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch, bad_head: bytes
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    namespace = namespace["verify_consumer_checkout"].__globals__
    original = namespace["run_git"]

    def malformed(repository, arguments, **options):
        if arguments[-1] == "HEAD^{commit}":
            return ProcessResult(0, bad_head, b"", False, False, False)
        return original(repository, arguments, **options)

    monkeypatch.setitem(namespace, "run_git", malformed)
    with pytest.raises(GitError, match="malformed"):
        namespace["verify_consumer_checkout"](*consumer)


@pytest.mark.parametrize("returncode", [0, 1])
def test_replay_rejects_git_errors_and_unexpected_stderr(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    namespace = namespace["verify_consumer_checkout"].__globals__
    monkeypatch.setitem(
        namespace,
        "run_git",
        lambda *args, **kwargs: ProcessResult(
            returncode, b"true\n", b"warning\n" if returncode == 0 else b"", False, False, False
        ),
    )
    with pytest.raises(GitError, match="verify"):
        namespace["verify_consumer_checkout"](*consumer)


def test_replay_cli_writes_only_after_verifying_source(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace, output = invoke_main(consumer, monkeypatch)
    assert namespace["main"]() == 0
    document = json.loads(output.read_bytes())
    assert (
        document["adapter_file_sha256"]
        == hashlib.sha256(
            (consumer[0] / "scripts/test_planning_shadow.py").read_bytes()
        ).hexdigest()
    )
    assert document["summary"]["revisions"] == 1
    assert document["summary"]["omissions"] == 0
    assert document["summary"]["test_runtime_observations"] == 0


@pytest.mark.parametrize("change", ["wrong-commit", "dirty"])
def test_replay_cli_rejects_bad_source_before_importing_adapter(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    repository, head, tree = consumer
    if change == "wrong-commit":
        head = "0" * 40
    else:
        (repository / "README.md").write_text("changed\n", encoding="utf-8")
    namespace, output = invoke_main((repository, head, tree), monkeypatch)
    monkeypatch.setattr(
        namespace["importlib"].util,
        "spec_from_file_location",
        lambda *args: pytest.fail("adapter imported before checkout validation"),
    )
    with pytest.raises(SystemExit) as error:
        namespace["main"]()
    assert error.value.code == 2
    assert not output.exists()


def test_replay_cli_rechecks_source_immediately_after_import(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, _ = consumer
    adapter_path = repository / "scripts/test_planning_shadow.py"
    adapter_path.write_text(
        ADAPTER_SOURCE + '\nPath(__file__).with_name("import-changed.py").write_text("changed")\n',
        encoding="utf-8",
    )
    head, tree = commit(repository)
    namespace, output = invoke_main((repository, head, tree), monkeypatch)
    monkeypatch.setitem(
        namespace,
        "replay",
        lambda *args, **kwargs: pytest.fail("replay started with modified adapter checkout"),
    )
    with pytest.raises(SystemExit) as error:
        namespace["main"]()
    assert error.value.code == 2
    assert (repository / "scripts/import-changed.py").exists()
    assert not output.exists()


@pytest.mark.parametrize("change", ["modified", "moved-head"])
def test_replay_cli_rechecks_source_before_writing_evidence(
    consumer: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    namespace, output = invoke_main(consumer, monkeypatch)
    original = namespace["replay"]

    def changed(*args, **kwargs):
        document = original(*args, **kwargs)
        if change == "modified":
            (consumer[0] / "README.md").write_text("changed\n", encoding="utf-8")
        else:
            commit(consumer[0])
        return document

    monkeypatch.setitem(namespace, "replay", changed)
    with pytest.raises(SystemExit) as error:
        namespace["main"]()
    assert error.value.code == 2
    assert not output.exists()


def test_replay_output_bound_includes_the_entire_utf8_payload_and_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    namespace = namespace["encode_replay"].__globals__
    document = {"value": "snowman \u2603"}
    encoded = namespace["encode_replay"](document)
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == document
    monkeypatch.setitem(namespace, "MAX_BYTES", len(encoded))
    assert namespace["encode_replay"](document) == encoded
    monkeypatch.setitem(namespace, "MAX_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="output limit"):
        namespace["encode_replay"](document)


@pytest.fixture
def retained_replay() -> dict[str, Any]:
    directory = EXAMPLE.parents[2] / "docs/designs/experiments/django-ray-shadow-replay"
    document = json.loads((directory / "report.json").read_bytes())
    observations = []
    for entry in document["observation_files"]:
        rows = json.loads((directory / entry["path"]).read_bytes())
        canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(canonical.encode()).hexdigest() == entry["canonical_sha256"]
        observations.extend(rows)
    document["observations"] = observations
    return document


def test_replay_diagnostics_explain_all_retained_revisions_without_rewriting_evidence(
    retained_replay: dict[str, Any],
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    original = namespace["encode_replay"](retained_replay)
    diagnostic = namespace["replay_diagnostics"](retained_replay)

    assert namespace["encode_replay"](retained_replay) == original
    assert diagnostic["source_replay_sha256"] == hashlib.sha256(original).hexdigest()
    assert diagnostic["test-replay-diagnostics-version"] == 1
    assert diagnostic["activation"] == "disabled"
    assert diagnostic["summary"] == {
        "revisions": 31,
        "selection_breadth": {"all": 28, "partial": 0, "empty": 3},
        "fallback_causes": {
            "profile-full": 0,
            "full-trigger": 17,
            "unknown-path": 11,
            "selective": 3,
        },
        "baseline_dispatches": 50,
        "planned_dispatches": 140,
        "additional_dispatches": 90,
        "omitted_dispatches": 0,
        "additional_selection_revisions": 24,
        "omission_revisions": 0,
        "test_runtime_observations": 0,
        "feedback_time_reduction": None,
    }
    assert len(diagnostic["observations"]) == 31
    unknowns = diagnostic["path_revisions"]["unknown_paths"]
    assert len(unknowns) == 19
    assert sum(item["revisions"] for item in unknowns) == 28
    assert unknowns == sorted(unknowns, key=lambda item: (-item["revisions"], item["path"]))
    assert diagnostic["path_revisions"]["paths_without_recorded_classification"]
    assert sum(item["selected"] for item in diagnostic["suite_revisions"]) == 140
    assert sum(item["missing"] for item in diagnostic["suite_revisions"]) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_version", True),
        ("repository", "other/repository"),
        ("mode", "execution"),
        ("activation", "enabled"),
        ("observations", []),
    ],
)
def test_replay_diagnostics_reject_wrong_or_incomplete_replay(
    retained_replay: dict[str, Any],
    field: str,
    value: object,
) -> None:
    retained_replay[field] = value
    with pytest.raises(ValueError):
        runpy.run_path(str(EXAMPLE))["replay_diagnostics"](retained_replay)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test-shadow-version", True),
        ("status", "executed"),
        ("effective_mode", "selective"),
        ("baseline_selected", ["unknown"]),
        ("baseline_selected", ["native", "native"]),
        ("planned_selected", []),
        ("missing", ["native"]),
        ("additional", ["native"]),
        ("plan_digest", "0" * 64),
        ("source", {}),
        ("policy_sha256", "0" * 64),
        ("bindings_sha256", None),
        ("activation", "enabled"),
        ("adapter_source", {"commit": "HEAD", "tree": "c" * 40}),
        ("baseline_policy_sha256", "0" * 64),
    ],
)
def test_replay_diagnostics_reject_inconsistent_comparison_and_mixed_identities(
    retained_replay: dict[str, Any],
    field: str,
    value: object,
) -> None:
    retained_replay["observations"][0]["comparison"][field] = value
    with pytest.raises(ValueError):
        runpy.run_path(str(EXAMPLE))["replay_diagnostics"](retained_replay)


def test_replay_diagnostics_reject_plan_digest_tampering(retained_replay: dict[str, Any]) -> None:
    retained_replay["observations"][0]["plan"]["plan"]["policy_sha256"] = "0" * 64
    with pytest.raises(InputError, match="canonical"):
        runpy.run_path(str(EXAMPLE))["replay_diagnostics"](retained_replay)


def test_replay_diagnostics_reject_reordered_or_displaced_source(
    retained_replay: dict[str, Any],
) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    reordered = deepcopy(retained_replay)
    reordered["observations"].reverse()
    with pytest.raises(ValueError, match="order"):
        namespace["replay_diagnostics"](reordered)
    retained_replay["cohort"][0]["base"] = "0" * 40
    retained_replay["observations"][0]["revision"]["base"] = "0" * 40
    with pytest.raises(ValueError, match="source"):
        namespace["replay_diagnostics"](retained_replay)


def test_replay_diagnostics_keep_omissions_visible(retained_replay: dict[str, Any]) -> None:
    namespace = runpy.run_path(str(EXAMPLE))
    empty = next(
        row for row in retained_replay["observations"] if not row["comparison"]["planned_selected"]
    )
    empty["comparison"]["baseline_selected"] = ["native"]
    empty["comparison"]["missing"] = ["native"]
    diagnostic = namespace["replay_diagnostics"](retained_replay)
    assert diagnostic["summary"]["omission_revisions"] == 1
    assert diagnostic["summary"]["omitted_dispatches"] == 1
    native = next(
        item for item in diagnostic["suite_revisions"] if item["suite"] == "qualification.native"
    )
    assert native["missing"] == 1


def test_replay_cli_optional_diagnostics_are_separate_and_correlated(
    consumer: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
    retained_replay: dict[str, Any],
) -> None:
    namespace, output = invoke_main(consumer, monkeypatch)
    diagnostics = output.with_name("diagnostics.json")
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--diagnostics-output", str(diagnostics)])
    monkeypatch.setitem(namespace, "replay", lambda *args, **kwargs: retained_replay)
    assert namespace["main"]() == 0
    assert json.loads(output.read_bytes())["experiment_version"] == 1
    report = json.loads(diagnostics.read_bytes())
    assert report["source_replay_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert report["summary"]["fallback_causes"]["unknown-path"] == 11


@pytest.mark.parametrize("existing", ["same-path", "replay", "diagnostics"])
def test_replay_cli_diagnostics_refuse_collisions_before_loading_consumer(
    consumer: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
    existing: str,
) -> None:
    namespace, output = invoke_main(consumer, monkeypatch)
    diagnostics = output if existing == "same-path" else output.with_name("diagnostics.json")
    if existing != "same-path":
        (output if existing == "replay" else diagnostics).write_bytes(b"original evidence")
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--diagnostics-output", str(diagnostics)])
    monkeypatch.setitem(
        namespace, "verify_consumer_checkout", lambda *args: pytest.fail("loaded consumer")
    )
    with pytest.raises(SystemExit) as error:
        namespace["main"]()
    assert error.value.code == 2
    if existing != "same-path":
        assert (
            output if existing == "replay" else diagnostics
        ).read_bytes() == b"original evidence"


def test_replay_cli_diagnostics_failure_writes_neither_report(
    consumer: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace, output = invoke_main(consumer, monkeypatch)
    diagnostics = output.with_name("diagnostics.json")
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--diagnostics-output", str(diagnostics)])
    with pytest.raises(SystemExit) as error:
        namespace["main"]()
    assert error.value.code == 2
    assert not output.exists()
    assert not diagnostics.exists()
