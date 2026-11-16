"""Walk-forward evaluation for Nelson-Siegel and partition-percentile signals."""

from __future__ import annotations

import csv
from collections import defaultdict
import json
import math
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np

from .bond_returns import calculate_historical_bond_total_return
from .nelson_siegel import METHODOLOGY, _basis, _fit_curve
from .trace_cleaning import TRACE_POLICY_VERSION, TRACE_SERIES_CLEANING_VERSION, clean_trace_value


ProgressCallback = Callable[[int, int], None]
MIN_DAILY_CHANGE_CROSS_SECTION = 5
PREDICTOR_NELSON_SIEGEL = "nelson_siegel_residual"
PREDICTOR_PARTITION_PERCENTILE = "historical_partition_percentile"


def _has_current_cleaning_version(path: Path) -> bool:
    report_path = path.parent / "diagnostics.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return report.get("policy_version") == TRACE_SERIES_CLEANING_VERSION


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0
        start = end
    return ranks


def _correlation(left: list[float], right: list[float], *, spearman: bool = False) -> float | None:
    if len(left) < 5:
        return None
    x = np.asarray(left, dtype=float)
    y = np.asarray(right, dtype=float)
    if spearman:
        x, y = _rank(x), _rank(y)
    if np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _historical_percentile(
    history: list[tuple[date, float]],
    value: float,
    *,
    as_of_date: date,
    lookback_days: int,
    minimum_observations: int,
) -> tuple[float | None, int]:
    """Rank a partition level against its strictly prior trailing history."""
    cutoff = as_of_date - timedelta(days=lookback_days)
    prior = [level for observed_date, level in history if cutoff <= observed_date < as_of_date]
    if len(prior) < minimum_observations:
        return None, len(prior)
    below = sum(level < value for level in prior)
    equal = sum(level == value for level in prior)
    percentile = 100.0 * (below + 0.5 * equal) / len(prior)
    return percentile, len(prior)


