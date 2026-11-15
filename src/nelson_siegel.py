"""Cross-sectional, issuer-ticker Nelson-Siegel yield curve fits."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .trace_cleaning import TRACE_POLICY_VERSION, clean_trace_value


METHODOLOGY = "Nelson-Siegel cross-sectional yield curve"
TAU_GRID_YEARS = tuple(float(value) for value in np.geomspace(0.25, 15.0, 100))


def _basis(maturity_years: np.ndarray, tau: float) -> np.ndarray:
    scaled = maturity_years / tau
    decay = np.exp(-scaled)
    level = np.where(scaled == 0, 1.0, -np.expm1(-scaled) / scaled)
    curvature = level - decay
    return np.column_stack((np.ones_like(scaled), level, curvature))


def _fit_curve(maturities: np.ndarray, yields_pct: np.ndarray) -> tuple[float, np.ndarray, float]:
    best: tuple[float, float, np.ndarray, float] | None = None
    for tau in TAU_GRID_YEARS:
        design = _basis(maturities, tau)
        beta, *_ = np.linalg.lstsq(design, yields_pct, rcond=None)
        residual = yields_pct - design @ beta
        sse = float(residual @ residual)
        candidate = (sse, tau, beta, float(np.sqrt(np.mean(residual**2))))
        if best is None or candidate[0] < best[0]:
            best = candidate
    assert best is not None
    _, tau, beta, rmse = best
    return tau, beta, rmse


def fit_ticker_curves(
    *,
    as_of_date: date,
    bond_list_path: Path,
    database_path: Path,
    minimum_bonds: int = 5,
) -> list[dict[str, Any]]:
    """Fit one yield curve per equity issuer ticker using latest valid yields.

    All TRACE observations are filtered to ``trade_date <= as_of_date``. A
    ticker's cross-section must contain at least ``minimum_bonds`` bonds and
    four distinct maturities to fit the three NS loadings and decay parameter.
    """
    if minimum_bonds < 4:
        raise ValueError("minimum_bonds must be at least 4")
    if not bond_list_path.exists():
        raise FileNotFoundError(f"Bond list not found: {bond_list_path}")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    bonds: dict[str, dict[str, str]] = {}
    with bond_list_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"isin", "equity_ticker", "maturity_date"}.issubset(reader.fieldnames):
            raise ValueError("Bond list must contain isin, equity_ticker, and maturity_date columns")
        for raw in reader:
            isin = (raw.get("isin") or "").strip().upper()
            ticker = (raw.get("equity_ticker") or "").strip().upper()
            maturity_text = (raw.get("maturity_date") or "").strip()
            if not isin or not ticker or not maturity_text:
                continue
            try:
                maturity = date.fromisoformat(maturity_text)
            except ValueError:
                continue
            years = (maturity - as_of_date).days / 365.25
            if years > 0:
                bonds[isin] = {"ticker": ticker, "maturity_date": maturity_text, "maturity_years": str(years),
                               "issuer_name": (raw.get("issuer_name") or "").strip(), "sector": (raw.get("sector") or "").strip()}
    if not bonds:
        raise ValueError("No unmatured bonds with a mapped equity_ticker and maturity date")
    provenance = {
        "model_version": "nelson_siegel_grid_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bond_list_path": str(bond_list_path.resolve()),
        "trace_database_path": str(database_path.resolve()),
        "minimum_bonds": minimum_bonds,
        "minimum_distinct_maturities": 4,
        "yield_unit": "percent",
        "trace_cleaning_policy_version": TRACE_POLICY_VERSION,
        "tau_grid_min_years": min(TAU_GRID_YEARS),
        "tau_grid_max_years": max(TAU_GRID_YEARS),
        "tau_grid_points": len(TAU_GRID_YEARS),
    }

    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        observed = connection.execute(
            "SELECT isin, trade_date, last_sale_yield FROM observations WHERE trade_date <= ? AND isin IN ("
            + ",".join("?" for _ in bonds) + ") ORDER BY isin, trade_date",
            [as_of_date, *bonds],
        ).fetchall()
    finally:
        connection.close()

    latest: dict[str, tuple[date, float]] = {}
    for isin, trade_date, raw_yield in observed:
        ytm = clean_trace_value("last_sale_yield", raw_yield)
        if ytm is not None:
            latest[isin] = (trade_date, ytm)

    groups: dict[str, list[dict[str, Any]]] = {}
    for isin, (yield_date, ytm) in latest.items():
        bond = bonds[isin]
        groups.setdefault(bond["ticker"], []).append(
            {"isin": isin, **bond, "yield_date": yield_date.isoformat(), "observed_yield_pct": ytm}
        )

    results: list[dict[str, Any]] = []
    for ticker in sorted(set(bond["ticker"] for bond in bonds.values())):
        rows = groups.get(ticker, [])
        unique_maturities = {round(float(row["maturity_years"]), 6) for row in rows}
        reason = ""
        if len(rows) < minimum_bonds:
            reason = f"Only {len(rows)} valid yields; need {minimum_bonds}"
        elif len(unique_maturities) < 4:
            reason = f"Only {len(unique_maturities)} distinct maturities; need 4"
        if reason:
            results.append({**provenance, "ticker": ticker, "status": "insufficient data", "reason": reason,
                            "bond_count": len(rows), "as_of_date": as_of_date.isoformat(), "methodology": METHODOLOGY})
            continue

        maturities = np.array([float(row["maturity_years"]) for row in rows], dtype=float)
        yields_pct = np.array([float(row["observed_yield_pct"]) for row in rows], dtype=float)
        tau, beta, rmse = _fit_curve(maturities, yields_pct)
        fitted = _basis(maturities, tau) @ beta
        for row, fitted_yield in zip(rows, fitted):
            residual = float(row["observed_yield_pct"] - fitted_yield)
            results.append({
                **provenance,
                **row,
                "ticker": ticker,
                "status": "fitted",
                "reason": "",
                "as_of_date": as_of_date.isoformat(),
                "methodology": METHODOLOGY,
                "tau_years": float(tau),
                "beta_level": float(beta[0]),
                "beta_slope": float(beta[1]),
                "beta_curvature": float(beta[2]),
                "curve_rmse_pct": rmse,
                "fitted_yield_pct": float(fitted_yield),
                "yield_residual_pct": residual,
                "relative_value_signal": "cheap vs curve" if residual > 0 else "rich vs curve" if residual < 0 else "on curve",
            })
    return results
