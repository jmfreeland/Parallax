"""Build exploratory long-only portfolios from a dated bond universe."""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .trace_cleaning import clean_trace_value


WEIGHT_METHODS = {
    "Equal weight": "equal_weight",
    "Inverse price-return variance": "inverse_price_variance",
}


@dataclass(frozen=True)
class ExploratoryPortfolio:
    as_of_date: date
    weighting_method: str
    estimation_window_days: int
    universe_count: int
    matured_count: int
    missing_yield_count: int
    excluded_no_yield: int
    position_count: int
    excluded_by_method: int
    rows: tuple[dict[str, Any], ...]


def _read_bond_list(path: Path) -> tuple[list[dict[str, str]], str]:
    if not path.exists():
        raise FileNotFoundError(f"Bond list not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "isin" not in reader.fieldnames:
            raise ValueError("Bond list CSV must contain an 'isin' column")
        rows = []
        seen: set[str] = set()
        for raw in reader:
            isin = (raw.get("isin") or "").strip().upper()
            if not isin:
                continue
            if isin in seen:
                raise ValueError(f"Bond list contains duplicate ISIN: {isin}")
            seen.add(isin)
            rows.append({key: (value or "").strip() for key, value in raw.items()})
            rows[-1]["isin"] = isin
        snapshot_date = ""
        if rows and "source_as_of_date" in rows[0]:
            snapshot_date = rows[0]["source_as_of_date"]
    if not rows:
        raise ValueError("Bond list contains no ISIN rows")
    return rows, snapshot_date


def _load_valid_history(
    database_path: Path,
    as_of_date: date,
    isins: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        placeholders = ", ".join("?" for _ in isins)
        rows = connection.execute(
            f"""SELECT isin, trade_date, price, last_sale_yield
                FROM observations
                WHERE trade_date <= ? AND isin IN ({placeholders})
                ORDER BY isin, trade_date""",
            [as_of_date, *isins],
        ).fetchall()
    finally:
        connection.close()

    history: dict[str, dict[str, Any]] = {}
    for isin, trade_date, raw_price, raw_yield in rows:
        price = clean_trace_value("price", raw_price)
        ytm = clean_trace_value("last_sale_yield", raw_yield)
        if price is None and ytm is None:
            continue
        record = history.setdefault(
            isin,
            {
                "first_valid_date": trade_date,
                "last_valid_date": trade_date,
                "latest_yield": None,
                "latest_yield_date": None,
                "latest_price": None,
                "latest_price_date": None,
                "prices": [],
            },
        )
        record["last_valid_date"] = trade_date
        if price is not None:
            record["prices"].append((trade_date, price))
            record["latest_price"] = price
            record["latest_price_date"] = trade_date
        if ytm is not None:
            record["latest_yield"] = ytm
            record["latest_yield_date"] = trade_date
    return history


def load_bond_observations(
    *,
    database_path: Path,
    isin: str,
    as_of_date: date,
    window_days: int = 90,
) -> list[dict[str, Any]]:
    """Load cleaned price/yield points around a date for visual inspection.

    The returned window includes both pre-date and post-date observations. It is
    intended for chart context only; portfolio eligibility and weights must
    continue to use observations on or before the as-of date.
    """
    if window_days < 1:
        raise ValueError("window_days must be positive")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    start_date = as_of_date - timedelta(days=window_days)
    end_date = as_of_date + timedelta(days=window_days)
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            """SELECT trade_date, price, last_sale_yield FROM observations
               WHERE isin = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            [isin.strip().upper(), start_date, end_date],
        ).fetchall()
    finally:
        connection.close()
    result = []
    for trade_date, raw_price, raw_yield in rows:
        price = clean_trace_value("price", raw_price)
        ytm = clean_trace_value("last_sale_yield", raw_yield)
        if price is None and ytm is None:
            continue
        result.append(
            {
                "trade_date": trade_date,
                "day_offset": (trade_date - as_of_date).days,
                "clean_price": price,
                "clean_yield_pct": ytm,
            }
        )
    return result


def _price_return_variance(
    prices: list[tuple[date, float]],
    window_start: date,
) -> tuple[float | None, int]:
    returns = []
    previous: tuple[date, float] | None = None
    for current in prices:
        if previous is not None and current[0] >= window_start:
            returns.append(math.log(current[1] / previous[1]))
        previous = current
    if len(returns) < 2:
        return None, len(returns)
    return statistics.variance(returns), len(returns)


def generate_exploratory_portfolio(
    *,
    as_of_date: date,
    bond_list_path: Path,
    database_path: Path,
    weighting_method: str = "Equal weight",
    estimation_window_days: int = 365,
    minimum_return_observations: int = 20,
    require_valid_yield: bool = False,
) -> ExploratoryPortfolio:
    """Generate weights using only valid TRACE history available by the date.

    The supplied CSV defines the candidate bond universe. A bond enters the
    date-specific universe if it has at least one valid TRACE price or yield on
    or before the as-of date. Inverse-variance weights use consecutive observed
    log clean-price changes in the trailing calendar window; these are price-only
    exploratory weights, not total-return or liquidity-adjusted estimates.
    """
    if weighting_method not in WEIGHT_METHODS:
        raise ValueError(f"unsupported weighting method: {weighting_method}")
    if estimation_window_days < 1:
        raise ValueError("estimation_window_days must be positive")
    if minimum_return_observations < 2:
        raise ValueError("minimum_return_observations must be at least 2")

    bonds, snapshot_date = _read_bond_list(bond_list_path)
    history = _load_valid_history(database_path, as_of_date, [bond["isin"] for bond in bonds])
    candidates: list[dict[str, Any]] = []
    matured_count = 0
    history_universe_count = 0
    missing_yield_count = 0
    excluded_no_yield = 0
    for bond in bonds:
        maturity_text = bond.get("maturity_date", "")
        try:
            maturity = date.fromisoformat(maturity_text) if maturity_text else None
        except ValueError:
            maturity = None
        if maturity is not None and maturity <= as_of_date:
            matured_count += 1
            continue
        record = history.get(bond["isin"])
        if record is None:
            continue
        history_universe_count += 1
        if record["latest_yield"] is None:
            missing_yield_count += 1
            if require_valid_yield:
                excluded_no_yield += 1
                continue
        candidates.append(
            {
                **bond,
                "history_started_date": record["first_valid_date"],
                "latest_valid_observation_date": record["last_valid_date"],
                "latest_price_per_100": record["latest_price"],
                "latest_price_date": record["latest_price_date"],
                "latest_yield_pct": record["latest_yield"],
                "latest_yield_date": record["latest_yield_date"],
                "_prices": record["prices"],
            }
        )
    if not candidates:
        if require_valid_yield:
            raise ValueError(
                "No unmatured bonds in the selected list had a valid TRACE yield by this date; "
                "clear 'Require valid yield by date' to include price-only history"
            )
        raise ValueError("No bonds in the selected list had valid TRACE history by this date")

    method = WEIGHT_METHODS[weighting_method]
    selected: list[dict[str, Any]] = []
    window_start = as_of_date - timedelta(days=estimation_window_days)
    for candidate in candidates:
        variance, return_count = _price_return_variance(
            candidate["_prices"], window_start
        )
        candidate["price_return_variance"] = variance
        candidate["price_return_observations"] = return_count
        if method == "equal_weight":
            selected.append(candidate)
        elif variance is not None and return_count >= minimum_return_observations:
            selected.append(candidate)
    if not selected:
        raise ValueError(
            "No bonds have enough price-return observations for this methodology; "
            "increase the window or lower the minimum observation count"
        )

    if method == "equal_weight":
        raw_weights = [1.0] * len(selected)
    else:
        # A small floor avoids infinite weights for bonds with a flat observed price.
        raw_weights = [1.0 / max(float(row["price_return_variance"]), 1e-12) for row in selected]
    total_weight = sum(raw_weights)
    portfolio_rows = []
    for row, raw_weight in zip(selected, raw_weights):
        output = {key: value for key, value in row.items() if key != "_prices"}
        output.update(
            {
                "as_of_date": as_of_date,
                "weighting_method": weighting_method,
                "estimation_window_days": estimation_window_days,
                "snapshot_as_of_date": snapshot_date,
                "require_valid_yield": require_valid_yield,
                "weight": raw_weight / total_weight,
            }
        )
        portfolio_rows.append(output)

    return ExploratoryPortfolio(
        as_of_date=as_of_date,
        weighting_method=weighting_method,
        estimation_window_days=estimation_window_days,
        universe_count=history_universe_count,
        matured_count=matured_count,
        missing_yield_count=missing_yield_count,
        excluded_no_yield=excluded_no_yield,
        position_count=len(portfolio_rows),
        excluded_by_method=len(candidates) - len(portfolio_rows),
        rows=tuple(portfolio_rows),
    )