def evaluate_nelson_siegel_signal(
    *,
    start_date: date,
    end_date: date,
    horizon_days: int,
    bond_list_path: Path,
    database_path: Path,
    minimum_bonds: int = 5,
    maximum_signal_age_days: int = 30,
    maximum_yield_change_gap_days: int = 7,
    maximum_price_age_days: int = 7,
    long_short_quantile: float = 0.2,
    data_source: str = "cleaned_parquet",
    data_path: Path | None = None,
    predictor_id: str = PREDICTOR_NELSON_SIEGEL,
    partition_field: str = "sector",
    percentile_lookback_days: int = 1095,
    minimum_history_observations: int = 20,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Evaluate a Nelson-Siegel residual or historical partition percentile.

    For every horizon, accumulate consecutive post-T TRACE yield changes,
    de-mean them across the eligible cross-section on each trade date, and
    compare the signal with the sign-reversed cumulative changes. TRACE does
    not include a Treasury benchmark here, so this is a spread-change proxy.
    Partition percentile scores rank each partition's current median TRACE
    yield against strictly prior values in a trailing lookback. As no Treasury
    curve is joined here, that level is explicitly a spread proxy. The event-study
    and rebalanced series use a duration-adjusted yield-only
    return proxy; the maximum-horizon strategy also reports historical total
    bond returns where prices and coupon data are available.
    """
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if horizon_days < 2:
        raise ValueError("horizon_days must be at least 2")
    if predictor_id not in {PREDICTOR_NELSON_SIEGEL, PREDICTOR_PARTITION_PERCENTILE}:
        raise ValueError(f"Unsupported predictor_id: {predictor_id}")
    if minimum_bonds < (5 if predictor_id == PREDICTOR_NELSON_SIEGEL else 1):
        minimum_required = 5 if predictor_id == PREDICTOR_NELSON_SIEGEL else 1
        raise ValueError(f"minimum_bonds must be at least {minimum_required} for this predictor")
    if maximum_signal_age_days < 0 or maximum_yield_change_gap_days < 1 or maximum_price_age_days < 0:
        raise ValueError("observation age limits must be non-negative")
    if not 0 < long_short_quantile < 0.5:
        raise ValueError("long_short_quantile must be between 0 and 0.5")
    if percentile_lookback_days < 1 or minimum_history_observations < 1:
        raise ValueError("percentile lookback and minimum history must be positive")
    if not bond_list_path.exists():
        raise FileNotFoundError(f"Bond list not found: {bond_list_path}")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    if data_source not in {"raw_duckdb", "preprocessed_parquet", "cleaned_parquet"}:
        raise ValueError(f"Unsupported TRACE data source: {data_source}")
    if data_source == "raw_duckdb":
        resolved_data_path = database_path.expanduser().resolve()
    else:
        if data_path is None:
            root = database_path.parent / "preprocessed"
            parent_prefix = "clean_id=" if data_source == "cleaned_parquet" else "preprocess_id="
            candidates = [
                path for path in root.rglob("observations.parquet")
                if path.parent.name.startswith(parent_prefix)
                and (data_source != "cleaned_parquet" or _has_current_cleaning_version(path))
            ] if root.exists() else []
            if candidates:
                data_path = max(candidates, key=lambda path: path.stat().st_mtime)
        if data_path is None or not data_path.exists():
            expected = "a cleaned" if data_source == "cleaned_parquet" else "a preprocessed"
            raise FileNotFoundError(f"Select {expected} TRACE observations.parquet dataset")
        resolved_data_path = data_path.expanduser().resolve()
        if data_source == "cleaned_parquet" and not _has_current_cleaning_version(resolved_data_path):
            raise ValueError(
                "This cleaned dataset uses an outdated or unknown cleaning policy. "
                "Run Clean preprocessed TRACE data again to create the current carried-mark dataset."
            )

    bonds: dict[str, dict[str, Any]] = {}
    with bond_list_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"isin", "maturity_date"}
        if predictor_id == PREDICTOR_NELSON_SIEGEL:
            required.add("equity_ticker")
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Bond list must contain {', '.join(sorted(required))} columns")
        if predictor_id == PREDICTOR_PARTITION_PERCENTILE and partition_field not in reader.fieldnames:
            raise ValueError(f"Partition field {partition_field!r} is not in the bond list")
        for raw in reader:
            isin = (raw.get("isin") or "").strip().upper()
            ticker = (raw.get("equity_ticker") or "").strip().upper()
            try:
                maturity = date.fromisoformat((raw.get("maturity_date") or "").strip())
            except ValueError:
                continue
            if not isin or (predictor_id == PREDICTOR_NELSON_SIEGEL and not ticker):
                continue
            try:
                duration = float(raw.get("modified_duration_years") or "")
                if duration <= 0 or not math.isfinite(duration):
                    duration = None
            except ValueError:
                duration = None
            try:
                coupon = float(raw.get("coupon_pct") or "")
                if coupon < 0 or coupon >= 100 or not math.isfinite(coupon):
                    coupon = None
            except ValueError:
                coupon = None
            bonds[isin] = {
                "isin": isin,
                "ticker": ticker,
                "issuer_name": (raw.get("issuer_name") or "").strip(),
                "sector": (raw.get("sector") or "").strip(),
                "maturity_date": maturity,
                "modified_duration_years": duration,
                "coupon_pct": coupon,
                "partition_value": (raw.get(partition_field) or "").strip() or "Unclassified",
            }
    if not bonds:
        raise ValueError("No bonds with valid ISIN and maturity date were found")

    query_start = start_date - timedelta(days=max(
        maximum_signal_age_days,
        maximum_price_age_days,
        percentile_lookback_days if predictor_id == PREDICTOR_PARTITION_PERCENTILE else 0,
    ))
    query_end = end_date + timedelta(days=horizon_days)
    connection = (
        duckdb.connect(str(database_path), read_only=True)
        if data_source == "raw_duckdb" else duckdb.connect(":memory:")
    )
    try:
        placeholders = ",".join("?" for _ in bonds)
        if data_source == "raw_duckdb":
            query = (
                "SELECT isin, trade_date, last_sale_yield, price FROM observations "
                f"WHERE trade_date BETWEEN ? AND ? AND isin IN ({placeholders}) ORDER BY isin, trade_date"
            )
            query_parameters: list[Any] = [query_start, query_end, *bonds]
        else:
            yield_column, price_column = "clean_last_sale_yield", "clean_price"
            parquet_path_sql = "'" + resolved_data_path.as_posix().replace("'", "''") + "'"
            query = (
                f"SELECT isin, trade_date, {yield_column} AS last_sale_yield, {price_column} AS price "
                f"FROM read_parquet({parquet_path_sql}) "
                f"WHERE trade_date BETWEEN ? AND ? AND isin IN ({placeholders}) ORDER BY isin, trade_date"
            )
            query_parameters = [query_start, query_end, *bonds]
        records = connection.execute(query, query_parameters).fetchall()
    finally:
        connection.close()

    observations: dict[str, list[tuple[date, float]]] = {isin: [] for isin in bonds}
    price_observations: dict[str, list[tuple[date, float]]] = {isin: [] for isin in bonds}
    signal_dates: set[date] = set()
    percentile_history_dates: set[date] = set()
    for isin, trade_date, raw_yield, raw_price in records:
        value = clean_trace_value("last_sale_yield", raw_yield)
        clean_price = clean_trace_value("price", raw_price)
        if value is not None:
            observations[isin].append((trade_date, value))
            if start_date <= trade_date <= end_date:
                signal_dates.add(trade_date)
            elif predictor_id == PREDICTOR_PARTITION_PERCENTILE and query_start <= trade_date < start_date:
                percentile_history_dates.add(trade_date)
        if clean_price is not None:
            price_observations[isin].append((trade_date, clean_price))
    if not signal_dates:
        raise ValueError("No valid TRACE yield observations fall within the selected test range")
    observation_dates = {isin: [row[0] for row in rows] for isin, rows in observations.items()}
    ordered_signal_dates = sorted(signal_dates | percentile_history_dates)
    evaluation_signal_dates = sorted(signal_dates)
    daily_results: list[dict[str, Any]] = []
    bond_results: list[dict[str, Any]] = []
    scatter_results: list[dict[str, Any]] = []
    rebalance_results: list[dict[str, Any]] = []
    event_rows_by_horizon: dict[int, list[dict[str, Any]]] = defaultdict(list)
    decile_returns_by_horizon: dict[int, dict[int, list[float]]] = {
        horizon: {decile: [] for decile in range(10)}
        for horizon in range(1, horizon_days + 1)
    }
    decile_bond_counts_by_horizon: dict[int, dict[int, list[int]]] = {
        horizon: {decile: [] for decile in range(10)}
        for horizon in range(1, horizon_days + 1)
    }
    event_pairs_by_horizon: dict[int, tuple[list[float], list[float]]] = {
        horizon: ([], []) for horizon in range(1, horizon_days + 1)
    }
    nav_proxy = 100.0
    total_return_nav_proxy = 100.0
    partition_history: dict[str, list[tuple[date, float]]] = defaultdict(list)
    stale_signal_yield_bonds = 0
    stale_first_prints_excluded = 0

    for date_index, as_of in enumerate(ordered_signal_dates, start=1):
        grouped: dict[str, list[dict[str, Any]]] = {}
        partitioned: dict[str, list[dict[str, Any]]] = {}
        for isin, metadata in bonds.items():
            if metadata["maturity_date"] <= as_of:
                continue
            dates = observation_dates[isin]
            obs_index = bisect_right(dates, as_of) - 1
            if obs_index < 0:
                continue
            yield_date, ytm = observations[isin][obs_index]
            if (as_of - yield_date).days > maximum_signal_age_days:
                continue
            maturity_years = (metadata["maturity_date"] - as_of).days / 365.25
            eligible = {
                **metadata,
                "yield_date": yield_date,
                "observed_yield_pct": ytm,
                "maturity_years": maturity_years,
            }
            if metadata["ticker"]:
                grouped.setdefault(metadata["ticker"], []).append(eligible)
            partitioned.setdefault(metadata["partition_value"], []).append(eligible)

        signals: list[dict[str, Any]] = []
        if predictor_id == PREDICTOR_NELSON_SIEGEL:
            for ticker, rows in grouped.items():
                if len(rows) < minimum_bonds:
                    continue
                maturities = np.asarray([row["maturity_years"] for row in rows], dtype=float)
                if len({round(float(value), 6) for value in maturities}) < 4:
                    continue
                yields_pct = np.asarray([row["observed_yield_pct"] for row in rows], dtype=float)
                try:
                    tau, beta, curve_rmse, loo_rmse, _tau_min, _tau_max = _fit_curve(maturities, yields_pct)
                except ValueError:
                    continue
                fitted = _basis(maturities, tau) @ beta
                for row, fitted_yield in zip(rows, fitted):
                    signals.append({
                        **row,
                        "as_of_date": as_of,
                        "signal_id": PREDICTOR_NELSON_SIEGEL,
                        "signal_name": "Nelson-Siegel ticker residual",
                        "signal_value_pct": float(row["observed_yield_pct"] - fitted_yield),
                        "fitted_yield_pct": float(fitted_yield),
                        "curve_rmse_pct": curve_rmse,
                        "tau_selection_loo_rmse_pct": loo_rmse,
                    })
        else:
            for partition_value, rows in partitioned.items():
                if len(rows) < minimum_bonds:
                    continue
                partition_median = float(np.median([row["observed_yield_pct"] for row in rows]))
                percentile, history_count = _historical_percentile(
                    partition_history[partition_value],
                    partition_median,
                    as_of_date=as_of,
                    lookback_days=percentile_lookback_days,
                    minimum_observations=minimum_history_observations,
                )
                if percentile is not None:
                    for row in rows:
                        signals.append({
                            **row,
                            "as_of_date": as_of,
                            "signal_id": PREDICTOR_PARTITION_PERCENTILE,
                            "signal_name": f"{partition_field} historical median-yield percentile",
                            "signal_value_pct": percentile,
                            "partition_field": partition_field,
                            "partition_median_yield_pct": partition_median,
                            "percentile_history_observations": history_count,
                            "fitted_yield_pct": None,
                            "curve_rmse_pct": None,
                            "tau_selection_loo_rmse_pct": None,
                        })
                if any(row["yield_date"] == as_of for row in rows):
                    partition_history[partition_value].append((as_of, partition_median))

        if as_of < start_date:
            if on_progress is not None:
                on_progress(date_index, len(ordered_signal_dates))
            continue

        horizon_end = as_of + timedelta(days=horizon_days)
        forward_start_boundary = as_of + timedelta(days=1)
        signal_ids = {row["isin"] for row in signals}
        raw_changes_after_signal: dict[date, dict[str, float]] = defaultdict(dict)
        for row in signals:
            isin = row["isin"]
            values = observations[isin]
            dates = observation_dates[isin]
            previous_value = float(row["observed_yield_pct"])
            previous_date = row["yield_date"]
            has_post_signal_baseline = previous_date >= as_of
            if not has_post_signal_baseline:
                stale_signal_yield_bonds += 1
            observation_index = bisect_right(dates, as_of)
            while observation_index < len(values) and values[observation_index][0] <= horizon_end:
                trade_date, value = values[observation_index]
                elapsed = (trade_date - previous_date).days
                if has_post_signal_baseline and 0 < elapsed <= maximum_yield_change_gap_days:
                    raw_changes_after_signal[trade_date][isin] = (value - previous_value) * 100.0
                elif not has_post_signal_baseline:
                    stale_first_prints_excluded += 1
                previous_value, previous_date = value, trade_date
                has_post_signal_baseline = True
                observation_index += 1
        demeaned_changes_after_signal: dict[date, dict[str, float]] = {}
        for trade_date, changes in raw_changes_after_signal.items():
            if predictor_id == PREDICTOR_PARTITION_PERCENTILE:
                # A regime signal should retain the market-wide level move;
                # cross-sectional demeaning would remove the effect being tested.
                demeaned_changes_after_signal[trade_date] = dict(changes)
                continue
            if len(changes) < MIN_DAILY_CHANGE_CROSS_SECTION:
                continue
            daily_mean = float(np.mean(list(changes.values())))
            demeaned_changes_after_signal[trade_date] = {
                isin: change - daily_mean for isin, change in changes.items()
            }
        portfolio_signals = [row for row in signals if row["maturity_date"] > forward_start_boundary]
        ordered = sorted(portfolio_signals, key=lambda row: (row["signal_value_pct"], row["isin"]))
        ordered_signal_rows = sorted(signals, key=lambda row: (row["signal_value_pct"], row["isin"]))
        signal_deciles = {
            row["isin"]: min(9, rank * 10 // len(ordered_signal_rows))
            for rank, row in enumerate(ordered_signal_rows)
        } if ordered_signal_rows else {}
        long_leg: list[dict[str, Any]] = []
        short_leg: list[dict[str, Any]] = []
        if predictor_id == PREDICTOR_PARTITION_PERCENTILE:
            partition_scores = {
                row["partition_value"]: float(row["signal_value_pct"])
                for row in portfolio_signals
            }
            ordered_partitions = sorted(partition_scores.items(), key=lambda item: (item[1], item[0]))
            if len(ordered_partitions) >= 2:
                leg_count = max(1, int(np.floor(len(ordered_partitions) * long_short_quantile)))
                short_partitions = {key for key, _ in ordered_partitions[:leg_count]}
                long_partitions = {key for key, _ in ordered_partitions[-leg_count:]}
                short_leg = [row for row in portfolio_signals if row["partition_value"] in short_partitions]
                long_leg = [row for row in portfolio_signals if row["partition_value"] in long_partitions]
        elif len(ordered) >= 10:
            leg_count = max(1, int(np.floor(len(ordered) * long_short_quantile)))
            short_leg, long_leg = ordered[:leg_count], ordered[-leg_count:]
        for row in signals:
            row["strategy_leg"] = ""
            row["bond_total_return_pct"] = None
            row["bond_total_return_status"] = "not selected"
            row["bond_total_return_error"] = ""
        cumulative_demeaned: dict[str, float] = defaultdict(float)
        cumulative_raw: dict[str, float] = defaultdict(float)
        cumulative_portfolio_raw: dict[str, float] = defaultdict(float)
        active_isins: set[str] = set()
        scatter_outcomes: dict[str, list[float | None]] = {
            row["isin"]: [None] * horizon_days for row in signals
        }
        maximum_horizon_point: dict[str, Any] = {}
        for horizon in range(1, horizon_days + 1):
            change_date = as_of + timedelta(days=horizon)
            for isin, change in demeaned_changes_after_signal.get(change_date, {}).items():
                if isin in signal_ids:
                    cumulative_demeaned[isin] += change
                    active_isins.add(isin)
            if change_date in raw_changes_after_signal:
                for isin, change in raw_changes_after_signal.get(change_date, {}).items():
                    if isin in signal_ids:
                        cumulative_raw[isin] += change
                        cumulative_portfolio_raw[isin] += change

            pairs = [row for row in signals if row["isin"] in active_isins]
            side_adjusted_targets = [-cumulative_demeaned[row["isin"]] for row in pairs]
            signal_values = [row["signal_value_pct"] for row in pairs]
            pearson = _correlation(signal_values, side_adjusted_targets)
            spearman = _correlation(signal_values, side_adjusted_targets, spearman=True)
            pooled_signals, pooled_targets = event_pairs_by_horizon[horizon]
            pooled_signals.extend(signal_values)
            pooled_targets.extend(side_adjusted_targets)

            long_leg_proxy = [
                -float(row["modified_duration_years"]) * cumulative_portfolio_raw[row["isin"]]
                if row["modified_duration_years"] is not None else 0.0
                for row in long_leg
            ]
            short_leg_proxy = [
                -float(row["modified_duration_years"]) * cumulative_portfolio_raw[row["isin"]]
                if row["modified_duration_years"] is not None else 0.0
                for row in short_leg
            ]
            side_adjusted_performance = (
                float(np.mean(long_leg_proxy) - np.mean(short_leg_proxy))
                if long_leg and short_leg else None
            )
            decile_outcomes: dict[int, list[float]] = {decile: [] for decile in range(10)}
            population_outcomes = {
                row["isin"]: -cumulative_demeaned[row["isin"]]
                for row in pairs
            }
            population_average_outcome = (
                float(np.mean(list(population_outcomes.values()))) if population_outcomes else None
            )
            if population_average_outcome is not None:
                for row in pairs:
                    outcome = population_outcomes[row["isin"]] - population_average_outcome
                    scatter_outcomes[row["isin"]][horizon - 1] = outcome
                    decile_outcomes[signal_deciles[row["isin"]]].append(outcome)
            for decile, outcomes in decile_outcomes.items():
                if outcomes and population_average_outcome is not None:
                    decile_returns_by_horizon[horizon][decile].append(
                        float(np.mean(outcomes))
                    )
                    decile_bond_counts_by_horizon[horizon][decile].append(len(outcomes))
            strategy_bonds = (*long_leg, *short_leg)
            change_coverage = (
                100.0 * sum(row["isin"] in active_isins and row["modified_duration_years"] is not None
                            for row in strategy_bonds) / len(strategy_bonds)
                if strategy_bonds else None
            )
            event_row = {
                "as_of_date": as_of.isoformat(),
                "horizon_days": horizon,
                "horizon_date": change_date.isoformat(),
                "signal_bonds_with_forward_change": len(pairs),
                "pearson_side_adjusted_outcome": pearson,
                "spearman_side_adjusted_outcome": spearman,
                "side_adjusted_long_short_yield_return_proxy_bps": side_adjusted_performance,
                "long_short_change_coverage_pct": change_coverage,
            }
            event_rows_by_horizon[horizon].append(event_row)
            if horizon == horizon_days:
                maximum_horizon_point = event_row

        # The long-short holdings are selected from information available at T,
        # regardless of whether their future TRACE observations exist.
        total_return_by_isin: dict[str, float] = {}
        for leg_name, leg in (("long", long_leg), ("short", short_leg)):
            for row in leg:
                row["strategy_leg"] = leg_name
                row["bond_total_return_status"] = "unavailable"
                row["bond_total_return_error"] = "Coupon or valid boundary TRACE price unavailable"
                coupon = row["coupon_pct"]
                if coupon is None or row["maturity_date"] <= forward_start_boundary:
                    continue
                try:
                    result = calculate_historical_bond_total_return(
                        isin=row["isin"],
                        start_date=forward_start_boundary,
                        end_date=horizon_end,
                        coupon_pct=coupon,
                        maturity_date=row["maturity_date"],
                        database_path=database_path,
                        max_price_age_days=maximum_price_age_days,
                        price_observations=price_observations[row["isin"]],
                        value_on_requested_dates=True,
                    )
                except Exception as error:
                    row["bond_total_return_error"] = str(error)
                    continue
                row["bond_total_return_pct"] = result.total_return_pct
                row["bond_total_return_status"] = "priced"
                row["bond_total_return_error"] = ""
                total_return_by_isin[row["isin"]] = result.total_return
        selected_count = len(long_leg) + len(short_leg)
        total_return_coverage = 100.0 * len(total_return_by_isin) / selected_count if selected_count else None
        total_return_pct = None
        if long_leg and short_leg and total_return_by_isin:
            # Preserve the target equal weights: unpriced bonds contribute zero
            # and coverage makes the resulting partial observation explicit.
            long_contribution = sum(
                total_return_by_isin.get(row["isin"], 0.0) / len(long_leg) for row in long_leg
            )
            short_contribution = sum(
                total_return_by_isin.get(row["isin"], 0.0) / len(short_leg) for row in short_leg
            )
            total_return_pct = 100.0 * (long_contribution - short_contribution)

        # Record every fitted signal at the maximum horizon, including missing
        # outcomes, so coverage remains inspectable in the export.
        for row in signals:
            row["cumulative_demeaned_yield_change_bps"] = (
                cumulative_demeaned.get(row["isin"])
                if predictor_id == PREDICTOR_NELSON_SIEGEL else None
            )
            row["side_adjusted_yield_change_bps"] = (
                -cumulative_demeaned[row["isin"]] if row["isin"] in active_isins else None
            )
            row["raw_cumulative_yield_change_bps"] = (
                cumulative_raw[row["isin"]] if row["isin"] in active_isins else None
            )
            row["bond_total_return_coverage_pct"] = total_return_coverage if row["strategy_leg"] else None
            bond_results.append({
                **row,
                "maturity_date": row["maturity_date"].isoformat(),
                "yield_date": row["yield_date"].isoformat(),
                "as_of_date": row["as_of_date"].isoformat(),
                "horizon_end_date": horizon_end.isoformat(),
            })
            scatter_results.append({
                "as_of_date": as_of.isoformat(),
                "isin": row["isin"],
                "partition_value": row["partition_value"],
                "ticker": row["ticker"],
                "signal_value_pct": row["signal_value_pct"],
                "side_adjusted_outcomes_by_horizon_bps": scatter_outcomes[row["isin"]],
            })

        daily_results.append({
            "as_of_date": as_of.isoformat(),
            "horizon_end_date": horizon_end.isoformat(),
            "signal_bonds_with_forward_change": maximum_horizon_point.get("signal_bonds_with_forward_change", 0),
            "ticker_curves_fitted": len({row["ticker"] for row in signals if row["ticker"]})
            if predictor_id == PREDICTOR_NELSON_SIEGEL else len({row["partition_value"] for row in signals}),
            "pearson_side_adjusted_outcome": maximum_horizon_point.get("pearson_side_adjusted_outcome"),
            "spearman_side_adjusted_outcome": maximum_horizon_point.get("spearman_side_adjusted_outcome"),
            "long_short_quantile": long_short_quantile,
            "long_positions": len(long_leg),
            "short_positions": len(short_leg),
            "side_adjusted_long_short_yield_return_proxy_bps": maximum_horizon_point.get("side_adjusted_long_short_yield_return_proxy_bps"),
            "long_short_total_return_pct": total_return_pct,
            "long_short_total_return_coverage_pct": total_return_coverage,
        })

        # Next-period rebalanced portfolio series; positions are formed at T
        # and measured on the next observed TRACE yield date.
        date_position = date_index - 1
        if date_position + 1 < len(ordered_signal_dates) and long_leg and short_leg:
            next_signal_date = ordered_signal_dates[date_position + 1]
            # Accumulate raw observed daily moves for portfolio P&L. De-meaning
            # is reserved for signal correlation and horizon analysis.
            next_changes: dict[str, float] = defaultdict(float)
            for change_date, changes in raw_changes_after_signal.items():
                if as_of < change_date <= next_signal_date:
                    for isin, change in changes.items():
                        next_changes[isin] += change
            long_returns = [
                -float(row["modified_duration_years"]) * next_changes.get(row["isin"], 0.0)
                if row["modified_duration_years"] is not None else 0.0 for row in long_leg
            ]
            short_returns = [
                -float(row["modified_duration_years"]) * next_changes.get(row["isin"], 0.0)
                if row["modified_duration_years"] is not None else 0.0 for row in short_leg
            ]
            if long_leg and short_leg:
                period_return_bps = float(np.mean(long_returns) - np.mean(short_returns))
                coverage_count = sum(
                    row["modified_duration_years"] is not None and row["isin"] in next_changes
                    for row in (*long_leg, *short_leg)
                )
                coverage_pct = 100.0 * coverage_count / (len(long_leg) + len(short_leg))
                nav_proxy *= 1.0 + period_return_bps / 10_000.0

                period_start = as_of + timedelta(days=1)
                period_end = next_signal_date + timedelta(days=1)
                step_total_return_by_isin: dict[str, float] = {}
                for row in (*long_leg, *short_leg):
                    coupon = row["coupon_pct"]
                    if coupon is None or row["maturity_date"] <= period_start:
                        continue
                    try:
                        result = calculate_historical_bond_total_return(
                            isin=row["isin"],
                            start_date=period_start,
                            end_date=period_end,
                            coupon_pct=coupon,
                            maturity_date=row["maturity_date"],
                            database_path=database_path,
                            max_price_age_days=maximum_price_age_days,
                            price_observations=price_observations[row["isin"]],
                            value_on_requested_dates=True,
                        )
                    except Exception:
                        continue
                    step_total_return_by_isin[row["isin"]] = result.total_return
                step_coverage = 100.0 * len(step_total_return_by_isin) / (len(long_leg) + len(short_leg))
                long_contribution = sum(step_total_return_by_isin.get(row["isin"], 0.0) / len(long_leg) for row in long_leg)
                short_contribution = sum(step_total_return_by_isin.get(row["isin"], 0.0) / len(short_leg) for row in short_leg)
                total_return_contribution_pct = 100.0 * (long_contribution - short_contribution)
                total_return_nav_proxy *= 1.0 + (long_contribution - short_contribution)
                rebalance_results.append({
                    "signal_date": as_of.isoformat(),
                    "holding_date": next_signal_date.isoformat(),
                    "holding_period_days": (next_signal_date - as_of).days,
                    "long_positions": len(long_leg),
                    "short_positions": len(short_leg),
                    "side_adjusted_yield_return_proxy_bps": period_return_bps,
                    "yield_change_coverage_pct": coverage_pct,
                    "yield_only_nav_index": nav_proxy,
                    "covered_total_return_contribution_pct": total_return_contribution_pct,
                    "total_return_coverage_pct": step_coverage,
                    "covered_total_return_nav_index": total_return_nav_proxy,
                })
        if on_progress is not None:
            on_progress(date_index, len(ordered_signal_dates))

    event_curve_results: list[dict[str, Any]] = []
    for horizon in range(1, horizon_days + 1):
        observations_at_horizon = event_rows_by_horizon[horizon]
        valid_pearson = [row["pearson_side_adjusted_outcome"] for row in observations_at_horizon
                         if row["pearson_side_adjusted_outcome"] is not None]
        valid_spearman = [row["spearman_side_adjusted_outcome"] for row in observations_at_horizon
                          if row["spearman_side_adjusted_outcome"] is not None]
        valid_performance = [row["side_adjusted_long_short_yield_return_proxy_bps"] for row in observations_at_horizon
                             if row["side_adjusted_long_short_yield_return_proxy_bps"] is not None]
        valid_coverage = [row["long_short_change_coverage_pct"] for row in observations_at_horizon
                          if row["long_short_change_coverage_pct"] is not None]
        pooled_signals, pooled_targets = event_pairs_by_horizon[horizon]
        decile_means = [
            float(np.mean(decile_returns_by_horizon[horizon][decile]))
            if decile_returns_by_horizon[horizon][decile] else None
            for decile in range(10)
        ]
        decile_date_counts = [len(decile_returns_by_horizon[horizon][decile]) for decile in range(10)]
        decile_mean_bond_counts = [
            float(np.mean(decile_bond_counts_by_horizon[horizon][decile]))
            if decile_bond_counts_by_horizon[horizon][decile] else None
            for decile in range(10)
        ]
        event_curve_results.append({
            "horizon_days": horizon,
            "dates_with_correlation": len(valid_spearman),
            "bond_date_pairs": len(pooled_signals),
            "mean_daily_side_adjusted_pearson": float(np.mean(valid_pearson)) if valid_pearson else None,
            "mean_daily_side_adjusted_spearman": float(np.mean(valid_spearman)) if valid_spearman else None,
            "pooled_side_adjusted_pearson": _correlation(pooled_signals, pooled_targets),
            "pooled_side_adjusted_spearman": _correlation(pooled_signals, pooled_targets, spearman=True),
            "mean_daily_side_adjusted_performance_bps": float(np.mean(valid_performance)) if valid_performance else None,
            "median_daily_side_adjusted_performance_bps": float(np.median(valid_performance)) if valid_performance else None,
            "mean_long_short_change_coverage_pct": float(np.mean(valid_coverage)) if valid_coverage else None,
            "decile_average_outperformance_bps": decile_means,
            "decile_dates_with_returns": decile_date_counts,
            "decile_mean_bond_counts": decile_mean_bond_counts,
            **{f"decile_{decile + 1}_outperformance_bps": decile_means[decile]
               for decile in range(10)},
        })

    max_horizon = event_curve_results[-1]
    valid_pearson = [row["pearson_side_adjusted_outcome"] for row in daily_results
                     if row["pearson_side_adjusted_outcome"] is not None]
    valid_spearman = [row["spearman_side_adjusted_outcome"] for row in daily_results
                      if row["spearman_side_adjusted_outcome"] is not None]
    valid_long_short = [row["side_adjusted_long_short_yield_return_proxy_bps"] for row in daily_results
                        if row["side_adjusted_long_short_yield_return_proxy_bps"] is not None]
    valid_total_returns = [row["long_short_total_return_pct"] for row in daily_results
                           if row["long_short_total_return_pct"] is not None]
    valid_return_coverages = [row["long_short_total_return_coverage_pct"] for row in daily_results
                              if row["long_short_total_return_coverage_pct"] is not None]
    valid_rebalance_returns = [row["side_adjusted_yield_return_proxy_bps"] for row in rebalance_results]
    metadata = {
        "signal_id": predictor_id,
        "signal_name": "Nelson-Siegel ticker residual"
        if predictor_id == PREDICTOR_NELSON_SIEGEL else "Historical partition median-yield percentile",
        "methodology": METHODOLOGY if predictor_id == PREDICTOR_NELSON_SIEGEL
        else "Trailing empirical percentile of partition median TRACE yield",
        "model_version": "nelson_siegel_loo_tau_v2" if predictor_id == PREDICTOR_NELSON_SIEGEL
        else "partition_yield_percentile_trailing_v1",
        "evaluation_version": "daily_demeaned_side_adjusted_v2"
        if predictor_id == PREDICTOR_NELSON_SIEGEL else "daily_raw_yield_change_side_adjusted_v1",
        "trace_cleaning_policy_version": TRACE_POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "test_start_date": start_date.isoformat(),
        "test_end_date": end_date.isoformat(),
        "horizon_days": horizon_days,
        "bond_list_path": str(bond_list_path.resolve()),
        "trace_database_path": str(database_path.resolve()),
        "data_source": data_source,
        "data_source_path": str(resolved_data_path),
        "data_source_uses_retrospective_cleaning": False,
        "data_source_contains_retrospective_outlier_flags": data_source == "cleaned_parquet",
        "minimum_bonds_per_ticker": minimum_bonds if predictor_id == PREDICTOR_NELSON_SIEGEL else None,
        "minimum_bonds_per_partition": minimum_bonds if predictor_id == PREDICTOR_PARTITION_PERCENTILE else None,
        "partition_field": partition_field if predictor_id == PREDICTOR_PARTITION_PERCENTILE else None,
        "signal_value_unit": "yield_percentage_points" if predictor_id == PREDICTOR_NELSON_SIEGEL
        else "percentile_rank_0_to_100",
        "percentile_lookback_days": percentile_lookback_days if predictor_id == PREDICTOR_PARTITION_PERCENTILE else None,
        "minimum_history_observations": minimum_history_observations if predictor_id == PREDICTOR_PARTITION_PERCENTILE else None,
        "maximum_signal_age_days": maximum_signal_age_days,
        "maximum_yield_change_gap_days": maximum_yield_change_gap_days,
        "maximum_price_age_days": maximum_price_age_days,
        "long_short_quantile": long_short_quantile,
        "daily_change_definition": "post-signal differences from each bond's latest available signal-date yield, expressed in bps; later increments use consecutive valid yields; gaps above maximum_yield_change_gap_days omitted",
        "daily_demeaning_definition": "not applied to the historical partition percentile; Nelson-Siegel residual outcomes subtract the per-date cross-sectional mean and omit dates with fewer than five changes",
        "stale_signal_yield_outcome_policy": "when a bond's signal yield predates the signal date, omit its first post-signal yield change because that move spans time before the signal; include changes only after two post-signal yield observations are available",
        "minimum_daily_change_cross_section": MIN_DAILY_CHANGE_CROSS_SECTION
        if predictor_id == PREDICTOR_NELSON_SIEGEL else None,
        "side_adjustment_definition": "multiply the forward yield change by -1 so a positive value is favorable to a high-yield signal; residual outcomes are cross-sectionally de-meaned, percentile outcomes are not",
        "horizon_decile_definition": "rank the full daily signal cross-section into ten equal-count deciles; within each signal date and forward horizon, calculate each decile's mean cumulative side-adjusted cross-sectionally de-meaned yield change in bps, subtract the available population average, then average daily decile outperformance across signal dates; no duration scaling or long-short tail cutoff is applied",
        "spread_measurement": "TRACE yield-level/change proxy; no Treasury benchmark-relative spread available",
        "percentile_definition": "mid-rank percentile of the partition median latest TRACE yield versus strictly prior partition medians in trailing history; partition signals apply to all constituent bonds and portfolio legs are bond equal-weighted"
        if predictor_id == PREDICTOR_PARTITION_PERCENTILE else None,
        "strategy_return_definition": "equal-weighted bonds in the top signal quantile long and bottom quantile short; percentile signals select whole partition tails; fixed modified-duration yield-only return proxy uses raw observed yield changes without cross-sectional demeaning; missing changes contribute zero and coverage is reported",
        "total_return_definition": "at maximum horizon, equal-weighted long minus short fixed-rate bond total return; unpriced positions contribute zero at their target weight and coverage is reported",
        "rebalanced_series_definition": "daily signal selection held to next TRACE signal date; show covered total-return contribution and duration-adjusted yield-change proxy using raw observed yield changes without cross-sectional demeaning; missing position contributions are zero and coverage is reported",
    }
    summary = {
        **metadata,
        "signal_dates": len(evaluation_signal_dates),
        "stale_signal_yield_bonds": stale_signal_yield_bonds,
        "stale_first_prints_excluded": stale_first_prints_excluded,
        "dates_with_correlation": len(valid_spearman),
        "dates_with_long_short": len(valid_long_short),
        "dates_with_total_return": len(valid_total_returns),
        "bond_date_observations": len(bond_results),
        "mean_daily_pearson_correlation_side_adjusted": float(np.mean(valid_pearson)) if valid_pearson else None,
        "mean_daily_spearman_correlation_side_adjusted": float(np.mean(valid_spearman)) if valid_spearman else None,
        "pooled_pearson_correlation_side_adjusted": max_horizon["pooled_side_adjusted_pearson"],
        "pooled_spearman_correlation_side_adjusted": max_horizon["pooled_side_adjusted_spearman"],
        "mean_daily_long_short_yield_return_proxy_bps": float(np.mean(valid_long_short)) if valid_long_short else None,
        "median_daily_long_short_yield_return_proxy_bps": float(np.median(valid_long_short)) if valid_long_short else None,
        "long_short_positive_date_fraction": float(np.mean(np.asarray(valid_long_short) > 0)) if valid_long_short else None,
        "mean_daily_long_short_total_return_pct": float(np.mean(valid_total_returns)) if valid_total_returns else None,
        "median_daily_long_short_total_return_pct": float(np.median(valid_total_returns)) if valid_total_returns else None,
        "total_return_positive_date_fraction": float(np.mean(np.asarray(valid_total_returns) > 0)) if valid_total_returns else None,
        "mean_long_short_total_return_coverage_pct": float(np.mean(valid_return_coverages)) if valid_return_coverages else None,
        "rebalanced_periods": len(rebalance_results),
        "mean_rebalanced_period_yield_return_proxy_bps": float(np.mean(valid_rebalance_returns)) if valid_rebalance_returns else None,
        "rebalanced_yield_only_nav_index": rebalance_results[-1]["yield_only_nav_index"] if rebalance_results else 100.0,
        "rebalanced_covered_total_return_nav_index": rebalance_results[-1]["covered_total_return_nav_index"] if rebalance_results else 100.0,
    }
    for rows in (daily_results, bond_results, rebalance_results):
        for row in rows:
            row.update(metadata)
    return {
        "summary": summary,
        "date_results": daily_results,
        "event_curve_results": event_curve_results,
        "scatter_results": scatter_results,
        "bond_results": bond_results,
        "rebalance_results": rebalance_results,
    }
