"""Cross-sectional bond-curve-ticker Nelson-Siegel yield curve fits."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .trace_cleaning import TRACE_POLICY_VERSION, clean_trace_value


METHODOLOGY = "Nelson-Siegel cross-sectional yield curve"
TAU_GRID_POINTS = 100
TAU_MIN_FLOOR_YEARS = 0.5
TAU_MAX_CAP_YEARS = 15.0


def _basis(maturity_years: np.ndarray, tau: float) -> np.ndarray:
    scaled = maturity_years / tau
    decay = np.exp(-scaled)
    level = np.where(scaled == 0, 1.0, -np.expm1(-scaled) / scaled)
    curvature = level - decay
    return np.column_stack((np.ones_like(scaled), level, curvature))


def _fit_curve(
    maturities: np.ndarray, yields_pct: np.ndarray
) -> tuple[float, np.ndarray, float, float, float, float]:
    tau_min = max(TAU_MIN_FLOOR_YEARS, float(np.min(maturities)))
    tau_max = max(tau_min, min(TAU_MAX_CAP_YEARS, float(np.max(maturities))))
    tau_grid = np.geomspace(tau_min, tau_max, TAU_GRID_POINTS)
    best: tuple[float, float] | None = None
    for tau in tau_grid:
        design = _basis(maturities, tau)
        loo_errors = []
        for held_out in range(len(maturities)):
            keep = np.arange(len(maturities)) != held_out
            training_design = design[keep]
            if np.linalg.matrix_rank(training_design) < 3:
                loo_errors = []
                break
            beta_loo, *_ = np.linalg.lstsq(training_design, yields_pct[keep], rcond=None)
            loo_errors.append(float(yields_pct[held_out] - design[held_out] @ beta_loo))
        if not loo_errors:
            continue
        loo_rmse = float(np.sqrt(np.mean(np.square(loo_errors))))
        if best is None or loo_rmse < best[0]:
            best = (loo_rmse, float(tau))
    if best is None:
        raise ValueError("Nelson-Siegel curve is underdetermined for leave-one-bond-out tau selection")
    loo_rmse, tau = best
    design = _basis(maturities, tau)
    beta, *_ = np.linalg.lstsq(design, yields_pct, rcond=None)
    residual = yields_pct - design @ beta
    rmse = float(np.sqrt(np.mean(residual**2)))
    return tau, beta, rmse, loo_rmse, tau_min, tau_max


def _fit_curve_with_outlier_filter(
    maturities: np.ndarray,
    values: np.ndarray,
    threshold_mad: float | None = None,
) -> tuple[float, np.ndarray, float, float, float, float, np.ndarray]:
    """Fit NS and optionally remove cross-sectional residual outliers by MAD."""
    initial = _fit_curve(maturities, values)
    included = np.ones(len(values), dtype=bool)
    if threshold_mad is None or len(values) < 5:
        return (*initial, included)
    tau, beta, *_ = initial
    residuals = values - _basis(maturities, tau) @ beta
    center = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - center)))
    robust_scale = 1.4826 * mad
    if not np.isfinite(robust_scale) or robust_scale <= 1e-10:
        return (*initial, included)
    included = np.abs(residuals - center) <= threshold_mad * robust_scale
    distinct = {round(float(value), 6) for value in maturities[included]}
    if int(included.sum()) < 4 or len(distinct) < 4:
        return (*initial, np.ones(len(values), dtype=bool))
    filtered = _fit_curve(maturities[included], values[included])
    return (*filtered, included)


def fit_ticker_curves(
    *,
    as_of_date: date,
    bond_list_path: Path,
    database_path: Path,
    minimum_bonds: int = 5,
) -> list[dict[str, Any]]:
    """Fit one yield curve per bond curve ticker using latest valid yields.

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
        if not reader.fieldnames or not {"isin", "bond_ticker", "maturity_date"}.issubset(reader.fieldnames):
            raise ValueError("Bond list must contain isin, bond_ticker, and maturity_date columns")
        for raw in reader:
            isin = (raw.get("isin") or "").strip().upper()
            ticker = (raw.get("bond_ticker") or "").strip().upper()
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
        raise ValueError("No unmatured bonds with a mapped bond_ticker and maturity date")
    provenance = {
        "model_version": "nelson_siegel_loo_tau_bond_ticker_v3",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bond_list_path": str(bond_list_path.resolve()),
        "trace_database_path": str(database_path.resolve()),
        "minimum_bonds": minimum_bonds,
        "minimum_distinct_maturities": 4,
        "yield_unit": "percent",
        "trace_cleaning_policy_version": TRACE_POLICY_VERSION,
        "tau_selection": "leave_one_bond_out_rmse",
        "tau_grid_points": TAU_GRID_POINTS,
        "tau_minimum_floor_years": TAU_MIN_FLOOR_YEARS,
        "tau_maximum_cap_years": TAU_MAX_CAP_YEARS,
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
        tau, beta, rmse, loo_rmse, tau_min, tau_max = _fit_curve(maturities, yields_pct)
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
                "tau_search_min_years": tau_min,
                "tau_search_max_years": tau_max,
                "beta_level": float(beta[0]),
                "beta_slope": float(beta[1]),
                "beta_curvature": float(beta[2]),
                "implied_zero_maturity_yield_pct": float(beta[0] + beta[1]),
                "curve_rmse_pct": rmse,
                "tau_selection_loo_rmse_pct": loo_rmse,
                "fitted_yield_pct": float(fitted_yield),
                "yield_residual_pct": residual,
                "relative_value_signal": "cheap vs curve" if residual > 0 else "rich vs curve" if residual < 0 else "on curve",
            })
    return results
