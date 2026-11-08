"""Provider-independent field cleaning policies and SQL helpers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class FieldPolicy:
    """Rules for one normalized numeric field from any provider."""

    field: str
    clean_field: str
    unit: str
    zero_is_missing: bool = False
    sentinels: tuple[float, ...] = ()
    minimum_valid_exclusive: float | None = None
    maximum_absolute_exclusive: float | None = None


def classify_value(value: Any, policy: FieldPolicy) -> tuple[float | None, str]:
    """Return a cleaned numeric value and an auditable disposition label."""
    if value is None:
        return None, "missing"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "non_numeric"
    if not math.isfinite(number):
        return None, "non_finite"
    if number in policy.sentinels:
        return None, "sentinel"
    if policy.zero_is_missing and number == 0:
        return None, "zero_is_missing"
    if policy.minimum_valid_exclusive is not None and number <= policy.minimum_valid_exclusive:
        return None, "below_minimum"
    if policy.maximum_absolute_exclusive is not None and abs(number) >= policy.maximum_absolute_exclusive:
        return None, "outside_absolute_limit"
    return number, "valid"


def _column(column: str) -> str:
    if not _IDENTIFIER.fullmatch(column):
        raise ValueError(f"Unsafe SQL column identifier: {column!r}")
    return f'"{column}"'


def _num(value: float) -> str:
    return format(float(value), ".15g")


def reason_sql(column: str, policy: FieldPolicy) -> str:
    """Return a SQL CASE expression with the same labels as classify_value."""
    col = _column(column)
    number = f"try_cast({col} AS DOUBLE)"
    checks = [
        (f"{col} IS NULL", "missing"),
        (f"{number} IS NULL", "non_numeric"),
        (f"NOT isfinite({number})", "non_finite"),
    ]
    if policy.sentinels:
        values = ", ".join(_num(value) for value in policy.sentinels)
        checks.append((f"{number} IN ({values})", "sentinel"))
    if policy.zero_is_missing:
        checks.append((f"{number} = 0", "zero_is_missing"))
    if policy.minimum_valid_exclusive is not None:
        checks.append((f"{number} <= {_num(policy.minimum_valid_exclusive)}", "below_minimum"))
    if policy.maximum_absolute_exclusive is not None:
        checks.append(
            (f"abs({number}) >= {_num(policy.maximum_absolute_exclusive)}", "outside_absolute_limit")
        )
    branches = " ".join(f"WHEN {condition} THEN '{label}'" for condition, label in checks)
    return f"CASE {branches} ELSE 'valid' END"


def clean_value_sql(column: str, policy: FieldPolicy) -> str:
    """Return a SQL expression which leaves valid values and NULLs rejected ones."""
    col = _column(column)
    return f"CASE WHEN ({reason_sql(column, policy)}) = 'valid' THEN try_cast({col} AS DOUBLE) ELSE NULL END"
