"""Fixed executor preflight across backend, frontend, containers, and qualification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import handoff
from yaga.test_planning.handoff import ExecutorRegistry, SuiteExecutor, create_handoff
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import test_plan_document as plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

POLICY_SHA = "a" * 64
DEFINITION_SHA = "b" * 64
SOURCE = PlanSource("1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40)
EXAMPLES = Path(__file__).resolve().parents[1] / "docs/designs/examples/test-planning"


@pytest.fixture
def policy() -> QualificationPolicy:
    return QualificationPolicy(
        1,
        suites=(
            Suite("backend", "pytest", variants=("py312", "py315"), requires=("python",)),
            Suite("frontend", "vitest", requires=("node",)),
            Suite("container", "compose", variants=("amd", "arm"), requires=("docker",)),
            Suite("chainsaw", "chainsaw", needs=("container",), requires=("kubernetes",)),
            Suite(
                "qualification",
                "django-ray",
                variants=("native", "jobs", "jobs-crash", "core-loss"),
                needs=("container",),
                requires=("ray",),
            ),
        ),
        profiles=(Profile("dev", SelectionMode.SELECTIVE), Profile("full", SelectionMode.FULL)),
        rules=(
            SelectionRule("backend", ("src/**",), select=("backend",)),
            SelectionRule("frontend", ("frontend/**",), select=("frontend",)),
            SelectionRule("container", ("docker/**",), select=("container",)),
            SelectionRule("chainsaw", ("infra/**",), select=("chainsaw",)),
            SelectionRule("qualification", ("qualification/**",), select=("qualification",)),
        ),
        no_impact=("docs/**",),
    )


def _registry(policy: QualificationPolicy) -> ExecutorRegistry:
    return ExecutorRegistry(
        tuple(
            SuiteExecutor(
                suite.id,
                suite.binding,
                f"job-{suite.id}",
                suite.variants,
                suite.requires,
                DEFINITION_SHA,
            )
            for suite in policy.suites
        )
    )


@pytest.fixture
def registry(policy: QualificationPolicy) -> ExecutorRegistry:
    return _registry(policy)


def _planned(
    policy: QualificationPolicy,
    registry: ExecutorRegistry,
    *,
    paths: tuple[str, ...] = ("src/worker.py",),
    profile: str = "dev",
) -> PlannedTests:
    return PlannedTests(
        POLICY_SHA,
        registry.sha256,
        SOURCE,
        plan_tests(policy, profile=profile, changed_paths=paths),
    )


def _handoff(planned: PlannedTests, registry: ExecutorRegistry, **changes: Any) -> dict[str, Any]:
    expected = {
        "expected_policy_sha256": POLICY_SHA,
        "expected_execution_commit": SOURCE.execution_commit,
        "expected_execution_tree": SOURCE.execution_tree,
    }
    expected.update(changes)
    return create_handoff(planned, registry, **expected)


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def test_registry_document_and_exact_canonical_digest(registry: ExecutorRegistry) -> None:
    document = registry.document()
    assert set(document) == {"test-executor-registry-version", "executors"}
    assert document["test-executor-registry-version"] == 1
    assert [item["suite"] for item in document["executors"]] == [
        "backend",
        "chainsaw",
        "container",
        "frontend",
        "qualification",
    ]
    assert document["executors"][0] == {
        "suite": "backend",
        "binding": "pytest",
        "dispatch": "job-backend",
        "variants": ["py312", "py315"],
        "capabilities": ["python"],
        "definition_sha256": DEFINITION_SHA,
    }
    assert registry.sha256 == hashlib.sha256(_canonical(document)).hexdigest()
    assert len(_canonical(document)) <= handoff.MAX_PLAN_BYTES
    document["executors"].clear()
    assert len(registry.document()["executors"]) == 5


def test_registry_order_is_not_execution_identity(registry: ExecutorRegistry) -> None:
    reordered = ExecutorRegistry(
        tuple(
            replace(
                item,
                variants=tuple(reversed(item.variants)),
                capabilities=tuple(reversed(item.capabilities)),
            )
            for item in reversed(registry.executors)
        )
    )
    assert reordered.document() == registry.document()
    assert reordered.sha256 == registry.sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"suite": "other"},
        {"binding": "other"},
        {"dispatch": "other"},
        {"variants": ("other",)},
        {"capabilities": ("other",)},
        {"definition_sha256": "c" * 64},
    ],
)
def test_every_registry_field_participates_in_identity(
    registry: ExecutorRegistry, changes: dict[str, Any]
) -> None:
    changed = ExecutorRegistry((replace(registry.executors[0], **changes), *registry.executors[1:]))
    assert changed.sha256 != registry.sha256


@pytest.mark.parametrize(
    ("path", "selected"),
    [
        ("src/service.py", ["backend"]),
        ("frontend/app.ts", ["frontend"]),
        ("docker/compose.yaml", ["container"]),
        ("infra/test.yaml", ["container", "chainsaw"]),
        ("qualification/run.py", ["container", "qualification"]),
    ],
)
def test_all_five_execution_categories_use_fixed_bindings(
    policy: QualificationPolicy,
    registry: ExecutorRegistry,
    path: str,
    selected: list[str],
) -> None:
    planned = _planned(policy, registry, paths=(path,))
    document = _handoff(planned, registry)
    assert [item["suite"] for item in document["selected"]] == selected
    assert document["dispatch"] == {
        f"job-{suite.id}": suite.id in selected for suite in policy.suites
    }
    assert all(type(value) is bool for value in document["dispatch"].values())
    assert len(document["cells"]) == sum(len(item.variants) for item in planned.selection.selected)
    assert {item["binding"] for item in document["cells"]} == {
        suite.binding for suite in policy.suites if suite.id in selected
    }
    assert not {"outcome", "passed", "results", "command"} & document.keys()


def test_handoff_preserves_complete_identity_and_plan_digest(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    planned = _planned(policy, registry)
    document = _handoff(planned, registry)
    assert set(document) == {
        "test-handoff-version",
        "plan_digest",
        "policy_sha256",
        "bindings_sha256",
        "source",
        "profile",
        "requested_mode",
        "effective_mode",
        "dispatch",
        "selected",
        "cells",
    }
    assert document["test-handoff-version"] == 1
    assert document["plan_digest"] == plan_document(planned)["plan_digest"]
    assert document["policy_sha256"] == POLICY_SHA
    assert document["bindings_sha256"] == registry.sha256
    assert document["source"] == plan_document(planned)["plan"]["source"]
    assert document["profile"] == "dev"
    assert document["requested_mode"] == document["effective_mode"] == "selective"
    assert document["selected"] == [
        {
            "suite": "backend",
            "binding": "pytest",
            "dispatch": "job-backend",
            "variants": ["py312", "py315"],
            "needs": [],
        }
    ]
    assert document["cells"] == [
        {"suite": "backend", "variant": variant, "binding": "pytest", "dispatch": "job-backend"}
        for variant in ("py312", "py315")
    ]
    assert _canonical(document) == _canonical(_handoff(planned, registry))


def test_native_qualification_dispatches_once_with_four_cells(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    document = _handoff(_planned(policy, registry, paths=("qualification/run.py",)), registry)
    native = [item for item in document["selected"] if item["suite"] == "qualification"]
    assert len(native) == 1
    assert native[0]["variants"] == ["core-loss", "jobs", "jobs-crash", "native"]
    assert native[0]["needs"] == ["job-container"]
    assert len([item for item in document["cells"] if item["suite"] == "qualification"]) == 4
    assert document["selected"][0]["suite"] == "container"


def test_full_handoff_keeps_all_suites_and_cells(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    document = _handoff(_planned(policy, registry, profile="full"), registry)
    assert all(document["dispatch"].values())
    assert len(document["selected"]) == len(policy.suites)
    assert len(document["cells"]) == sum(len(suite.variants) for suite in policy.suites)
    assert document["requested_mode"] == document["effective_mode"] == "full"


def test_empty_selection_is_explicit_without_pass_claim(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    document = _handoff(_planned(policy, registry, paths=()), registry)
    assert document["selected"] == document["cells"] == []
    assert len(document["dispatch"]) == len(policy.suites)
    assert not any(document["dispatch"].values())
    assert "passed" not in _canonical(document).decode("utf-8")


def test_revision_only_full_handoff_preserves_sha256_source(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    source = PlanSource(None, None, None, "2" * 64, "4" * 64)
    planned = PlannedTests(
        POLICY_SHA,
        registry.sha256,
        source,
        plan_tests(policy, profile="full", changed_paths=None),
    )
    document = _handoff(
        planned,
        registry,
        expected_execution_commit=source.execution_commit,
        expected_execution_tree=source.execution_tree,
    )
    assert document["source"] == {
        "base_commit": None,
        "head_commit": None,
        "comparison_commit": None,
        "execution_commit": source.execution_commit,
        "execution_tree": source.execution_tree,
    }
    assert all(document["dispatch"].values())


def test_handoff_does_not_read_files_or_execute(
    policy: QualificationPolicy, registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = _planned(policy, registry)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pure handoff must not read files or execute subprocesses")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert _handoff(planned, registry)["dispatch"]["job-backend"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_policy_sha256": "f" * 64},
        {"expected_policy_sha256": "A" * 64},
        {"expected_policy_sha256": None},
        {"expected_execution_commit": "f" * 40},
        {"expected_execution_tree": "f" * 40},
        {"expected_execution_commit": "HEAD"},
        {"expected_execution_commit": "2" * 64},
        {"expected_execution_tree": True},
    ],
)
def test_expected_identities_are_required_and_exact(
    policy: QualificationPolicy, registry: ExecutorRegistry, changes: dict[str, Any]
) -> None:
    with pytest.raises(InputError):
        _handoff(_planned(policy, registry), registry, **changes)


@pytest.mark.parametrize("digest", [None, "c" * 64])
def test_preview_and_mismatched_binding_identity_fail(
    policy: QualificationPolicy, registry: ExecutorRegistry, digest: str | None
) -> None:
    with pytest.raises(InputError, match="binding"):
        _handoff(replace(_planned(policy, registry), bindings_sha256=digest), registry)


def test_changed_executor_definition_invalidates_existing_plan(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    changed = ExecutorRegistry(
        (replace(registry.executors[0], definition_sha256="c" * 64), *registry.executors[1:])
    )
    with pytest.raises(InputError, match="binding identity"):
        _handoff(_planned(policy, registry), changed)


@pytest.mark.parametrize("remove", [True, False])
def test_entire_inventory_must_have_exact_mapping_even_when_unselected(
    policy: QualificationPolicy, registry: ExecutorRegistry, remove: bool
) -> None:
    entries = (
        registry.executors[:-1]
        if remove
        else (
            *registry.executors,
            SuiteExecutor("extra", "extra", "job-extra", ("default",), (), DEFINITION_SHA),
        )
    )
    changed = ExecutorRegistry(entries)
    with pytest.raises(ConfigurationError, match="complete catalog"):
        _handoff(_planned(policy, changed), changed)


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": "other"},
        {"variants": ("native",)},
        {"variants": ("native", "jobs", "jobs-crash", "core-loss", "extra")},
        {"capabilities": ()},
    ],
)
def test_binding_variants_and_capabilities_checked_for_unselected_suites(
    policy: QualificationPolicy, registry: ExecutorRegistry, changes: dict[str, Any]
) -> None:
    changed = ExecutorRegistry(
        (*registry.executors[:-1], replace(registry.executors[-1], **changes))
    )
    with pytest.raises(ConfigurationError):
        _handoff(_planned(policy, changed), changed)


def test_available_capabilities_may_exceed_requirements(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    changed = ExecutorRegistry(
        (
            replace(registry.executors[0], capabilities=("python", "linux")),
            *registry.executors[1:],
        )
    )
    assert _handoff(_planned(policy, changed), changed)["dispatch"]["job-backend"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"suite": "$(run)"},
        {"binding": "../run"},
        {"dispatch": "command --arg"},
        {"dispatch": "job\ncommand"},
        {"dispatch": "job_name"},
        {"dispatch": "x" * 65},
        {"dispatch": True},
        {"variants": ()},
        {"variants": ("same", "same")},
        {"variants": ["default"]},
        {"capabilities": ("same", "same")},
        {"capabilities": ("cap",) * 17},
        {"definition_sha256": "B" * 64},
        {"definition_sha256": "b" * 63},
        {"definition_sha256": None},
    ],
)
def test_executor_records_are_strict_and_bounded(changes: dict[str, Any]) -> None:
    executor = SuiteExecutor("suite", "binding", "job", ("default",), (), DEFINITION_SHA)
    with pytest.raises(ValueError):
        replace(executor, **changes)


def test_executor_and_registry_records_are_frozen(registry: ExecutorRegistry) -> None:
    with pytest.raises(FrozenInstanceError):
        cast(Any, registry.executors[0]).dispatch = "other"
    with pytest.raises(FrozenInstanceError):
        cast(Any, registry).executors = ()


@pytest.mark.parametrize("entries", [(), [], (True,), ("executor",)])
def test_registry_requires_bounded_immutable_records(entries: Any) -> None:
    with pytest.raises(ValueError):
        ExecutorRegistry(entries)


@pytest.mark.parametrize("field", ["suite", "dispatch"])
def test_registry_rejects_duplicate_mapping_ids(registry: ExecutorRegistry, field: str) -> None:
    second = replace(registry.executors[1], **{field: getattr(registry.executors[0], field)})
    with pytest.raises(ValueError, match="unique"):
        ExecutorRegistry((registry.executors[0], second))


def test_registry_rejects_excessive_suites() -> None:
    with pytest.raises(ValueError, match="bounded"):
        ExecutorRegistry(
            tuple(
                SuiteExecutor(f"suite{i}", "binding", f"job{i}", ("default",), (), DEFINITION_SHA)
                for i in range(129)
            )
        )


def test_registry_rejects_excessive_cells() -> None:
    with pytest.raises(ValueError, match="cell limit"):
        ExecutorRegistry(
            tuple(
                SuiteExecutor(
                    f"suite{i}",
                    "binding",
                    f"job{i}",
                    tuple(f"variant{j}" for j in range(16)),
                    (),
                    DEFINITION_SHA,
                )
                for i in range(33)
            )
        )


def test_registry_rejects_excessive_distinct_capabilities() -> None:
    with pytest.raises(ValueError, match="capability limit"):
        ExecutorRegistry(
            tuple(
                SuiteExecutor(
                    f"suite{i}",
                    "binding",
                    f"job{i}",
                    ("default",),
                    tuple(f"cap{i * 16 + j}" for j in range(16)),
                    DEFINITION_SHA,
                )
                for i in range(9)
            )
        )


def test_registry_document_bound_is_complete(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = len(_canonical(registry.document()))
    monkeypatch.setattr(handoff, "MAX_PLAN_BYTES", size)
    assert len(_canonical(registry.document())) == size
    monkeypatch.setattr(handoff, "MAX_PLAN_BYTES", size - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        registry.document()


def test_handoff_document_bound_is_complete(
    policy: QualificationPolicy, registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = _planned(policy, registry, profile="full")
    size = len(_canonical(_handoff(planned, registry)))
    assert size > len(_canonical(registry.document()))
    monkeypatch.setattr(handoff, "MAX_PLAN_BYTES", size)
    assert len(_canonical(_handoff(planned, registry))) == size
    monkeypatch.setattr(handoff, "MAX_PLAN_BYTES", size - 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        _handoff(planned, registry)


@pytest.mark.parametrize("filename", ["polyglot.toml", "django-ray.toml"])
def test_both_example_catalogs_handoff_complete_full_inventory(filename: str) -> None:
    loaded = load_test_policy(EXAMPLES / filename)
    registry = _registry(loaded.policy)
    full = next(
        profile.name for profile in loaded.policy.profiles if profile.mode is SelectionMode.FULL
    )
    planned = PlannedTests(
        loaded.sha256,
        registry.sha256,
        SOURCE,
        plan_tests(loaded.policy, profile=full, changed_paths=()),
    )
    document = _handoff(planned, registry, expected_policy_sha256=loaded.sha256)
    assert len(document["selected"]) == len(loaded.policy.suites)
    assert len(document["cells"]) == sum(len(item.variants) for item in loaded.policy.suites)
    assert all(document["dispatch"].values())
    if filename == "django-ray.toml":
        assert len(document["selected"]) == 5
        assert len(document["cells"]) == 8


def test_non_model_inputs_fail_as_input_errors(
    policy: QualificationPolicy, registry: ExecutorRegistry
) -> None:
    with pytest.raises(InputError, match="validated plan"):
        _handoff(cast(Any, None), registry)
    with pytest.raises(InputError, match="executor registry"):
        _handoff(_planned(policy, registry), cast(Any, None))
