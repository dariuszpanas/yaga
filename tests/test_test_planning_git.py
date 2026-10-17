"""Real Git and strict identity coverage for explicit test-plan source collection."""

from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from yaga.errors import GitError, InputError
from yaga.git import changes as shared_changes
from yaga.git import runtime as git_runtime
from yaga.test_planning import git as planning_git
from yaga.test_planning import source as planning_source
from yaga.test_planning.source import CollectedTestSource, PlanSource


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repository: Path, changes: dict[str, str]) -> str:
    for name, contents in changes.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "--allow-empty", "-m", "feat: update source")
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "--quiet", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "yaga@example.invalid")
    git(tmp_path, "config", "user.name", "YAGA Tests")
    return tmp_path, commit(tmp_path, {"README.md": "root\n"})


def test_ranges_bind_comparison_and_execution_to_the_correct_commits(
    repository: tuple[Path, str],
) -> None:
    path, common = repository
    git(path, "switch", "--quiet", "-c", "feature")
    head = commit(path, {"feature.txt": "feature\n"})
    git(path, "switch", "--quiet", "main")
    base = commit(path, {"main.txt": "main\n"})
    tree = git(path, "rev-parse", f"{head}^{{tree}}")

    two_dot = planning_git.collect_test_source(path, revision_range="main..feature")
    three_dot = planning_git.collect_test_source(path, revision_range="main...feature")

    assert two_dot.source == PlanSource(base, head, base, head, tree)
    assert two_dot.changed_paths == ("feature.txt", "main.txt")
    assert three_dot.source == PlanSource(base, head, common, head, tree)
    assert three_dot.changed_paths == ("feature.txt",)


def test_revision_and_empty_range_preserve_distinct_source_meanings(
    repository: tuple[Path, str],
) -> None:
    path, head = repository
    tree = git(path, "rev-parse", "HEAD^{tree}")
    revision = planning_git.collect_test_source(path, revision="HEAD")
    empty_range = planning_git.collect_test_source(path, revision_range="HEAD..HEAD")

    assert revision == CollectedTestSource(PlanSource(None, None, None, head, tree), None)
    assert empty_range == CollectedTestSource(PlanSource(head, head, head, head, tree), ())


