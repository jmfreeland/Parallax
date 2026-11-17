"""Point-in-time bond curves and observed level histories for inspection."""

from __future__ import annotations

import csv
import math
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .nelson_siegel import _basis, _fit_curve
from .trace_cleaning import clean_trace_value
from .trace_history import DATABASE_PATH


def _as_date(value: Any) -> date | None:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _history_zscores(rows: list[tuple[date, float]], lookback_days: int = 1095) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, (observed_date, value) in enumerate(rows):
        cutoff = observed_date - timedelta(days=lookback_days)
        prior = [prior_value for prior_date, prior_value in rows[:index]
                 if cutoff <= prior_date < observed_date]
        zscore = None
        if len(prior) >= 20:
            standard_deviation = float(np.std(prior, ddof=1))
            if standard_deviation > 0 and math.isfinite(standard_deviation):
                zscore = (value - float(np.mean(prior))) / standard_deviation
        output.append({"date": observed_date.isoformat(), "value": value, "zscore": zscore})
    return output


def _fit_curve_for_target(
    bonds: dict[str, dict[str, Any]],
    histories: dict[str, list[tuple[date, float]]],
    ticker: str,
    as_of: date,
    *,
    unit: str,
    maximum_age_days: int = 30,
) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    for isin, bond in bonds.items():
        maturity = bond.get("maturity_date")
        history = histories.get(isin, [])
        dates = [row[0] for row in history]
        index = bisect_right(dates, as_of) - 1
        if (bond.get("bond_ticker", "").upper() != ticker.upper()
                or maturity is None or maturity <= as_of or index < 0):
            continue
        observation_date, value = history[index]
        if (as_of - observation_date).days > maximum_age_days:
            continue
        points.append({
            "isin": isin,
            "maturity_years": (maturity - as_of).days / 365.25,
            "value": value,
            "observation_date": observation_date.isoformat(),
        })
    unique_maturities = {round(row["maturity_years"], 6) for row in points}
    if len(points) < 5 or len(unique_maturities) < 4:
        return {"unit": unit, "points": points, "residual": None,
                "message": f"Need five current ticker observations and four maturities; found {len(points)}."}
    maturities = np.asarray([row["maturity_years"] for row in points], dtype=float)
    values = np.asarray([row["value"] for row in points], dtype=float)
    try:
        tau, beta, rmse, _loo_rmse, _tau_min, _tau_max = _fit_curve(maturities, values)
    except ValueError as error:
        return {"unit": unit, "points": points, "residual": None, "message": str(error)}
    fitted = _basis(maturities, tau) @ beta
    for point, fit in zip(points, fitted):
        point["fitted"] = float(fit)
        point["residual"] = float(point["value"] - fit)
    return {"unit": unit, "points": points, "tau": tau, "rmse": rmse,
            "residual": None, "message": ""}


