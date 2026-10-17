"""Executable selection contracts for both documented test-planning catalogs."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.test_planning.models import QualificationPolicy
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.selection import SelectionPlan

EXAMPLES = Path(__file__).resolve().parents[1] / "docs/designs/examples/test-planning"

POLYGLOT_VARIANTS = {
    "backend.integration": ("py312-postgres",),
    "backend.unit": ("py312", "py315"),
    "docs.check": ("default",),
    "frontend.browser": ("chromium",),
    "frontend.unit": ("node-supported",),
    "image.build": ("linux-amd64",),
    "container.smoke": ("linux-amd64",),
    "infra.chainsaw": ("kind-supported",),
    "qualification.ray": ("linux-ray-supported",),
    "repository.smoke": ("default",),
}
POLYGLOT_ALL = tuple(POLYGLOT_VARIANTS)
DJANGO_RAY_VARIANTS = {
    "qualification.application": ("default",),
    "qualification.cancellation": ("default",),
    "qualification.latency": ("default",),
    "qualification.monitoring": ("default",),
    "qualification.native": ("core-loss", "jobs", "jobs-crash", "native"),
}
DJANGO_RAY_ALL = tuple(DJANGO_RAY_VARIANTS)


@pytest.fixture(scope="module")
def polyglot_policy() -> QualificationPolicy:
    return load_test_policy(EXAMPLES / "polyglot.toml").policy


@pytest.fixture(scope="module")
def django_ray_policy() -> QualificationPolicy:
    return load_test_policy(EXAMPLES / "django-ray.toml").policy


def selected_suites(plan: SelectionPlan) -> tuple[str, ...]:
    return tuple(item.suite for item in plan.selected)


@pytest.mark.parametrize(
    ("paths", "expected", "effective_mode"),
    [
        pytest.param(
            ("frontend/src/button.ts",),
            ("frontend.browser", "frontend.unit", "repository.smoke"),
            "selective",
            id="frontend",
        ),
        pytest.param(
            ("backend/src/billing.py",),
            ("backend.integration", "backend.unit", "repository.smoke"),
            "selective",
            id="backend",
        ),
        pytest.param(
            ("backend/src/runtime/worker.py",),
            (
                "backend.integration",
                "backend.unit",
                "image.build",
                "qualification.ray",
                "repository.smoke",
            ),
            "selective",
            id="runtime-overlapping-rules",
        ),
        pytest.param(
            ("schemas/api.json",),
            (
                "backend.integration",
                "backend.unit",
                "frontend.browser",
                "frontend.unit",
                "image.build",
                "container.smoke",
                "repository.smoke",
            ),
            "selective",
            id="cross-stack-api",
        ),
        pytest.param(
            ("deploy/service.yaml",),
            ("image.build", "infra.chainsaw", "repository.smoke"),
            "selective",
            id="chainsaw",
        ),
        pytest.param(
            ("tests/container/test_startup.py",),
            (
                "image.build",
                "container.smoke",
                "infra.chainsaw",
                "qualification.ray",
                "repository.smoke",
            ),
            "selective",
            id="docker-and-custom-qualification",
        ),
        pytest.param(
            ("README.md", "frontend/src/button.ts"),
            ("docs.check", "frontend.browser", "frontend.unit", "repository.smoke"),
            "selective",
            id="mixed-documentation-and-code",
        ),
        pytest.param(("LICENSE",), ("repository.smoke",), "selective", id="no-impact"),
        pytest.param((), ("repository.smoke",), "selective", id="empty-change"),
        pytest.param(
            ("frontend/package-lock.json",),
            POLYGLOT_ALL,
            "full",
            id="lockfile-full-trigger-overlaps-frontend-rule",
        ),
        pytest.param(
            ("new-component/source.rs",), POLYGLOT_ALL, "full", id="unknown-path-fallback"
        ),
    ],
)
def test_polyglot_documented_development_selections(
    polyglot_policy: QualificationPolicy,
    paths: tuple[str, ...],
    expected: tuple[str, ...],
    effective_mode: str,
) -> None:
    plan = plan_tests(polyglot_policy, profile="development", changed_paths=paths)

    assert plan.profile == "development"
    assert plan.requested_mode == "selective"
    assert plan.effective_mode == effective_mode
    assert plan.changed_paths == tuple(sorted(paths))
    assert selected_suites(plan) == expected
    assert {item.suite: item.variants for item in plan.selected} == {
        suite: POLYGLOT_VARIANTS[suite] for suite in expected
    }


@pytest.mark.parametrize("profile", ["integration", "release"])
@pytest.mark.parametrize("paths", [None, (), ("unknown/changed-file",)])
def test_polyglot_full_profiles_require_all_eleven_cells(
    polyglot_policy: QualificationPolicy, profile: str, paths: tuple[str, ...] | None
) -> None:
    plan = plan_tests(polyglot_policy, profile=profile, changed_paths=paths)

    assert plan.profile == profile
    assert plan.requested_mode == plan.effective_mode == "full"
    assert plan.changed_paths == paths
    assert selected_suites(plan) == POLYGLOT_ALL
    assert {item.suite: item.variants for item in plan.selected} == POLYGLOT_VARIANTS
    assert sum(len(item.variants) for item in plan.selected) == 11
    assert {reason.code for reason in plan.reasons} == {"test.profile-full"}


def test_polyglot_prerequisite_is_shared_and_explains_each_dependent(
    polyglot_policy: QualificationPolicy,
) -> None:
    plan = plan_tests(
        polyglot_policy,
        profile="development",
        changed_paths=("tests/container/test_startup.py",),
    )

    assert selected_suites(plan).count("image.build") == 1
    assert {
        (reason.suite, reason.required_by, reason.rule, reason.path)
        for reason in plan.reasons
        if reason.code == "test.prerequisite"
    } == {
        ("image.build", "container.smoke", None, None),
        ("image.build", "infra.chainsaw", None, None),
        ("image.build", "qualification.ray", None, None),
    }
    assert all(
        selected_suites(plan).index("image.build") < selected_suites(plan).index(dependent)
        for dependent in ("container.smoke", "infra.chainsaw", "qualification.ray")
    )


def test_polyglot_overlapping_rules_retain_each_selection_reason(
    polyglot_policy: QualificationPolicy,
) -> None:
    path = "backend/src/runtime/worker.py"
    plan = plan_tests(polyglot_policy, profile="development", changed_paths=(path,))

    assert {
        (reason.suite, reason.rule, reason.path)
        for reason in plan.reasons
        if reason.code == "test.rule"
    } == {
        ("backend.integration", "backend", path),
        ("backend.unit", "backend", path),
        ("backend.integration", "runtime", path),
        ("backend.unit", "runtime", path),
        ("qualification.ray", "runtime", path),
    }
    assert {reason.suite for reason in plan.reasons if reason.code == "test.always"} == {
        "repository.smoke"
    }


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("LICENSE", "test.no-impact"),
        ("frontend/package-lock.json", "test.full-trigger"),
        ("new-component/source.rs", "test.unknown-path"),
    ],
)
def test_polyglot_global_path_decisions_are_explained(
    polyglot_policy: QualificationPolicy, path: str, code: str
) -> None:
    plan = plan_tests(polyglot_policy, profile="development", changed_paths=(path,))

    assert [
        (reason.suite, reason.rule, reason.path, reason.required_by)
        for reason in plan.reasons
        if reason.code == code
    ] == [(None, None, path, None)]


def test_polyglot_mixed_selection_is_deterministic_and_preserves_all_variants(
    polyglot_policy: QualificationPolicy,
) -> None:
    paths = ("frontend/src/button.ts", "README.md", "backend/src/runtime/worker.py", "LICENSE")
    plan = plan_tests(polyglot_policy, profile="development", changed_paths=paths)

    assert plan == plan_tests(
        polyglot_policy, profile="development", changed_paths=tuple(reversed(paths))
    )
    assert selected_suites(plan) == (
        "backend.integration",
        "backend.unit",
        "docs.check",
        "frontend.browser",
        "frontend.unit",
        "image.build",
        "qualification.ray",
        "repository.smoke",
    )
    assert {item.suite: item.variants for item in plan.selected}["backend.unit"] == (
        "py312",
        "py315",
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("qualification/application/run_chainsaw.py", ("qualification.application",)),
        ("qualification/upgrade/check.py", ("qualification.native",)),
        ("qualification/latency/check.py", ("qualification.latency",)),
        ("qualification/cancellation/check.py", ("qualification.cancellation",)),
        ("qualification/monitoring/check.py", ("qualification.monitoring",)),
        (
            "src/django_ray/runner/driver.py",
            ("qualification.application", "qualification.cancellation"),
        ),
        (
            "src/django_ray/metrics.py",
            ("qualification.application", "qualification.monitoring"),
        ),
        (
            "src/django_ray/backends.py",
            ("qualification.application", "qualification.cancellation", "qualification.native"),
        ),
        (
            "qualification/transactions/Dockerfile",
            ("qualification.cancellation", "qualification.latency", "qualification.native"),
        ),
        (
            "qualification/upgrade/scenario.py",
            ("qualification.cancellation", "qualification.native"),
        ),
        ("k8s/overlays/kuberay-kind/deployment.yaml", ("qualification.monitoring",)),
        ("tests/unit/test_qualification_gate.py", DJANGO_RAY_ALL),
        ("docs/usage.md", ()),
        ("README.md", ()),
    ],
)
def test_django_ray_rule_classes_and_overlaps_preserve_qualification_cells(
    django_ray_policy: QualificationPolicy, path: str, expected: tuple[str, ...]
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))

    assert plan.requested_mode == plan.effective_mode == "selective"
    assert selected_suites(plan) == expected
    assert {item.suite: item.variants for item in plan.selected} == {
        suite: DJANGO_RAY_VARIANTS[suite] for suite in expected
    }


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("new-component/source.rs", "test.unknown-path"),
        ("pyproject.toml", "test.full-trigger"),
        ("uv.lock", "test.full-trigger"),
        (".github/workflows/application-qualification.yml", "test.full-trigger"),
        (".yaga/test-policy.toml", "test.full-trigger"),
        ("mk/k8s.mk", "test.full-trigger"),
        ("Dockerfile", "test.full-trigger"),
    ],
)
def test_django_ray_fallback_and_shared_infrastructure_require_complete_catalog(
    django_ray_policy: QualificationPolicy, path: str, code: str
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))

    assert plan.requested_mode == "selective"
    assert plan.effective_mode == "full"
    assert selected_suites(plan) == DJANGO_RAY_ALL
    assert {item.suite: item.variants for item in plan.selected} == DJANGO_RAY_VARIANTS
    assert any(reason.code == code and reason.path == path for reason in plan.reasons)


@pytest.mark.parametrize("profile", ["integration", "release"])
def test_django_ray_full_profiles_preserve_all_eight_cells(
    django_ray_policy: QualificationPolicy, profile: str
) -> None:
    plan = plan_tests(django_ray_policy, profile=profile, changed_paths=None)

    assert plan.requested_mode == plan.effective_mode == "full"
    assert selected_suites(plan) == DJANGO_RAY_ALL
    assert {item.suite: item.variants for item in plan.selected} == DJANGO_RAY_VARIANTS
    assert sum(len(item.variants) for item in plan.selected) == 8


def test_django_ray_empty_change_is_zero_obligations(
    django_ray_policy: QualificationPolicy,
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=())

    assert plan.effective_mode == "selective"
    assert plan.changed_paths == ()
    assert plan.selected == ()
    assert plan.reasons == ()
