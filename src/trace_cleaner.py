"""Build a derived TRACE dataset with carried marks and reversal flags."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from .trace_cleaning import TRACE_SERIES_CLEANING_VERSION


CLEANING_POLICY_VERSION = TRACE_SERIES_CLEANING_VERSION


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


def _mark_series(
    rows_by_key: dict[tuple[str, date], dict[str, Any]],
    *,
    value_field: str,
    action_field: str,
    mark_field: str,
    age_field: str,
    spike_threshold: float,
    rebound_absolute_tolerance: float,
    max_mark_age_business_days: int,
) -> tuple[int, int]:
    by_isin: dict[str, list[tuple[date, float]]] = {}
    source_dates_by_isin: dict[str, list[date]] = {}
    for (isin, observed_date), row in rows_by_key.items():
        source_dates_by_isin.setdefault(isin, []).append(observed_date)
        value = row.get(value_field)
        if value is not None:
            by_isin.setdefault(isin, []).append((observed_date, float(value)))
    flagged_count = carried_count = 0
    for isin, observations in by_isin.items():
        observations.sort()
        source_dates = sorted(source_dates_by_isin[isin])
        # Reversal detection uses a later point, so flag the original value
        # instead of revising the point-in-time observation.
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
            key = (isin, spike_date)
            rows_by_key[key][action_field] = "reversal_spike_flagged"
            flagged_count += 1

        last_value: float | None = None
        last_observation_date: date | None = None
        for mark_date in _business_dates_between(observations[0][0] - timedelta(days=1), observations[-1][0] + timedelta(days=1)):
            key = (isin, mark_date)
            row = rows_by_key.get(key)
            if row is not None and row.get(value_field) is not None:
                last_value = float(row[value_field])
                last_observation_date = mark_date
            if row is None:
                template_index = bisect_right(source_dates, mark_date) - 1
                template_date = source_dates[template_index]
                row = rows_by_key[(isin, template_date)].copy()
                row["trade_date"] = mark_date
                for field in ("price_raw", "clean_price", "last_sale_yield_raw", "clean_last_sale_yield"):
                    if field in row:
                        row[field] = None
                for field in ("price_clean_status", "yield_clean_status", "retrieved_at_utc", "observation_last_run_id"):
                    if field in row:
                        row[field] = None
                for field in ("mark_clean_price", "mark_clean_last_sale_yield", "mark_price_age_days", "mark_yield_age_days"):
                    row[field] = None
                row["cleaning_row_type"] = "mark_only_business_day"
                row["price_series_clean_action"] = "not_observed"
                row["yield_series_clean_action"] = "not_observed"
                rows_by_key[key] = row
            age_business_days = (
                _business_days_between(last_observation_date, mark_date)
                if last_observation_date is not None else None
            )
            if last_value is not None and age_business_days is not None and age_business_days <= max_mark_age_business_days:
                row[mark_field] = last_value
                row[age_field] = (mark_date - last_observation_date).days
                if row.get(value_field) is None:
                    row[action_field] = "carried_forward_mark"
                    carried_count += 1
            else:
                row[mark_field] = None
                row[age_field] = None
    for row in rows_by_key.values():
        value = row.get(value_field)
        if value is not None:
            row[mark_field] = float(value)
            row[age_field] = 0
    return flagged_count, carried_count


def clean_preprocessed_trace(
    input_path: Path,
    *,
    yield_spike_threshold_bps: float = 50.0,
    price_spike_threshold_points: float = 5.0,
    max_mark_age_business_days: int = 3,
) -> dict[str, Any]:
    """Create a new cleaned Parquet dataset; the preprocessed source is untouched."""
    if not input_path.exists():
        raise FileNotFoundError(f"Preprocessed TRACE file not found: {input_path}")
    if yield_spike_threshold_bps <= 0 or price_spike_threshold_points <= 0:
        raise ValueError("Spike thresholds must be positive")
    if max_mark_age_business_days < 0:
        raise ValueError("Maximum mark age must be non-negative")
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
        row["mark_clean_price"] = None
        row["mark_clean_last_sale_yield"] = None
        row["mark_price_age_days"] = None
        row["mark_yield_age_days"] = None
        rows_by_key[key] = row

    price_flagged, price_carried = _mark_series(
        rows_by_key, value_field="clean_price",
        action_field="price_series_clean_action", mark_field="mark_clean_price", age_field="mark_price_age_days",
        spike_threshold=price_spike_threshold_points,
        rebound_absolute_tolerance=1.0, max_mark_age_business_days=max_mark_age_business_days,
    )
    yield_flagged, yield_carried = _mark_series(
        rows_by_key, value_field="clean_last_sale_yield",
        action_field="yield_series_clean_action", mark_field="mark_clean_last_sale_yield",
        age_field="mark_yield_age_days", spike_threshold=yield_spike_threshold_bps / 100.0,
        rebound_absolute_tolerance=0.10, max_mark_age_business_days=max_mark_age_business_days,
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
        "max_mark_age_business_days": max_mark_age_business_days,
        "max_reversal_gap_business_days": 3,
        "rebound_absolute_tolerance": {"yield_percentage_points": 0.10, "price_points": 1.0},
        "rebound_tolerance_fraction_of_spike": 0.20,
        "business_day_marks_carried_forward": {"yield": yield_carried, "price": price_carried},
        "reversal_spikes_flagged": {"yield": yield_flagged, "price": price_flagged},
        "raw_preprocessed_values_modified": False,
        "actions": "Observed policy-cleaned price and yield values are unchanged. For marking, carry the latest value forward for at most the configured number of missing business days and record the mark age. Large isolated moves promptly reversed toward the prior level are flagged using the later observation; the original value is retained because the flag is retrospective.",
        "lookahead_used_to_build_observed_values": False,
        "reversal_flags_are_retrospective": True,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
