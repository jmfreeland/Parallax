"""Single-bond total and financing-excess return time series."""

from __future__ import annotations

import csv
import math
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from .bond_returns import calculate_historical_bond_total_return, calculate_modified_duration_from_yield
from .trace_cleaning import clean_trace_value
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _load_bond_terms(bond_list_path: Path, isin: str) -> dict[str, Any]:
    with bond_list_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"isin", "coupon_pct", "maturity_date"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Bond list must contain {', '.join(sorted(required))} columns")
        for row in reader:
            if (row.get("isin") or "").strip().upper() != isin:
                continue
            try:
                coupon = float(row.get("coupon_pct") or "")
            except ValueError as error:
                raise ValueError(f"Bond {isin} has no valid coupon in the bond list") from error
            if not math.isfinite(coupon) or not 0 <= coupon < 100:
                raise ValueError(f"Bond {isin} has an invalid coupon in the bond list")
            try:
                duration = float(row.get("modified_duration_years") or "")
                if duration <= 0 or not math.isfinite(duration):
                    duration = None
            except ValueError:
                duration = None
            maturity = _parse_date(row.get("maturity_date"))
            if maturity is None:
                raise ValueError(f"Bond {isin} has no valid maturity date in the bond list")
            return {
                "isin": isin,
                "issuer_name": (row.get("issuer_name") or "").strip(),
                "coupon_pct": coupon,
                "maturity_date": maturity,
                "modified_duration_years": duration,
            }
    raise ValueError(f"ISIN {isin} was not found in {bond_list_path}")


def available_treasury_tenors(database_path: Path = DATABASE_PATH) -> list[float]:
    """List available Treasury curve tenors from the shared research database."""
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT tenor_years FROM treasury_curve_observations ORDER BY tenor_years"
        ).fetchall()
    except duckdb.Error as error:
        raise ValueError("No Treasury curve history is available in the shared database.") from error
    finally:
        connection.close()
    return [float(row[0]) for row in rows]


def _load_treasury_history(
    database_path: Path, tenor_years: float
) -> list[tuple[date, float]]:
    """Load one Treasury tenor, using the most recently retrieved value per date."""
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            """SELECT observation_date, par_yield_pct FROM (
                   SELECT o.observation_date, o.par_yield_pct,
                          row_number() OVER (
                              PARTITION BY o.observation_date ORDER BY r.retrieved_at_utc DESC
                          ) AS vintage_rank
                   FROM treasury_curve_observations o
                   JOIN treasury_curve_runs r USING (curve_run_id)
                   WHERE abs(o.tenor_years - ?) < 0.00000001
                     AND o.par_yield_pct IS NOT NULL
               ) WHERE vintage_rank=1 ORDER BY observation_date""",
            [tenor_years],
        ).fetchall()
    except duckdb.Error as error:
        raise ValueError("Treasury curve history is unavailable in the shared database.") from error
    finally:
        connection.close()
    history = [(observed_date, float(rate)) for observed_date, rate in rows]
    if not history:
        raise ValueError(f"No Treasury history is available for the {tenor_years:g}-year tenor.")
    return history


def _load_g_spread_history(
    database_path: Path, isin: str
) -> tuple[str, list[tuple[date, float, float, float]]]:
    """Load matched Treasury yields and G-spreads from the latest usable snapshot."""
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        latest = connection.execute(
            "SELECT snapshot_id FROM spread_snapshot_runs ORDER BY generated_at_utc DESC LIMIT 1"
        ).fetchone()
        if not latest:
            raise ValueError("No G-spread snapshot is available. Build one from the Data page.")
        snapshot_id = str(latest[0])
        rows = connection.execute(
            """SELECT trade_date, treasury_par_yield_pct, g_spread_bps, trace_yield_pct
               FROM bond_spread_observations
               WHERE snapshot_id=? AND upper(isin)=? AND treasury_par_yield_pct IS NOT NULL
                 AND g_spread_bps IS NOT NULL AND trace_yield_pct IS NOT NULL
                 AND NOT yield_reversal_flagged
               ORDER BY trade_date""",
            [snapshot_id, isin],
        ).fetchall()
    except duckdb.Error as error:
        raise ValueError("A valid G-spread snapshot is required for the Treasury hedge.") from error
    finally:
        connection.close()
    history = [(observed_date, float(treasury_yield), float(spread_bps), float(trace_yield))
               for observed_date, treasury_yield, spread_bps, trace_yield in rows]
    if not history:
        raise ValueError(f"The latest G-spread snapshot has no valid observations for {isin}.")
    return snapshot_id, history


