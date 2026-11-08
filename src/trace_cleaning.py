"""FINRA TRACE source policies for the generic data preprocessor."""

from __future__ import annotations

from typing import Any

from .data_preprocessing import FieldPolicy, classify_value, clean_value_sql, reason_sql


SENTINEL_MAGNITUDE_LIMIT = 10_000.0
KNOWN_TRACE_SENTINELS = (999_999.99,)
TRACE_POLICY_VERSION = "finra-trace-v1"

TRACE_FIELD_POLICIES = {
    "price": FieldPolicy(
        field="price",
        clean_field="clean_price",
        unit="price per 100 par",
        zero_is_missing=True,
        sentinels=KNOWN_TRACE_SENTINELS,
        minimum_valid_exclusive=0.0,
        maximum_absolute_exclusive=SENTINEL_MAGNITUDE_LIMIT,
    ),
    "last_sale_yield": FieldPolicy(
        field="last_sale_yield",
        clean_field="clean_last_sale_yield",
        unit="percent",
        zero_is_missing=True,
        sentinels=KNOWN_TRACE_SENTINELS,
        maximum_absolute_exclusive=SENTINEL_MAGNITUDE_LIMIT,
    ),
}


def clean_trace_value(field: str, value: Any) -> float | None:
    """Apply the registered TRACE policy to a single field value."""
    return classify_value(value, TRACE_FIELD_POLICIES[field])[0]


def trace_value_sql(field: str, column: str) -> str:
    """Build SQL for the registered TRACE field policy."""
    return clean_value_sql(column, TRACE_FIELD_POLICIES[field])


def trace_reason_sql(field: str, column: str) -> str:
    """Build a SQL reason label for the registered TRACE field policy."""
    return reason_sql(column, TRACE_FIELD_POLICIES[field])
