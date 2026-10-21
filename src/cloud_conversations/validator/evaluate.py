"""Rubric evaluation."""

from __future__ import annotations

import uuid
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations.schema.description import Check
from cloud_conversations.schema.runs import CheckResult, Connection


class ComparisonError(Exception):
    """Raised when a check's expected value is not comparable under its comparator."""


_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})


def _as_bool(value: Any) -> bool | None:
    """Parse a boolean without Python truthiness."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return None


def compare(comparator: str, expected: Any, actual: Any) -> bool:
    if actual is None:
        return False
    if comparator == "eq":
        if isinstance(actual, bool):
            want = _as_bool(expected)
            if want is None:
                raise ComparisonError(
                    f"{expected!r} is not a boolean, but the observed value is"
                )
            return want == actual
        if isinstance(expected, int | float) and isinstance(actual, int | float):
            return expected == actual
        return str(expected).strip().lower() == str(actual).strip().lower()
    if comparator == "gte":
        try:
            return float(actual) >= float(expected)
        except (TypeError, ValueError) as exc:
            raise ComparisonError(
                f"'gte' needs numbers, got expected={expected!r} actual={actual!r}"
            ) from exc
    if comparator == "prefix":
        return str(actual).strip().lower().startswith(str(expected).strip().lower())
    if comparator == "contains":
        return str(expected).strip().lower() in str(actual).strip().lower()
    raise ComparisonError(f"Unknown comparator {comparator!r}")


def evaluate_rubric(rubric: list[Check], facts: dict[str, Any]) -> list[CheckResult]:
    """Evaluate every check against the probed facts.

    A missing fact or malformed comparison fails the check.
    """
    results: list[CheckResult] = []
    for check in rubric:
        actual = facts.get(check.kind)
        try:
            ok = compare(check.comparator, check.expected, actual)
        except ComparisonError:
            ok = False
        results.append(
            CheckResult(
                check_id=check.id,
                kind=check.kind,
                expected=check.expected,
                actual=actual,
                passed=ok,
            )
        )
    return results


def validate_run(
    run_id: uuid.UUID,
    owner_id: str,
    adapter: Any,
    site: str,
    rubric: list[Check],
    connection: Connection,
    probe_s: float,
) -> list[CheckResult]:
    """Probe the host and record the rubric's results."""
    from cloud_conversations.validator.probe import probe_host

    facts = probe_host(adapter, site, connection.host, connection.user, probe_s)
    results = evaluate_rubric(rubric, facts)

    for r in results:
        store.append_event(
            run_id,
            "validator",
            "check_observed",
            {
                "check_id": r.check_id,
                "kind": r.kind,
                "expected": r.expected,
                "actual": r.actual,
                "passed": r.passed,
            },
        )

    failed = [r.check_id for r in results if not r.passed]
    store.update_run(owner_id, run_id, results=results)
    store.append_event(
        run_id,
        "validator",
        "validation_completed",
        {"checks": len(results), "failed": failed},
    )
    return results
