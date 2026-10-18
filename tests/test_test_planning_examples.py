"""Executable selection contracts for both documented test-planning catalogs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from yaga.test_planning.models import QualificationPolicy
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.selection import SelectionPlan
from yaga.test_planning.service import create_test_plan

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
APPLICATION_TEST_PATHS = tuple(
    f"tests/unit/test_application_{name}.py"
    for name in (
        "chainsaw",
        "core",
        "generation",
        "result_presentation",
        "runtime_env_refusal",
        "profile",
        "api_runner",
        "workflow_runner",
    )
)


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


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "src/django_ray/lease_policy.py",
            (
                "qualification.application",
                "qualification.cancellation",
                "qualification.monitoring",
                "qualification.native",
            ),
        ),
        (
            "src/django_ray/management/commands/django_ray_lease_policy.py",
            ("qualification.application", "qualification.native"),
        ),
    ],
)
def test_django_ray_release_lease_dependencies_keep_all_consumers(
    django_ray_policy: QualificationPolicy,
    path: str,
    expected: tuple[str, ...],
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))
    assert plan.effective_mode == "selective"
    assert selected_suites(plan) == expected
    assert {item.suite: item.variants for item in plan.selected} == {
        suite: DJANGO_RAY_VARIANTS[suite] for suite in expected
    }


@pytest.mark.parametrize("path", APPLICATION_TEST_PATHS)
def test_django_ray_exact_application_test_ownership(
    django_ray_policy: QualificationPolicy, path: str
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))

    assert plan.effective_mode == "selective"
    assert selected_suites(plan) == ("qualification.application",)
    assert plan.selected[0].variants == ("default",)
    assert [(reason.code, reason.rule, reason.path) for reason in plan.reasons] == [
        ("test.rule", "application", path)
    ]


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_application_core_extra.py",
        "tests/unit/test_application_new.py",
        "tests/unit/test_application_core.py.bak",
        "tests/unit/nested/test_application_core.py",
        "tests/integration/test_application_core.py",
    ],
)
def test_django_ray_similar_unowned_tests_still_require_full_catalog(
    django_ray_policy: QualificationPolicy, path: str
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))

    assert plan.effective_mode == "full"
    assert selected_suites(plan) == DJANGO_RAY_ALL
    assert {item.suite: item.variants for item in plan.selected} == DJANGO_RAY_VARIANTS
    assert any(
        reason.code == "test.unknown-path" and reason.path == path for reason in plan.reasons
    )


def test_django_ray_shared_docker_scenario_selects_every_known_consumer(
    django_ray_policy: QualificationPolicy,
) -> None:
    path = "qualification/docker/scenario.py"
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path,))

    assert plan.effective_mode == "selective"
    assert selected_suites(plan) == (
        "qualification.cancellation",
        "qualification.latency",
        "qualification.native",
    )
    assert {item.suite: item.variants for item in plan.selected} == {
        suite: DJANGO_RAY_VARIANTS[suite] for suite in selected_suites(plan)
    }
    assert {(reason.rule, reason.path) for reason in plan.reasons} == {
        ("cancellation", path),
        ("latency", path),
        ("native", path),
    }


@pytest.mark.parametrize("path", APPLICATION_TEST_PATHS)
def test_django_ray_owned_application_test_with_lockfile_remains_full(
    django_ray_policy: QualificationPolicy, path: str
) -> None:
    plan = plan_tests(django_ray_policy, profile="development", changed_paths=(path, "uv.lock"))

    assert plan.effective_mode == "full"
    assert selected_suites(plan) == DJANGO_RAY_ALL
    assert {item.suite: item.variants for item in plan.selected} == DJANGO_RAY_VARIANTS
    assert any(
        reason.code == "test.full-trigger" and reason.path == "uv.lock" for reason in plan.reasons
    )


@pytest.fixture(scope="module")
def django_ray_recorded_replay() -> dict[int, dict[str, Any]]:
    directory = EXAMPLES.parents[1] / "experiments/django-ray-shadow-replay"
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    records = {}
    for entry in report["observation_files"]:
        observations = json.loads((directory / entry["path"]).read_text(encoding="utf-8"))
        canonical = json.dumps(
            observations, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == entry["canonical_sha256"]
        for record in observations:
            number = record["revision"]["number"]
            assert number not in records
            records[number] = record
    assert len(records) == 31
    return records


@pytest.mark.parametrize(
    ("number", "base", "head"),
    [
        (
            625,
            "60af8d6e4b44e0066562a8134dfaf5cec56dba32",
            "145461f24d950a1abc56a4c1fb328abfd0fcd645",
        ),
        (
            619,
            "8f030d50232cd48e4c20dceb5eb189d698b6c7eb",
            "5911f8d8663d8a77a6e9a41a7caf4dbd36c01e61",
        ),
        (
            623,
            "d5b037579e6c6181f4f70fe3a9edb12a4280e14e",
            "55b1c8b5c69596620f8f122e0e021ee0dff371d1",
        ),
        (
            614,
            "b00b60c15e21f999980ccb6af7c4cf39450a2813",
            "d5b037579e6c6181f4f70fe3a9edb12a4280e14e",
        ),
        (
            595,
            "02b7fb7e64f0d0752ae542204ad7a933d88b52a8",
            "6de8fb9b58d7ca073c18425d9fe85eeb17804955",
        ),
    ],
)
def test_django_ray_complete_recorded_application_ranges_select_application(
    django_ray_policy: QualificationPolicy,
    django_ray_recorded_replay: dict[int, dict[str, Any]],
    number: int,
    base: str,
    head: str,
) -> None:
    record = django_ray_recorded_replay[number]
    original = record["plan"]["plan"]
    assert (record["revision"]["base"], record["revision"]["head"]) == (base, head)
    assert original["source"]["base_commit"] == original["source"]["comparison_commit"] == base
    assert original["source"]["head_commit"] == original["source"]["execution_commit"] == head
    assert original["source"]["execution_tree"] == record["revision"]["tree"]
    assert original["effective_mode"] == "full"
    assert tuple(item["suite"] for item in original["selected"]) == DJANGO_RAY_ALL
    assert record["comparison"]["baseline_selected"] == ["application"]

    plan = plan_tests(
        django_ray_policy, profile="development", changed_paths=tuple(original["changed_paths"])
    )

    assert plan.changed_paths == tuple(original["changed_paths"])
    assert plan.effective_mode == "selective"
    assert selected_suites(plan) == ("qualification.application",)
    assert plan.selected[0].variants == ("default",)


def test_django_ray_recorded_frontend_and_unowned_tests_remain_full(
    django_ray_policy: QualificationPolicy,
    django_ray_recorded_replay: dict[int, dict[str, Any]],
) -> None:
    record = django_ray_recorded_replay[356]
    original = record["plan"]["plan"]
    assert record["revision"]["head"] == "5a69a892b4d8fe6682ed77a39b5f13ea67823d61"
    assert original["changed_paths"] == [
        "docs/changelog.md",
        "src/django_ray/static/django_ray/admin/diagnostics.css",
        "tests/integration/test_testproject_admin_theme.py",
        "tests/unit/test_package_metadata.py",
    ]

    plan = plan_tests(
        django_ray_policy, profile="development", changed_paths=tuple(original["changed_paths"])
    )

    assert plan.effective_mode == "full"
    assert selected_suites(plan) == DJANGO_RAY_ALL
    assert {item.suite: item.variants for item in plan.selected} == DJANGO_RAY_VARIANTS
    assert {reason.path for reason in plan.reasons if reason.code == "test.unknown-path"} == {
        "tests/integration/test_testproject_admin_theme.py",
        "tests/unit/test_package_metadata.py",
    }


def test_django_ray_owned_tests_reduce_selection_without_omitting_recorded_baseline(
    django_ray_policy: QualificationPolicy,
    django_ray_recorded_replay: dict[int, dict[str, Any]],
) -> None:
    plans = {
        number: plan_tests(
            django_ray_policy,
            profile="development",
            changed_paths=tuple(record["plan"]["plan"]["changed_paths"]),
        )
        for number, record in django_ray_recorded_replay.items()
    }
    assert sum(len(plan.selected) for plan in plans.values()) == 120
    assert sum(plan.effective_mode == "full" for plan in plans.values()) == 23
    assert sum(not plan.selected for plan in plans.values()) == 3
    assert {
        number for number, plan in plans.items() if 0 < len(plan.selected) < len(DJANGO_RAY_ALL)
    } == {625, 619, 623, 614, 595}
    for number, plan in plans.items():
        baseline = django_ray_recorded_replay[number]["comparison"]["baseline_selected"]
        assert {"qualification." + job for job in baseline} <= set(selected_suites(plan))


@pytest.mark.parametrize(
    ("old_path", "new_path", "expected", "effective_mode"),
    [
        (
            "tests/unit/test_application_core.py",
            "tests/unit/test_platform_auth.py",
            ("qualification.application", "qualification.monitoring"),
            "selective",
        ),
        (
            "tests/unit/test_application_core.py",
            "tests/unit/test_application_core_renamed.py",
            DJANGO_RAY_ALL,
            "full",
        ),
        ("tests/unit/test_application_core.py", None, ("qualification.application",), "selective"),
        ("tests/unit/test_application_unknown.py", None, DJANGO_RAY_ALL, "full"),
    ],
    ids=["rename-unions-owners", "rename-to-unknown", "delete-owned", "delete-unknown"],
)
def test_django_ray_renames_and_deletions_preserve_complete_path_selection(
    tmp_path: Path,
    old_path: str,
    new_path: str | None,
    expected: tuple[str, ...],
    effective_mode: str,
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "yaga@example.invalid")
    git("config", "user.name", "YAGA Tests")
    original = tmp_path / old_path
    original.parent.mkdir(parents=True)
    original.write_text("recorded test contents\n", encoding="utf-8")
    git("add", "--all")
    git("commit", "--quiet", "-m", "test: create ownership fixture")
    base = git("rev-parse", "HEAD")
    if new_path is None:
        git("rm", "--quiet", old_path)
    else:
        git("mv", old_path, new_path)
    git("commit", "--quiet", "-m", "test: change owned path")
    head = git("rev-parse", "HEAD")

    planned = create_test_plan(
        tmp_path,
        policy_path=EXAMPLES / "django-ray.toml",
        profile="development",
        revision_range=f"{base}..{head}",
    )

    assert planned.source.base_commit == base
    assert planned.source.execution_commit == head
    assert planned.selection.changed_paths == tuple(
        sorted((old_path,) if new_path is None else (old_path, new_path))
    )
    assert planned.selection.effective_mode == effective_mode
    assert selected_suites(planned.selection) == expected
    assert {item.suite: item.variants for item in planned.selection.selected} == {
        suite: DJANGO_RAY_VARIANTS[suite] for suite in expected
    }