def _business_dates(start_date: date, end_date: date) -> list[date]:
    dates: list[date] = []
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def analyze_bond_return_series(
    *,
    isin: str,
    start_date: date,
    end_date: date,
    treasury_tenor_years: float = 1.0 / 12.0,
    financing_spread_bps: float = 0.0,
    include_g_spread_hedge: bool = True,
    include_financing_comparison: bool = True,
    maximum_price_age_days: int = 30,
    maximum_financing_rate_age_days: int = 7,
    bond_list_path: Path = DEFAULT_SNAPSHOT,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Mark a bond on business days and calculate log excess return vs funding.

    TRACE clean prices are carried forward between prints. Financing rates use
    the selected Treasury curve tenor known at the start of each interval,
    then add the configured annualized spread. The financing accrual uses ACT/365 simple
    compounding; bond total return uses the synthetic coupon schedule in
    ``bond_returns``.
    """
    normalized_isin = isin.strip().upper()
    if not normalized_isin:
        raise ValueError("An ISIN is required")
    if end_date <= start_date:
        raise ValueError("End date must be after start date")
    if not math.isfinite(financing_spread_bps):
        raise ValueError("Financing spread must be finite")
    if maximum_price_age_days < 0 or maximum_financing_rate_age_days < 0:
        raise ValueError("Maximum mark ages must be non-negative")
    if not Path(database_path).exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    terms = _load_bond_terms(Path(bond_list_path), normalized_isin)
    maturity_date: date = terms["maturity_date"]
    if maturity_date <= start_date:
        raise ValueError("The bond matures on or before the selected start date")
    rates = (
        _load_treasury_history(Path(database_path), treasury_tenor_years)
        if include_financing_comparison else []
    )
    rate_dates = [observed_date for observed_date, _ in rates]
    g_spread_snapshot_id: str | None = None
    g_spread_history: list[tuple[date, float, float, float]] = []
    g_spread_dates: list[date] = []
    try:
        g_spread_snapshot_id, g_spread_history = _load_g_spread_history(
            Path(database_path), normalized_isin
        )
        g_spread_dates = [observed_date for observed_date, _tsy, _spread, _ytm in g_spread_history]
    except ValueError:
        if include_g_spread_hedge:
            raise
    if include_g_spread_hedge:
        start_spread_index = bisect_right(g_spread_dates, start_date) - 1
        if start_spread_index < 0:
            raise ValueError(f"No G-spread observation on or before {start_date} for {normalized_isin}.")
        if (start_date - g_spread_dates[start_spread_index]).days > maximum_price_age_days:
            raise ValueError(f"G-spread observation is too old at {start_date} for {normalized_isin}.")
    else:
        start_spread_index = bisect_right(g_spread_dates, start_date) - 1 if g_spread_dates else -1
    query_end = min(end_date, maturity_date)
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        raw_prices = connection.execute(
            "SELECT trade_date, price FROM observations WHERE isin = ? AND trade_date <= ? ORDER BY trade_date",
            [normalized_isin, query_end],
        ).fetchall()
    finally:
        connection.close()
    price_history = [
        (trade_date, clean_price)
        for trade_date, raw_price in raw_prices
        if (clean_price := clean_trace_value("price", raw_price)) is not None
    ]
    if not any(observed_date <= start_date for observed_date, _ in price_history):
        raise ValueError(f"No valid TRACE price on or before {start_date} for {normalized_isin}")

    mark_dates = _business_dates(start_date, query_end)
    if start_date < maturity_date <= query_end and maturity_date not in mark_dates:
        mark_dates.append(maturity_date)
        mark_dates.sort()
    if not mark_dates:
        raise ValueError("No business-day marks fall in the requested period")

    duration_cache: dict[int, float] = {}

    def duration_at_history_index(index: int) -> float:
        if index not in duration_cache:
            observed_date, _tsy, _spread, yield_pct = g_spread_history[index]
            duration_cache[index] = calculate_modified_duration_from_yield(
                as_of_date=observed_date,
                maturity_date=maturity_date,
                coupon_pct=float(terms["coupon_pct"]),
                yield_pct=yield_pct,
            )
        return duration_cache[index]

    start_spread_row = g_spread_history[start_spread_index] if start_spread_index >= 0 else None
    start_hedge_duration = (
        duration_at_history_index(start_spread_index)
        if include_g_spread_hedge and start_spread_index >= 0 else None
    )
    rows: list[dict[str, Any]] = [{
        "date": start_date.isoformat(),
        "price_observation_date": None,
        "price_age_days": None,
        "clean_price_per_100": None,
        "period_days": 0,
        "bond_total_return_pct": 0.0,
        "financing_rate_pct": None,
        "financing_spread_bps": financing_spread_bps,
        "financing_cost_return_pct": 0.0 if include_financing_comparison else None,
        "financing_log_return": 0.0 if include_financing_comparison else None,
        "bond_log_return": 0.0,
        "log_excess_return": 0.0 if include_financing_comparison else None,
        "cumulative_bond_index": 100.0,
        "cumulative_financing_index": 100.0 if include_financing_comparison else None,
        "cumulative_excess_index": 100.0 if include_financing_comparison else None,
        "treasury_hedge_yield_pct": start_spread_row[1] if start_spread_row else None,
        "hedge_modified_duration_years": start_hedge_duration,
        "treasury_benchmark_return_pct": 0.0 if include_g_spread_hedge else None,
        "g_spread_observation_date": start_spread_row[0].isoformat() if start_spread_row else None,
        "g_spread_bps": start_spread_row[2] if start_spread_row else None,
        "g_spread_change_bps": 0.0 if include_g_spread_hedge else None,
        "g_spread_carry_log_return": 0.0 if include_g_spread_hedge else None,
        "g_spread_price_log_return": 0.0 if include_g_spread_hedge else None,
        "g_spread_carry_return_pct": 0.0 if include_g_spread_hedge else None,
        "g_spread_price_return_pct": 0.0 if include_g_spread_hedge else None,
        "cumulative_g_spread_carry_index": 100.0 if include_g_spread_hedge else None,
        "g_spread_hedged_log_return": 0.0 if include_g_spread_hedge else None,
        "g_spread_hedged_return_pct": 0.0 if include_g_spread_hedge else None,
        "cumulative_g_spread_hedged_index": 100.0 if include_g_spread_hedge else None,
        "mark_status": "start",
    }]
    previous_date = start_date
    cumulative_bond_log = 0.0
    cumulative_financing_log = 0.0
    cumulative_excess_log = 0.0
    daily_excess_logs: list[float] = []
    cumulative_g_spread_hedged_log = 0.0
    cumulative_g_spread_carry_log = 0.0
    previous_spread_index = start_spread_index

    for mark_date in mark_dates:
        if mark_date > maturity_date:
            break
        period_days = (mark_date - previous_date).days
        financing_rate_pct: float | None = None
        base_rate_pct: float | None = None
        rate_date: date | None = None
        rate_age_days: int | None = None
        financing_log_return: float | None = None
        if include_financing_comparison:
            rate_index = bisect_right(rate_dates, previous_date) - 1
            if rate_index < 0:
                raise ValueError(f"No financing-rate observation on or before {previous_date}")
            rate_date, base_rate_pct = rates[rate_index]
            rate_age_days = (previous_date - rate_date).days
            if rate_age_days > maximum_financing_rate_age_days:
                raise ValueError(
                    f"Financing rate is {rate_age_days} days old at {previous_date}; "
                    f"limit is {maximum_financing_rate_age_days}"
                )
            financing_rate_pct = base_rate_pct + financing_spread_bps / 100.0
            financing_simple_return = (financing_rate_pct / 100.0) * period_days / 365.0
            if financing_simple_return <= -1.0:
                raise ValueError("Financing accrual implies a loss of 100% or more")
            financing_log_return = -math.log1p(financing_simple_return)
        bond_result = calculate_historical_bond_total_return(
            isin=normalized_isin,
            start_date=previous_date,
            end_date=mark_date,
            coupon_pct=float(terms["coupon_pct"]),
            maturity_date=maturity_date,
            database_path=Path(database_path),
            max_price_age_days=None,
            price_observations=price_history,
            value_on_requested_dates=True,
            carry_forward_stale_prices=True,
        )
        if previous_date == start_date and bond_result.start_price_age_days > maximum_price_age_days:
            raise ValueError(
                f"Starting price for {normalized_isin} is {bond_result.start_price_age_days} days old; "
                f"limit is {maximum_price_age_days}"
            )
        bond_log_return = math.log1p(bond_result.total_return)
        excess_log_return = (
            bond_log_return + financing_log_return
            if financing_log_return is not None else None
        )
        hedge_yield: float | None = None
        spread_date: date | None = None
        spread_bps: float | None = None
        spread_change_bps: float | None = None
        spread_carry_log_return: float | None = None
        spread_price_log_return: float | None = None
        treasury_benchmark_log_return: float | None = None
        hedge_duration: float | None = None
        hedged_log_return: float | None = None
        hedged_return_pct: float | None = None
        spread_index = bisect_right(g_spread_dates, mark_date) - 1 if g_spread_dates else -1
        if spread_index >= 0:
            spread_date, hedge_yield, spread_bps, _trace_yield = g_spread_history[spread_index]
            spread_age = (mark_date - spread_date).days
            if include_g_spread_hedge and spread_age > maximum_price_age_days:
                raise ValueError(
                    f"G-spread observation is {spread_age} days old at {mark_date}; "
                    f"limit is {maximum_price_age_days}"
                )
        if include_g_spread_hedge:
            if spread_index < 0:
                raise ValueError(f"No G-spread observation on or before {mark_date} for {normalized_isin}.")
            previous_spread_bps = g_spread_history[previous_spread_index][2]
            previous_treasury_yield = g_spread_history[previous_spread_index][1]
            duration = duration_at_history_index(previous_spread_index)
            hedge_duration = duration
            # The hedge removes a duration-matched Treasury benchmark return
            # from the bond's realized total return. G-spread carry and price
            # moves remain separately available as explanatory diagnostics.
            spread_carry_simple_return = previous_spread_bps / 10_000.0 * period_days / 365.0
            spread_carry_log_return = math.log1p(spread_carry_simple_return)
            spread_change_bps = spread_bps - previous_spread_bps
            spread_price_log_return = -duration * spread_change_bps / 10_000.0
            treasury_carry_simple_return = previous_treasury_yield / 100.0 * period_days / 365.0
            treasury_price_log_return = -duration * (hedge_yield - previous_treasury_yield) / 100.0
            treasury_benchmark_log_return = (
                math.log1p(treasury_carry_simple_return) + treasury_price_log_return
            )
            hedged_log_return = bond_log_return - treasury_benchmark_log_return
            hedged_return_pct = 100.0 * math.expm1(hedged_log_return)
            cumulative_g_spread_carry_log += spread_carry_log_return
            cumulative_g_spread_hedged_log += hedged_log_return
            previous_spread_index = spread_index
        cumulative_bond_log += bond_log_return
        if financing_log_return is not None and excess_log_return is not None:
            cumulative_financing_log += financing_log_return
            cumulative_excess_log += excess_log_return
            daily_excess_logs.append(excess_log_return)
        price_age = bond_result.end_price_age_days
        rows.append({
            "date": mark_date.isoformat(),
            "price_observation_date": bond_result.end_price_date.isoformat() if bond_result.end_price_date else None,
            "price_age_days": price_age,
            "clean_price_per_100": bond_result.end_clean_price_per_100,
            "period_days": period_days,
            "bond_total_return_pct": bond_result.total_return_pct,
            "financing_rate_pct": financing_rate_pct,
            "financing_cost_return_pct": (
                100.0 * math.expm1(financing_log_return)
                if financing_log_return is not None else None
            ),
            "base_short_rate_pct": base_rate_pct,
            "financing_rate_observation_date": rate_date.isoformat() if rate_date else None,
            "financing_rate_age_days": rate_age_days,
            "financing_spread_bps": financing_spread_bps,
            "financing_log_return": financing_log_return,
            "bond_log_return": bond_log_return,
            "log_excess_return": excess_log_return,
            "cumulative_bond_index": 100.0 * math.exp(cumulative_bond_log),
            "cumulative_financing_index": (
                100.0 * math.exp(cumulative_financing_log)
                if include_financing_comparison else None
            ),
            "cumulative_excess_index": (
                100.0 * math.exp(cumulative_excess_log)
                if include_financing_comparison else None
            ),
            "treasury_hedge_yield_pct": hedge_yield,
            "hedge_modified_duration_years": hedge_duration,
            "treasury_benchmark_return_pct": (
                100.0 * math.expm1(treasury_benchmark_log_return)
                if treasury_benchmark_log_return is not None else None
            ),
            "g_spread_observation_date": spread_date.isoformat() if spread_date else None,
            "g_spread_bps": spread_bps,
            "g_spread_change_bps": spread_change_bps,
            "g_spread_carry_log_return": spread_carry_log_return,
            "g_spread_price_log_return": spread_price_log_return,
            "g_spread_carry_return_pct": (
                100.0 * math.expm1(spread_carry_log_return)
                if spread_carry_log_return is not None else None
            ),
            "cumulative_g_spread_carry_index": (
                100.0 * math.exp(cumulative_g_spread_carry_log)
                if include_g_spread_hedge else None
            ),
            "g_spread_price_return_pct": (
                100.0 * math.expm1(spread_price_log_return)
                if spread_price_log_return is not None else None
            ),
            "g_spread_hedged_log_return": hedged_log_return,
            "g_spread_hedged_return_pct": hedged_return_pct,
            "cumulative_g_spread_hedged_index": (
                100.0 * math.exp(cumulative_g_spread_hedged_log)
                if include_g_spread_hedge else None
            ),
            "mark_status": "maturity" if mark_date == maturity_date else (
                "stale price carried" if price_age is not None and price_age > maximum_price_age_days else "marked"
            ),
        })
        previous_date = mark_date
        if mark_date == maturity_date:
            break

    elapsed_days = (date.fromisoformat(rows[-1]["date"]) - start_date).days
    annualized_excess_pct = (
        100.0 * math.expm1(cumulative_excess_log * 365.0 / elapsed_days)
        if elapsed_days > 0 else None
    )
    excess_volatility = None
    sharpe = None
    if len(daily_excess_logs) > 1:
        mean_daily = sum(daily_excess_logs) / len(daily_excess_logs)
        variance = sum((value - mean_daily) ** 2 for value in daily_excess_logs) / (len(daily_excess_logs) - 1)
        excess_volatility = math.sqrt(variance * 252.0)
        if excess_volatility > 0:
            sharpe = mean_daily * 252.0 / excess_volatility
    summary = {
        "isin": normalized_isin,
        "issuer_name": terms["issuer_name"],
        "coupon_pct": terms["coupon_pct"],
        "modified_duration_years": terms["modified_duration_years"],
        "maturity_date": maturity_date.isoformat(),
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_end_date": rows[-1]["date"],
        "treasury_tenor_years": treasury_tenor_years if include_financing_comparison else None,
        "bond_list_path": str(Path(bond_list_path).resolve()),
        "trace_database_path": str(Path(database_path).resolve()),
        "financing_spread_bps": financing_spread_bps,
        "include_g_spread_hedge": include_g_spread_hedge,
        "financing_comparison_calculated": include_financing_comparison,
        "g_spread_snapshot_id": g_spread_snapshot_id,
        "cumulative_g_spread_hedged_return_pct": (
            100.0 * math.expm1(cumulative_g_spread_hedged_log)
            if include_g_spread_hedge else None
        ),
        "g_spread_hedge_definition": (
            "realized bond log total return less the duration-matched Treasury benchmark log return; benchmark return includes par-yield carry and a duration-scaled price move; duration is recalculated with QuantLib from each point-in-time observed bond yield; G-spread carry and spread-price moves are reported separately"
            if include_g_spread_hedge else "not calculated"
        ),
        "cumulative_g_spread_carry_return_pct": (
            100.0 * math.expm1(cumulative_g_spread_carry_log)
            if include_g_spread_hedge else None
        ),
        "financing_definition": (
            "latest observation of the selected Treasury curve tenor known at interval start plus configured spread; "
            "ACT/365 simple accrual; financing cost is recorded as a negative return contribution and added to bond log return"
            if include_financing_comparison else "not calculated while the G-spread hedge is active"
        ),
        "price_marking_definition": "business-day marks use the latest valid TRACE clean price on or before the mark date; clean price carries forward and age is reported; accrued interest and synthetic coupon schedule are included",
        "mark_count": max(0, len(rows) - 1),
        "cumulative_bond_return_pct": 100.0 * math.expm1(cumulative_bond_log),
        "cumulative_financing_return_pct": (
            100.0 * math.expm1(cumulative_financing_log)
            if include_financing_comparison else None
        ),
        "cumulative_log_excess_return": cumulative_excess_log,
        "cumulative_excess_return_pct": (
            100.0 * math.expm1(cumulative_excess_log)
            if include_financing_comparison else None
        ),
        "annualized_log_excess_return_pct": annualized_excess_pct if include_financing_comparison else None,
        "annualized_excess_volatility_pct": (
            100.0 * excess_volatility if include_financing_comparison and excess_volatility is not None else None
        ),
        "excess_sharpe_ratio": sharpe if include_financing_comparison else None,
        "maximum_price_age_days": maximum_price_age_days,
        "maximum_financing_rate_age_days": maximum_financing_rate_age_days,
    }
    return {"summary": summary, "time_series": rows}
