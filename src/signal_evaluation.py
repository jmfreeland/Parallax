"""Walk-forward evaluation for the initial Nelson-Siegel residual signal."""

from __future__ import annotations

import csv
import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np

from .bond_returns import calculate_historical_bond_total_return
from .nelson_siegel import METHODOLOGY, _basis, _fit_curve
from .trace_cleaning import TRACE_POLICY_VERSION, clean_trace_value


ProgressCallback = Callable[[int, int], None]


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


def evaluate_nelson_siegel_signal(
    *,
    start_date: date,
    end_date: date,
    horizon_days: int,
    bond_list_path: Path,
    database_path: Path,
    minimum_bonds: int = 5,
    maximum_signal_age_days: int = 30,
    maximum_forward_start_delay_days: int = 7,
    maximum_horizon_end_age_days: int = 14,
    maximum_price_age_days: int = 7,
    long_short_quantile: float = 0.2,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Evaluate cross-sectional NS residuals on each TRACE date in a range.

    The forward dependent variable is each bond's yield change in basis points
    from its first new observation on/after T+1 through its last observation on
    or before T+horizon. TRACE does not include a Treasury benchmark here, so
    yield change is explicitly a spread-change proxy. The long-short result is
    a duration-adjusted yield-only price return proxy and, where inputs allow,
    a historical total-return long-short. Both are gross of costs.
    """
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if horizon_days < 2:
        raise ValueError("horizon_days must be at least 2")
    if minimum_bonds < 5:
        raise ValueError("minimum_bonds must be at least 5")
    if (maximum_signal_age_days < 0 or maximum_forward_start_delay_days < 0
            or maximum_horizon_end_age_days < 0 or maximum_price_age_days < 0):
        raise ValueError("observation age limits must be non-negative")
    if not 0 < long_short_quantile < 0.5:
        raise ValueError("long_short_quantile must be between 0 and 0.5")
    if not bond_list_path.exists():
        raise FileNotFoundError(f"Bond list not found: {bond_list_path}")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    bonds: dict[str, dict[str, Any]] = {}
    with bond_list_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"isin", "equity_ticker", "maturity_date"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Bond list must contain isin, equity_ticker, and maturity_date columns")
        for raw in reader:
            isin = (raw.get("isin") or "").strip().upper()
            ticker = (raw.get("equity_ticker") or "").strip().upper()
            try:
                maturity = date.fromisoformat((raw.get("maturity_date") or "").strip())
            except ValueError:
                continue
            if not isin or not ticker:
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
            }
    if not bonds:
        raise ValueError("No bonds with a mapped equity ticker and valid maturity date were found")

    query_start = start_date - timedelta(days=max(maximum_signal_age_days, maximum_price_age_days))
    query_end = end_date + timedelta(days=horizon_days)
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        records = connection.execute(
            "SELECT isin, trade_date, last_sale_yield, price FROM observations "
            "WHERE trade_date BETWEEN ? AND ? AND isin IN ("
            + ",".join("?" for _ in bonds) + ") ORDER BY isin, trade_date",
            [query_start, query_end, *bonds],
        ).fetchall()
    finally:
        connection.close()

    observations: dict[str, list[tuple[date, float]]] = {isin: [] for isin in bonds}
    price_observations: dict[str, list[tuple[date, float]]] = {isin: [] for isin in bonds}
    signal_dates: set[date] = set()
    for isin, trade_date, raw_yield, raw_price in records:
        value = clean_trace_value("last_sale_yield", raw_yield)
        clean_price = clean_trace_value("price", raw_price)
        if value is not None:
            observations[isin].append((trade_date, value))
            if start_date <= trade_date <= end_date:
                signal_dates.add(trade_date)
        if clean_price is not None:
            price_observations[isin].append((trade_date, clean_price))
    if not signal_dates:
        raise ValueError("No valid TRACE yield observations fall within the selected test range")
    observation_dates = {isin: [row[0] for row in rows] for isin, rows in observations.items()}
    ordered_signal_dates = sorted(signal_dates)
    daily_results: list[dict[str, Any]] = []
    bond_results: list[dict[str, Any]] = []

    for date_index, as_of in enumerate(ordered_signal_dates, start=1):
        grouped: dict[str, list[dict[str, Any]]] = {}
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
            grouped.setdefault(metadata["ticker"], []).append(
                {**metadata, "yield_date": yield_date, "observed_yield_pct": ytm,
                 "maturity_years": maturity_years}
            )

        signals: list[dict[str, Any]] = []
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
                    "signal_id": "nelson_siegel_residual",
                    "signal_name": "Nelson-Siegel ticker residual",
                    "signal_value_pct": float(row["observed_yield_pct"] - fitted_yield),
                    "fitted_yield_pct": float(fitted_yield),
                    "curve_rmse_pct": curve_rmse,
                    "tau_selection_loo_rmse_pct": loo_rmse,
                })

        horizon_end = as_of + timedelta(days=horizon_days)
        forward_start_boundary = as_of + timedelta(days=1)
        eligible: list[dict[str, Any]] = []
        for signal in signals:
            if signal["maturity_date"] <= horizon_end:
                continue
            dates = observation_dates[signal["isin"]]
            values = observations[signal["isin"]]
            first_index = bisect_left(dates, forward_start_boundary)
            end_index = bisect_right(dates, horizon_end) - 1
            if first_index < 0 or end_index <= first_index:
                continue
            start_date_actual, start_yield = values[first_index]
            end_date_actual, end_yield = values[end_index]
            if (start_date_actual - forward_start_boundary).days > maximum_forward_start_delay_days:
                continue
            if (horizon_end - end_date_actual).days > maximum_horizon_end_age_days:
                continue
            yield_change_bps = (end_yield - start_yield) * 100.0
            duration = signal["modified_duration_years"]
            return_proxy_bps = -duration * yield_change_bps if duration is not None else None
            eligible.append({
                **signal,
                "forward_start_boundary": forward_start_boundary,
                "forward_start_yield_date": start_date_actual,
                "forward_start_yield_pct": start_yield,
                "horizon_end_boundary": horizon_end,
                "horizon_end_yield_date": end_date_actual,
                "horizon_end_yield_pct": end_yield,
                "forward_yield_change_bps": yield_change_bps,
                "yield_duration_return_proxy_bps": return_proxy_bps,
                "forward_window_days": (end_date_actual - start_date_actual).days,
            })
        signal_values = [row["signal_value_pct"] for row in eligible]
        yield_changes = [row["forward_yield_change_bps"] for row in eligible]
        pearson = _correlation(signal_values, yield_changes)
        spearman = _correlation(signal_values, yield_changes, spearman=True)
        sorted_rows = sorted(eligible, key=lambda row: (row["signal_value_pct"], row["isin"]))
        long_short_bps = None
        long_short_total_return_pct = None
        long_short_total_return_coverage_pct = None
        long_count = short_count = 0
        if len(sorted_rows) >= 10:
            leg_count = max(1, int(np.floor(len(sorted_rows) * long_short_quantile)))
            short_leg = sorted_rows[:leg_count]
            long_leg = sorted_rows[-leg_count:]
            long_count, short_count = len(long_leg), len(short_leg)
            total_return_by_isin: dict[str, float] = {}
            for leg_name, leg in (("long", long_leg), ("short", short_leg)):
                for row in leg:
                    row["strategy_leg"] = leg_name
                    row["bond_total_return_pct"] = None
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
            coverage_count = len(total_return_by_isin)
            long_short_total_return_coverage_pct = 100.0 * coverage_count / (long_count + short_count)
            # Do not silently rebalance around selected positions with missing prices.
            if coverage_count == long_count + short_count:
                long_mean = float(np.mean([total_return_by_isin[row["isin"]] for row in long_leg]))
                short_mean = float(np.mean([total_return_by_isin[row["isin"]] for row in short_leg]))
                long_short_total_return_pct = 100.0 * (long_mean - short_mean)
            if all(row["modified_duration_years"] is not None for row in (*long_leg, *short_leg)):
                long_proxy = float(np.mean([row["yield_duration_return_proxy_bps"] for row in long_leg]))
                short_proxy = float(np.mean([row["yield_duration_return_proxy_bps"] for row in short_leg]))
                long_short_bps = long_proxy - short_proxy

        for row in eligible:
            bond_results.append({
                **row,
                "maturity_date": row["maturity_date"].isoformat(),
                "yield_date": row["yield_date"].isoformat(),
                "as_of_date": row["as_of_date"].isoformat(),
                "forward_start_boundary": row["forward_start_boundary"].isoformat(),
                "forward_start_yield_date": row["forward_start_yield_date"].isoformat(),
                "horizon_end_boundary": row["horizon_end_boundary"].isoformat(),
                "horizon_end_yield_date": row["horizon_end_yield_date"].isoformat(),
            })

        daily_results.append({
            "as_of_date": as_of.isoformat(),
            "horizon_end_date": horizon_end.isoformat(),
            "signal_bonds_with_forward_data": len(eligible),
            "ticker_curves_fitted": len({row["ticker"] for row in signals}),
            "pearson_signal_forward_yield_change": pearson,
            "spearman_signal_forward_yield_change": spearman,
            "long_short_quantile": long_short_quantile,
            "long_positions": long_count,
            "short_positions": short_count,
            "long_short_duration_adjusted_return_proxy_bps": long_short_bps,
            "long_short_total_return_pct": long_short_total_return_pct,
            "long_short_total_return_coverage_pct": long_short_total_return_coverage_pct,
        })
        if on_progress is not None:
            on_progress(date_index, len(ordered_signal_dates))

    valid_pearson = [row["pearson_signal_forward_yield_change"] for row in daily_results
                     if row["pearson_signal_forward_yield_change"] is not None]
    valid_spearman = [row["spearman_signal_forward_yield_change"] for row in daily_results
                      if row["spearman_signal_forward_yield_change"] is not None]
    valid_long_short = [row["long_short_duration_adjusted_return_proxy_bps"] for row in daily_results
                        if row["long_short_duration_adjusted_return_proxy_bps"] is not None]
    valid_total_returns = [row["long_short_total_return_pct"] for row in daily_results
                           if row["long_short_total_return_pct"] is not None]
    valid_return_coverages = [row["long_short_total_return_coverage_pct"] for row in daily_results
                              if row["long_short_total_return_coverage_pct"] is not None]
    all_signal = [float(row["signal_value_pct"]) for row in bond_results]
    all_changes = [float(row["forward_yield_change_bps"]) for row in bond_results]
    metadata = {
        "signal_id": "nelson_siegel_residual",
        "signal_name": "Nelson-Siegel ticker residual",
        "methodology": METHODOLOGY,
        "model_version": "nelson_siegel_loo_tau_v2",
        "trace_cleaning_policy_version": TRACE_POLICY_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "test_start_date": start_date.isoformat(),
        "test_end_date": end_date.isoformat(),
        "horizon_days": horizon_days,
        "bond_list_path": str(bond_list_path.resolve()),
        "trace_database_path": str(database_path.resolve()),
        "minimum_bonds_per_ticker": minimum_bonds,
        "maximum_signal_age_days": maximum_signal_age_days,
        "maximum_forward_start_delay_days": maximum_forward_start_delay_days,
        "maximum_horizon_end_age_days": maximum_horizon_end_age_days,
        "maximum_price_age_days": maximum_price_age_days,
        "long_short_quantile": long_short_quantile,
        "forward_change_definition": "first TRACE yield observation on/after T+1 to last on/before T+horizon",
        "spread_measurement": "TRACE yield change proxy; no Treasury benchmark available",
        "strategy_return_definition": "equal-weighted high-residual long minus low-residual short using fixed-rate bond total return; no transaction costs; full leg price coverage required",
        "yield_only_return_proxy_definition": "-modified-duration * yield change; excludes carry, convexity, and costs",
    }
    summary = {
        **metadata,
        "signal_dates": len(ordered_signal_dates),
        "dates_with_correlation": len(valid_spearman),
        "dates_with_long_short": len(valid_long_short),
        "dates_with_total_return": len(valid_total_returns),
        "bond_date_observations": len(bond_results),
        "mean_daily_pearson_correlation": float(np.mean(valid_pearson)) if valid_pearson else None,
        "mean_daily_spearman_correlation": float(np.mean(valid_spearman)) if valid_spearman else None,
        "pooled_pearson_correlation": _correlation(all_signal, all_changes),
        "pooled_spearman_correlation": _correlation(all_signal, all_changes, spearman=True),
        "mean_daily_long_short_return_proxy_bps": float(np.mean(valid_long_short)) if valid_long_short else None,
        "median_daily_long_short_return_proxy_bps": float(np.median(valid_long_short)) if valid_long_short else None,
        "long_short_positive_date_fraction": float(np.mean(np.asarray(valid_long_short) > 0)) if valid_long_short else None,
        "mean_daily_long_short_total_return_pct": float(np.mean(valid_total_returns)) if valid_total_returns else None,
        "median_daily_long_short_total_return_pct": float(np.median(valid_total_returns)) if valid_total_returns else None,
        "total_return_positive_date_fraction": float(np.mean(np.asarray(valid_total_returns) > 0)) if valid_total_returns else None,
        "mean_long_short_total_return_coverage_pct": float(np.mean(valid_return_coverages)) if valid_return_coverages else None,
    }
    for rows in (daily_results, bond_results):
        for row in rows:
            row.update(metadata)
    return {"summary": summary, "date_results": daily_results, "bond_results": bond_results}