def test_range_never_reresolves_the_moving_head_ref(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    original = commit(path, {"original.txt": "original\n"})
    tree = git(path, "rev-parse", "HEAD^{tree}")
    read_changed_paths = planning_git.read_changed_paths

    def advance_ref(
        directory: Path, revision_range: str, *, strict_output: bool
    ) -> shared_changes.ChangedPaths:
        selected = read_changed_paths(directory, revision_range, strict_output=strict_output)
        commit(path, {"subsequent.txt": "subsequent\n"})
        return selected

    monkeypatch.setattr(planning_git, "read_changed_paths", advance_ref)
    selected = planning_git.collect_test_source(path, revision_range=f"{base}..HEAD")

    assert selected.source.execution_commit == original
    assert selected.source.execution_tree == tree
    assert selected.changed_paths == ("original.txt",)
    assert git(path, "rev-parse", "HEAD") != original


def test_collection_ignores_worktree_index_environment_and_replacements(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = repository
    head = commit(path, {"committed.txt": "committed\n"})
    tree = git(path, "rev-parse", "HEAD^{tree}")
    (path / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (path / "README.md").write_text("dirty\n", encoding="utf-8")
    replacement = git(path, "commit-tree", f"{base}^{{tree}}", "-m", "replacement")
    git(path, "replace", head, replacement)
    monkeypatch.setenv("GIT_DIR", str(path / "missing-metadata"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(path / "missing-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.relative")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    selected = planning_git.collect_test_source(path, revision_range=f"{base}..{head}")

    assert selected.source == PlanSource(base, head, base, head, tree)
    assert selected.changed_paths == ("committed.txt",)


def test_added_deleted_renamed_mode_and_gitlink_changes_are_all_visible(
    repository: tuple[Path, str],
) -> None:
    path, target = repository
    base = commit(path, {"old.txt": "rename\n", "remove.txt": "remove\n", "run.sh": "run\n"})
    git(path, "mv", "old.txt", "new.txt")
    git(path, "rm", "--quiet", "remove.txt")
    git(path, "update-index", "--chmod=+x", "run.sh")
    git(path, "update-index", "--add", "--cacheinfo", f"160000,{target},vendor/dependency")
    (path / "added.txt").write_text("added\n", encoding="utf-8")
    git(path, "add", "added.txt")
    git(path, "commit", "--quiet", "-m", "feat: exercise change types")
    git(path, "config", "diff.ignoreSubmodules", "all")

    selected = planning_git.collect_test_source(path, revision_range=f"{base}..HEAD")

    assert selected.changed_paths == (
        "added.txt",
        "new.txt",
        "old.txt",
        "remove.txt",
        "run.sh",
        "vendor/dependency",
    )


def test_shallow_revision_is_allowed_but_shallow_range_is_rejected(
    repository: tuple[Path, str],
) -> None:
    path, _ = repository
    head = commit(path, {"next.txt": "next\n"})
    clone = path.with_name(f"{path.name}-shallow")
    git(path, "clone", "--quiet", "--depth=1", "--no-local", str(path), str(clone))

    selected = planning_git.collect_test_source(clone, revision="HEAD")

    assert selected.source.execution_commit == head
    assert selected.changed_paths is None
    with pytest.raises(GitError, match="non-shallow"):
        planning_git.collect_test_source(clone, revision_range="HEAD..HEAD")


@pytest.mark.parametrize("source", [{"revision": "HEAD"}, {"revision_range": "HEAD..HEAD"}])
def test_grafts_rejected_for_both_sources(
    repository: tuple[Path, str], source: dict[str, str]
) -> None:
    path, head = repository
    grafts = path / ".git" / "info" / "grafts"
    grafts.write_text(f"{head}\n", encoding="ascii")

    with pytest.raises(GitError, match="legacy graft overlays"):
        planning_git.collect_test_source(path, **source)


@pytest.mark.parametrize("source", [{}, {"revision": "HEAD", "revision_range": "HEAD..HEAD"}])
def test_exactly_one_source_is_required_before_opening_git(
    tmp_path: Path, source: dict[str, str]
) -> None:
    with pytest.raises(InputError, match="exactly one explicit"):
        planning_git.collect_test_source(tmp_path / "missing", **source)


@pytest.mark.parametrize(
    "source",
    [
        {"revision": ""},
        {"revision": "--all"},
        {"revision": "HEAD other"},
        {"revision": "HEAD\u202e"},
        {"revision": "HEAD..HEAD"},
        {"revision": "HEAD^@"},
        {"revision": "HEAD^{tree}"},
        {"revision": "HEAD:path"},
        {"revision": "x" * 513},
        {"revision_range": "HEAD"},
        {"revision_range": "HEAD..."},
        {"revision_range": "HEAD..--all"},
        {"revision_range": "HEAD^!..HEAD"},
        {"revision_range": "HEAD:path..HEAD"},
        {"revision_range": "HEAD..HEAD:path"},
    ],
)
def test_unsafe_or_noncommit_sources_fail_closed(
    repository: tuple[Path, str], source: dict[str, str]
) -> None:
    with pytest.raises((InputError, GitError)):
        planning_git.collect_test_source(repository[0], **source)


@pytest.mark.parametrize("source", [{"revision": "missing"}, {"revision_range": "HEAD..missing"}])
def test_missing_objects_are_errors_not_a_full_fallback(
    repository: tuple[Path, str], source: dict[str, str]
) -> None:
    with pytest.raises(GitError, match="could not resolve"):
        planning_git.collect_test_source(repository[0], **source)


def test_runtime_failure_does_not_become_a_partial_plan(
    repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_runtime.shutil, "which", lambda _name: None)
    with pytest.raises(GitError, match="executable was not found"):
        planning_git.collect_test_source(repository[0], revision="HEAD")


@pytest.mark.parametrize("length", [40, 64])
def test_source_dataclass_supports_one_full_object_format(length: int) -> None:
    source = PlanSource("a" * length, "b" * length, "c" * length, "b" * length, "d" * length)
    assert source.head_commit == source.execution_commit
    with pytest.raises(FrozenInstanceError):
        source.execution_commit = "e" * length  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "fields",
    [
        {"base_commit": None},
        {"head_commit": None},
        {"comparison_commit": None},
        {"execution_commit": "e" * 40},
        {"execution_tree": "A" * 40},
        {"execution_tree": "a" * 39},
        {"execution_tree": "a" * 64},
        {"execution_tree": "\u001b" + "a" * 39},
        {"execution_tree": None},
        {"execution_tree": True},
    ],
)
def test_source_dataclass_rejects_partial_inconsistent_and_invalid_identities(
    fields: dict[str, object],
) -> None:
    source = PlanSource("a" * 40, "b" * 40, "c" * 40, "b" * 40, "d" * 40)
    with pytest.raises(ValueError):
        replace(source, **fields)


@pytest.mark.parametrize("paths", [None, [], ("b", "a"), ("a", "a"), ("../outside",)])
def test_collected_range_rejects_missing_or_malformed_path_sets(paths: object) -> None:
    source = PlanSource("a" * 40, "b" * 40, "c" * 40, "b" * 40, "d" * 40)
    with pytest.raises(ValueError):
        CollectedTestSource(source, paths)  # ty: ignore[invalid-argument-type]


def test_collected_revision_rejects_comparison_paths() -> None:
    source = PlanSource(None, None, None, "b" * 40, "d" * 40)
    with pytest.raises(ValueError, match="revision-only"):
        CollectedTestSource(source, ())


def test_collected_paths_are_bounded_before_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PlanSource("a" * 40, "b" * 40, "c" * 40, "b" * 40, "d" * 40)
    with monkeypatch.context() as context:
        context.setattr(planning_source, "MAX_CHANGED_PATHS", 1)
        with pytest.raises(ValueError, match="at most 1"):
            CollectedTestSource(source, ("a", "b"))
    with monkeypatch.context() as context:
        context.setattr(planning_source, "MAX_CHANGED_PATH_BYTES_TOTAL", 1)
        with pytest.raises(ValueError, match="byte limit"):
            CollectedTestSource(source, ("a",))
