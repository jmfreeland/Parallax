"""Single-bond total and financing-excess return time series."""

from __future__ import annotations

import csv
import math
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from .bond_returns import calculate_historical_bond_total_return
from .trace_cleaning import clean_trace_value
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


DATE_COLUMNS = ("date", "observation_date", "trade_date", "effective_date")


def financing_rate_columns(path: Path) -> tuple[str, tuple[str, ...]]:
    """Return the detected date field and numeric-looking rate columns."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
    date_column = next((name for name in fields if name.lower() in DATE_COLUMNS), "")
    if not date_column:
        raise ValueError(f"Financing-rate CSV needs a date column named one of: {', '.join(DATE_COLUMNS)}")
    return date_column, tuple(field for field in fields if field != date_column)


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
            maturity = _parse_date(row.get("maturity_date"))
            if maturity is None:
                raise ValueError(f"Bond {isin} has no valid maturity date in the bond list")
            return {
                "isin": isin,
                "issuer_name": (row.get("issuer_name") or "").strip(),
                "coupon_pct": coupon,
                "maturity_date": maturity,
            }
    raise ValueError(f"ISIN {isin} was not found in {bond_list_path}")


def _load_financing_history(
    path: Path, rate_column: str
) -> list[tuple[date, float]]:
    date_column, rate_columns = financing_rate_columns(path)
    if rate_column not in rate_columns:
        raise ValueError(f"Rate column {rate_column!r} is not in {path}")
    observations: dict[date, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            observed_date = _parse_date(row.get(date_column))
            raw_rate = (row.get(rate_column) or "").strip()
            if observed_date is None or not raw_rate or raw_rate in {".", "NA", "N/A", "null"}:
                continue
            try:
                rate_pct = float(raw_rate)
            except ValueError:
                continue
            if math.isfinite(rate_pct) and rate_pct > -100.0:
                observations[observed_date] = rate_pct
    history = sorted(observations.items())
    if not history:
        raise ValueError(f"No valid rate observations found in {path} column {rate_column!r}")
    return history


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
    financing_rate_csv_path: Path,
    financing_rate_column: str,
    financing_spread_bps: float = 0.0,
    maximum_price_age_days: int = 30,
    maximum_financing_rate_age_days: int = 7,
    bond_list_path: Path = DEFAULT_SNAPSHOT,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Mark a bond on business days and calculate log excess return vs funding.

    TRACE clean prices are carried forward between prints. Financing rates use
    the latest observation known at the start of each interval, then add the
    configured annualized spread. The financing accrual uses ACT/365 simple
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
    if not financing_rate_csv_path.exists():
        raise FileNotFoundError(f"Financing-rate CSV not found: {financing_rate_csv_path}")
    if not Path(database_path).exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    terms = _load_bond_terms(Path(bond_list_path), normalized_isin)
    maturity_date: date = terms["maturity_date"]
    if maturity_date <= start_date:
        raise ValueError("The bond matures on or before the selected start date")
    rates = _load_financing_history(Path(financing_rate_csv_path), financing_rate_column)
    rate_dates = [observed_date for observed_date, _ in rates]
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

    rows: list[dict[str, Any]] = [{
        "date": start_date.isoformat(),
        "price_observation_date": None,
        "price_age_days": None,
        "clean_price_per_100": None,
        "period_days": 0,
        "bond_total_return_pct": 0.0,
        "financing_rate_pct": None,
        "financing_spread_bps": financing_spread_bps,
        "financing_log_return": 0.0,
        "bond_log_return": 0.0,
        "log_excess_return": 0.0,
        "cumulative_bond_index": 100.0,
        "cumulative_financing_index": 100.0,
        "cumulative_excess_index": 100.0,
        "mark_status": "start",
    }]
    previous_date = start_date
    cumulative_bond_log = 0.0
    cumulative_financing_log = 0.0
    cumulative_excess_log = 0.0
    daily_excess_logs: list[float] = []

    for mark_date in mark_dates:
        if mark_date > maturity_date:
            break
        period_days = (mark_date - previous_date).days
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
        financing_log_return = math.log1p(financing_simple_return)
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
        excess_log_return = bond_log_return - financing_log_return
        cumulative_bond_log += bond_log_return
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
            "base_short_rate_pct": base_rate_pct,
            "financing_rate_observation_date": rate_date.isoformat(),
            "financing_rate_age_days": rate_age_days,
            "financing_spread_bps": financing_spread_bps,
            "financing_log_return": financing_log_return,
            "bond_log_return": bond_log_return,
            "log_excess_return": excess_log_return,
            "cumulative_bond_index": 100.0 * math.exp(cumulative_bond_log),
            "cumulative_financing_index": 100.0 * math.exp(cumulative_financing_log),
            "cumulative_excess_index": 100.0 * math.exp(cumulative_excess_log),
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
        "maturity_date": maturity_date.isoformat(),
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_end_date": rows[-1]["date"],
        "financing_rate_column": financing_rate_column,
        "financing_rate_csv_path": str(Path(financing_rate_csv_path).resolve()),
        "bond_list_path": str(Path(bond_list_path).resolve()),
        "trace_database_path": str(Path(database_path).resolve()),
        "financing_spread_bps": financing_spread_bps,
        "financing_definition": "latest short-rate observation known at interval start plus annualized spread; ACT/365 simple accrual; log excess = log bond gross return minus log financing gross return",
        "price_marking_definition": "business-day marks use the latest valid TRACE clean price on or before the mark date; clean price carries forward and age is reported; accrued interest and synthetic coupon schedule are included",
        "mark_count": max(0, len(rows) - 1),
        "cumulative_bond_return_pct": 100.0 * math.expm1(cumulative_bond_log),
        "cumulative_financing_return_pct": 100.0 * math.expm1(cumulative_financing_log),
        "cumulative_log_excess_return": cumulative_excess_log,
        "cumulative_excess_return_pct": 100.0 * math.expm1(cumulative_excess_log),
        "annualized_log_excess_return_pct": annualized_excess_pct,
        "annualized_excess_volatility_pct": 100.0 * excess_volatility if excess_volatility is not None else None,
        "excess_sharpe_ratio": sharpe,
        "maximum_price_age_days": maximum_price_age_days,
        "maximum_financing_rate_age_days": maximum_financing_rate_age_days,
    }
    return {"summary": summary, "time_series": rows}
