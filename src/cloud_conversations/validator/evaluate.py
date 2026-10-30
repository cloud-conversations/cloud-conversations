"""Rubric evaluation."""

from __future__ import annotations

import re
from typing import Any

from cloud_conversations.validator.rubric import Check, CheckResult


class ComparisonError(Exception):
    """Raised when a check's expected value is not comparable under its comparator."""


_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})

_VERSION = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)")


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


def _version(value: Any) -> tuple[int, ...]:
    """The numeric parts of a version: v20.11.1 -> (20, 11, 1)."""
    match = _VERSION.match(str(value).strip())
    if match is None:
        raise ComparisonError(f"{value!r} is not a version")
    return tuple(int(part) for part in match.group(1).split("."))


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
    if comparator == "version_eq":
        wanted = _version(expected)
        return _version(actual)[: len(wanted)] == wanted
    if comparator == "version_gte":
        wanted, seen = _version(expected), _version(actual)
        width = max(len(wanted), len(seen))
        return seen + (0,) * (width - len(seen)) >= wanted + (0,) * (width - len(wanted))
    raise ComparisonError(f"Unknown comparator {comparator!r}")


def evaluate_rubric(rubric: list[Check], facts: dict[str, Any]) -> list[CheckResult]:
    """Evaluate every check against the probed facts.

    A missing fact or malformed comparison fails the check.
    """
    results: list[CheckResult] = []
    for check in rubric:
        actual = facts.get(check.fact())
        try:
            ok = compare(check.comparator, check.expected, actual)
        except ComparisonError:
            ok = False
        results.append(
            CheckResult(
                check_id=check.id,
                kind=check.kind,
                target=check.target,
                expected=check.expected,
                actual=actual,
                passed=ok,
            )
        )
    return results
