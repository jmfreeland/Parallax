"""Build a derived TRACE series with short-gap fills and reversal filtering."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl


CLEANING_POLICY_VERSION = "trace-series-cleaning-v1"


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


def _business_days_between(start: date, end: date) -> int:
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        count += current.weekday() < 5
        current += timedelta(days=1)
    return count


def _business_dates_between(start: date, end: date) -> list[date]:
    dates = []
    current = start + timedelta(days=1)
    while current < end:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _clean_series(
    rows_by_key: dict[tuple[str, date], dict[str, Any]],
    *,
    value_field: str,
    action_field: str,
    spike_threshold: float,
    rebound_absolute_tolerance: float,
    max_fill_business_days: int,
) -> tuple[int, int]:
    by_isin: dict[str, list[tuple[date, float]]] = {}
    for (isin, observed_date), row in rows_by_key.items():
        value = row.get(value_field)
        if value is not None:
            by_isin.setdefault(isin, []).append((observed_date, float(value)))
    corrected_count = filled_count = 0
    for isin, observations in by_isin.items():
        observations.sort()
        # Only remove an isolated, large move that reverses promptly to near
        # its prior level. The center point is replaced by time interpolation.
        corrected = list(observations)
        for index in range(1, len(observations) - 1):
            before_date, before = observations[index - 1]
            spike_date, spike = observations[index]
            after_date, after = observations[index + 1]
            if (_business_days_between(before_date, spike_date) > 3
                    or _business_days_between(spike_date, after_date) > 3):
                continue
            first_move, reversal = spike - before, after - spike
            tolerance = max(rebound_absolute_tolerance, abs(first_move) * 0.20)
            if (abs(first_move) < spike_threshold or first_move * reversal >= 0
                    or abs(after - before) > tolerance):
                continue
            span = (after_date - before_date).days
            weight = (spike_date - before_date).days / span if span else 0.5
            corrected[index] = (spike_date, before + weight * (after - before))
            key = (isin, spike_date)
            rows_by_key[key][value_field] = corrected[index][1]
            rows_by_key[key][action_field] = "reversal_spike_interpolated"
            corrected_count += 1

        for (left_date, left_value), (right_date, right_value) in zip(corrected, corrected[1:]):
            missing_days = _business_days_between(left_date, right_date) - 1
            if missing_days <= 0 or missing_days > max_fill_business_days:
                continue
            span = (right_date - left_date).days
            if span <= 0:
                continue
            for fill_date in _business_dates_between(left_date, right_date):
                key = (isin, fill_date)
                existing = rows_by_key.get(key)
                if existing is not None and existing.get(value_field) is not None:
                    continue
                value = left_value + (right_value - left_value) * ((fill_date - left_date).days / span)
                if existing is None:
                    template = rows_by_key[(isin, left_date)].copy()
                    template["trade_date"] = fill_date
                    for field in ("price_raw", "clean_price", "last_sale_yield_raw", "clean_last_sale_yield"):
                        if field in template:
                            template[field] = None
                    for field in ("price_clean_status", "yield_clean_status", "retrieved_at_utc", "observation_last_run_id"):
                        if field in template:
                            template[field] = None
                    template["cleaning_row_type"] = "interpolated_business_day"
                    template["price_series_clean_action"] = "not_observed"
                    template["yield_series_clean_action"] = "not_observed"
                    existing = rows_by_key.setdefault(key, template)
                existing[value_field] = value
                if existing.get(action_field) in (None, "missing", "not_observed"):
                    existing[action_field] = "short_gap_interpolated"
                filled_count += 1
    return corrected_count, filled_count


def clean_preprocessed_trace(
    input_path: Path,
    *,
    yield_spike_threshold_bps: float = 50.0,
    price_spike_threshold_points: float = 5.0,
    max_fill_business_days: int = 3,
) -> dict[str, Any]:
    """Create a new cleaned Parquet dataset; the preprocessed source is untouched."""
    if not input_path.exists():
        raise FileNotFoundError(f"Preprocessed TRACE file not found: {input_path}")
    if yield_spike_threshold_bps <= 0 or price_spike_threshold_points <= 0:
        raise ValueError("Spike thresholds must be positive")
    if max_fill_business_days < 0:
        raise ValueError("Maximum fill gap must be non-negative")
    frame = pl.read_parquet(input_path)
    required = {"isin", "trade_date", "clean_price", "clean_last_sale_yield"}
    if not required.issubset(frame.columns):
        raise ValueError("Input must be preprocessed TRACE Parquet with clean_price and clean_last_sale_yield")
    rows_by_key: dict[tuple[str, date], dict[str, Any]] = {}
    for raw_row in frame.iter_rows(named=True):
        row = dict(raw_row)
        row["trade_date"] = _as_date(row["trade_date"])
        key = (str(row["isin"]), row["trade_date"])
        row["cleaning_row_type"] = "observed"
        row["price_series_clean_action"] = "valid" if row.get("clean_price") is not None else "missing_or_rejected"
        row["yield_series_clean_action"] = "valid" if row.get("clean_last_sale_yield") is not None else "missing_or_rejected"
        rows_by_key[key] = row

    price_corrected, price_filled = _clean_series(
        rows_by_key, value_field="clean_price",
        action_field="price_series_clean_action", spike_threshold=price_spike_threshold_points,
        rebound_absolute_tolerance=1.0, max_fill_business_days=max_fill_business_days,
    )
    yield_corrected, yield_filled = _clean_series(
        rows_by_key, value_field="clean_last_sale_yield",
        action_field="yield_series_clean_action", spike_threshold=yield_spike_threshold_bps / 100.0,
        rebound_absolute_tolerance=0.10, max_fill_business_days=max_fill_business_days,
    )
    clean_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
    output_dir = input_path.parent / f"clean_id={clean_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "observations.parquet"
    report_path = output_dir / "diagnostics.json"
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output = (
        pl.DataFrame(list(rows_by_key.values()))
        .with_columns(
            pl.lit(clean_id).alias("clean_id"),
            pl.lit(CLEANING_POLICY_VERSION).alias("cleaning_policy_version"),
            pl.lit(str(input_path.resolve())).alias("cleaning_source_path"),
        )
        .sort(["isin", "trade_date"])
    )
    output.write_parquet(output_path, compression="zstd")
    report = {
        "clean_id": clean_id,
        "policy_version": CLEANING_POLICY_VERSION,
        "created_at_utc": created_at,
        "input_preprocessed_parquet": str(input_path.resolve()),
        "output_cleaned_parquet": str(output_path.resolve()),
        "diagnostics_report": str(report_path.resolve()),
        "input_rows": frame.height,
        "output_rows": output.height,
        "yield_spike_threshold_bps": yield_spike_threshold_bps,
        "price_spike_threshold_points": price_spike_threshold_points,
        "max_fill_business_days": max_fill_business_days,
        "max_reversal_gap_business_days": 3,
        "rebound_absolute_tolerance": {"yield_percentage_points": 0.10, "price_points": 1.0},
        "rebound_tolerance_fraction_of_spike": 0.20,
        "reversal_spikes_corrected": {"yield": yield_corrected, "price": price_corrected},
        "business_day_values_interpolated": {"yield": yield_filled, "price": price_filled},
        "raw_preprocessed_values_modified": False,
        "actions": "Per security and field, an isolated large observation is interpolated only when it reverses within three business days and the following value is near the prior value. Missing business days are linearly interpolated only between valid endpoints with no more than the configured number of missing business days.",
        "point_in_time_safe": False,
        "lookahead_note": "Reversal detection and gap interpolation use a later endpoint. Treat this as a retrospective cleaned dataset; do not use corrected/interpolated values as if they were known on that date in a live or point-in-time backtest.",
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