def analyze_bond_inspector(
    *,
    isin: str,
    as_of_date: date | str,
    bond_list_path: Path,
    data_source: str,
    data_path: Path | str | None,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Load observations through the selected date and calculate curve residuals."""
    normalized_isin = isin.strip().upper()
    as_of = _as_date(as_of_date)
    if not normalized_isin or as_of is None:
        raise ValueError("A valid ISIN and as-of date are required")
    bonds: dict[str, dict[str, Any]] = {}
    with Path(bond_list_path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            bond_isin = str(row.get("isin") or "").strip().upper()
            maturity = _as_date(row.get("maturity_date"))
            if not bond_isin or maturity is None:
                continue
            try:
                coupon = float(row.get("coupon_pct") or "")
            except (TypeError, ValueError):
                coupon = None
            bonds[bond_isin] = {
                **row,
                "isin": bond_isin,
                "maturity_date": maturity,
                "coupon_pct": coupon,
                "bond_ticker": str(row.get("bond_ticker") or "").strip().upper(),
                "equity_ticker": str(row.get("equity_ticker") or "").strip().upper(),
            }
    if normalized_isin not in bonds:
        raise ValueError(f"{normalized_isin} was not found in the reference bond list")
    target = bonds[normalized_isin]
    ticker = target["bond_ticker"]
    universe_isins = [key for key, bond in bonds.items()
                      if ticker and bond["bond_ticker"] == ticker and bond["maturity_date"] > as_of]
    if normalized_isin not in universe_isins:
        universe_isins.append(normalized_isin)
    history_start = as_of - timedelta(days=365 * 6)
    connection = duckdb.connect(str(database_path), read_only=True) if data_source == "raw_duckdb" else duckdb.connect(":memory:")
    try:
        placeholders = ",".join("?" for _ in universe_isins)
        if data_source == "raw_duckdb":
            query = (
                "SELECT isin, trade_date, last_sale_yield, price FROM observations "
                f"WHERE upper(isin) IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
                "ORDER BY upper(isin), trade_date"
            )
            parameters: list[Any] = [*universe_isins, history_start, as_of]
            yield_field, price_field = "last_sale_yield", "price"
        else:
            parquet = Path(data_path or "").expanduser().resolve()
            if not parquet.is_file():
                raise FileNotFoundError(f"TRACE observation dataset not found: {parquet}")
            parquet_sql = "'" + parquet.as_posix().replace("'", "''") + "'"
            query = (
                f"SELECT upper(isin), trade_date, clean_last_sale_yield, clean_price "
                f"FROM read_parquet({parquet_sql}) WHERE upper(isin) IN ({placeholders}) "
                "AND trade_date BETWEEN ? AND ? ORDER BY upper(isin), trade_date"
            )
            parameters = [*universe_isins, history_start, as_of]
            yield_field, price_field = "clean_last_sale_yield", "clean_price"
        records = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    yield_histories: dict[str, list[tuple[date, float]]] = {key: [] for key in universe_isins}
    price_histories: dict[str, list[tuple[date, float]]] = {key: [] for key in universe_isins}
    for row_isin, observed_date, raw_yield, raw_price in records:
        row_isin = str(row_isin).upper()
        yld = clean_trace_value("last_sale_yield", raw_yield) if data_source == "raw_duckdb" else raw_yield
        price = clean_trace_value("price", raw_price) if data_source == "raw_duckdb" else raw_price
        if yld is not None and math.isfinite(float(yld)):
            yield_histories[row_isin].append((observed_date, float(yld)))
        if price is not None and math.isfinite(float(price)) and float(price) > 0:
            price_histories[row_isin].append((observed_date, float(price)))

    spread_histories: dict[str, list[tuple[date, float]]] = {key: [] for key in universe_isins}
    spread_snapshot_id = None
    spread_connection = duckdb.connect(str(database_path), read_only=True)
    try:
        latest = spread_connection.execute(
            "SELECT snapshot_id FROM spread_snapshot_runs ORDER BY generated_at_utc DESC LIMIT 1"
        ).fetchone()
        if latest:
            spread_snapshot_id = str(latest[0])
            spreads = spread_connection.execute(
                f"""SELECT upper(isin), trade_date, g_spread_bps FROM bond_spread_observations
                    WHERE snapshot_id=? AND upper(isin) IN ({placeholders})
                      AND trade_date BETWEEN ? AND ? AND g_spread_bps IS NOT NULL
                      AND coalesce(yield_reversal_flagged, false)=false
                    ORDER BY upper(isin), trade_date""",
                [spread_snapshot_id, *universe_isins, history_start, as_of],
            ).fetchall()
            for row_isin, observed_date, spread_bps in spreads:
                spread_histories[str(row_isin).upper()].append((observed_date, float(spread_bps)))
    except duckdb.Error:
        pass
    finally:
        spread_connection.close()

    yield_curve = _fit_curve_for_target(bonds, yield_histories, ticker, as_of, unit="yield (%)") if ticker else {
        "points": [], "residual": None, "message": "No bond curve ticker is mapped for this bond."
    }
    spread_curve = _fit_curve_for_target(bonds, spread_histories, ticker, as_of, unit="G-spread (bp)") if ticker else {
        "points": [], "residual": None, "message": "No bond curve ticker is mapped for this bond."
    }
    for curve in (yield_curve, spread_curve):
        point = next((row for row in curve.get("points", []) if row["isin"] == normalized_isin), None)
        curve["residual"] = point.get("residual") if point else None
        curve["target_observation_date"] = point.get("observation_date") if point else None
        curve["target_observed_value"] = point.get("value") if point else None
    history = {
        "yield": _history_zscores(yield_histories.get(normalized_isin, [])),
        "spread": _history_zscores(spread_histories.get(normalized_isin, [])),
        "price": _history_zscores(price_histories.get(normalized_isin, [])),
    }
    return {
        "isin": normalized_isin,
        "ticker": ticker,
        "equity_ticker": target.get("equity_ticker", ""),
        "issuer": target.get("issuer_name", ""),
        "sector": target.get("sector", ""),
        "coupon_pct": target.get("coupon_pct"),
        "maturity_date": target["maturity_date"].isoformat(),
        "as_of_date": as_of.isoformat(),
        "spread_snapshot_id": spread_snapshot_id,
        "yield_curve": yield_curve,
        "spread_curve": spread_curve,
        "history": history,
        "zscore_lookback_days": 1095,
    }
