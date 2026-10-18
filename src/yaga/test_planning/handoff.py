"""Pure preflight and fixed executor handoff, without execution or result evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning.limits import (
    MAX_CELLS,
    MAX_DISTINCT_CAPABILITIES,
    MAX_PLAN_BYTES,
    MAX_SUITES,
    MAX_TOKEN_BYTES,
)
from yaga.test_planning.models import Suite
from yaga.test_planning.reporting import test_plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

EXECUTOR_REGISTRY_VERSION = 1
TEST_HANDOFF_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", flags=re.ASCII)


@dataclass(frozen=True, slots=True)
class SuiteExecutor:
    """A consumer-owned fixed job and the complete cells it can execute once."""

    suite: str
    binding: str
    dispatch: str
    variants: tuple[str, ...]
    capabilities: tuple[str, ...]
    definition_sha256: str

    def __post_init__(self) -> None:
        # Share the policy's exact token, tuple, duplicate, and per-suite bounds.
        Suite(self.suite, self.binding, variants=self.variants, requires=self.capabilities)
        if (
            type(self.dispatch) is not str
            or not 1 <= len(self.dispatch) <= MAX_TOKEN_BYTES
            or not _TOKEN.fullmatch(self.dispatch)
        ):
            raise ValueError("executor dispatch must be a bounded lowercase ASCII token")
        if not _is_digest(self.definition_sha256):
            raise ValueError("executor definition_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ExecutorRegistry:
    """Complete fixed suite mappings with a canonical execution-manifest identity."""

    executors: tuple[SuiteExecutor, ...]

    def __post_init__(self) -> None:
        if (
            type(self.executors) is not tuple
            or not 1 <= len(self.executors) <= MAX_SUITES
            or any(type(executor) is not SuiteExecutor for executor in self.executors)
        ):
            raise ValueError("executor registry requires a bounded nonempty immutable tuple")
        for field in ("suite", "dispatch"):
            if len({getattr(executor, field) for executor in self.executors}) != len(
                self.executors
            ):
                raise ValueError(f"executor registry requires unique {field} identifiers")
        if sum(len(executor.variants) for executor in self.executors) > MAX_CELLS:
            raise ValueError("executor registry exceeds the expanded variant cell limit")
        if (
            len({value for executor in self.executors for value in executor.capabilities})
            > MAX_DISTINCT_CAPABILITIES
        ):
            raise ValueError("executor registry exceeds the distinct capability limit")
        self.document()

    def document(self) -> dict[str, Any]:
        """Return the complete versioned manifest, independent of declaration order."""
        document = {
            "test-executor-registry-version": EXECUTOR_REGISTRY_VERSION,
            "executors": [
                {
                    "suite": executor.suite,
                    "binding": executor.binding,
                    "dispatch": executor.dispatch,
                    "variants": sorted(executor.variants),
                    "capabilities": sorted(executor.capabilities),
                    "definition_sha256": executor.definition_sha256,
                }
                for executor in sorted(self.executors, key=lambda item: item.suite)
            ],
        }
        _canonical_json(document)
        return document

    @property
    def sha256(self) -> str:
        """Hash the exact canonical manifest, including each executor definition."""
        return hashlib.sha256(_canonical_json(self.document()).encode("utf-8")).hexdigest()


def create_handoff(
    planned: PlannedTests,
    registry: ExecutorRegistry,
    *,
    expected_policy_sha256: str,
    expected_execution_commit: str,
    expected_execution_tree: str,
) -> dict[str, Any]:
    """Validate a freshly computed plan against independently supplied consumer inputs.

    A registry or digest is correlation, not trusted origin. The consumer owns the
    fixed mappings, expected identities, actual capabilities, and definition hash
    construction. It must preserve normal execution, cancellation, and cleanup
    checks. This document is never evidence that any selected test ran or passed.
    """
    if type(planned) is not PlannedTests or type(registry) is not ExecutorRegistry:
        raise InputError("handoff requires a validated plan and executor registry")
    if not _is_digest(expected_policy_sha256):
        raise InputError("expected policy identity must be a lowercase SHA-256 digest")
    try:
        PlanSource(None, None, None, expected_execution_commit, expected_execution_tree)
    except ValueError as error:
        raise InputError(
            "expected execution identities must be same-format Git object IDs"
        ) from error
    if planned.policy_sha256 != expected_policy_sha256:
        raise InputError("test handoff policy identity does not match the expected policy")
    if (
        planned.source.execution_commit != expected_execution_commit
        or planned.source.execution_tree != expected_execution_tree
    ):
        raise InputError("test handoff source does not match the expected execution identity")
    if planned.bindings_sha256 is None:
        raise InputError("preview test plans without a bindings identity cannot be handed off")
    if planned.bindings_sha256 != registry.sha256:
        raise InputError("test handoff binding identity does not match the executor registry")

    executors = {executor.suite: executor for executor in registry.executors}
    suites = {suite.id: suite for suite in planned.selection.inventory}
    if set(executors) != set(suites):
        raise ConfigurationError("executor registry must map the complete catalog exactly once")
    for suite in planned.selection.inventory:
        executor = executors[suite.id]
        if executor.binding != suite.binding:
            raise ConfigurationError("executor binding does not match the catalog binding")
        if tuple(sorted(executor.variants)) != suite.variants:
            raise ConfigurationError("executor must preserve every catalog variant exactly")
        if not set(suite.requires) <= set(executor.capabilities):
            raise ConfigurationError("executor is missing a required catalog capability")

    selected_ids = {item.suite for item in planned.selection.selected}
    document = {
        "test-handoff-version": TEST_HANDOFF_VERSION,
        "plan_digest": test_plan_document(planned)["plan_digest"],
        "policy_sha256": planned.policy_sha256,
        "bindings_sha256": planned.bindings_sha256,
        "source": asdict(planned.source),
        "profile": planned.selection.profile,
        "requested_mode": planned.selection.requested_mode.value,
        "effective_mode": planned.selection.effective_mode.value,
        "dispatch": {
            executor.dispatch: executor.suite in selected_ids
            for executor in sorted(registry.executors, key=lambda item: item.dispatch)
        },
        "selected": [
            {
                "suite": item.suite,
                "binding": executors[item.suite].binding,
                "dispatch": executors[item.suite].dispatch,
                "variants": list(item.variants),
                "needs": [executors[name].dispatch for name in suites[item.suite].needs],
            }
            for item in planned.selection.selected
        ],
        "cells": [
            {
                "suite": item.suite,
                "variant": variant,
                "binding": executors[item.suite].binding,
                "dispatch": executors[item.suite].dispatch,
            }
            for item in planned.selection.selected
            for variant in item.variants
        ],
    }
    _canonical_json(document)
    return document


def _is_digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _canonical_json(document: dict[str, Any]) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    remaining = MAX_PLAN_BYTES
    chunks = []
    for chunk in encoder.iterencode(document):
        remaining -= len(chunk.encode("utf-8"))
        if remaining < 0:
            raise ConfigurationError(f"test handoff exceeds the hard {MAX_PLAN_BYTES}-byte limit")
        chunks.append(chunk)
    return "".join(chunks)
