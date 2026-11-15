"""Pairwise covariance and correlation for archived bond portfolios."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np
import pandas as pd

from .trace_cleaning import clean_trace_value
from .trace_history import DATABASE_PATH


ProgressCallback = Callable[[int, int], None]


def _maturity_bucket(maturity_date: Any, as_of_date: date) -> str:
    if isinstance(maturity_date, str):
        try:
            maturity_date = date.fromisoformat(maturity_date[:10])
        except ValueError:
            maturity_date = None
    if not isinstance(maturity_date, date):
        return "Unknown"
    years = max(0.0, (maturity_date - as_of_date).days / 365.25)
    if years < 3.0:
        return "0-3 years"
    if years < 7.0:
        return "3-7 years"
    return "7+ years"


def calculate_portfolio_covariance(
    archive: dict[str, Any],
    *,
    as_of_date: date,
    lookback_days: int = 365,
    minimum_pair_observations: int = 20,
    max_price_age_days: int = 7,
    database_path: Path = DATABASE_PATH,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Calculate pairwise daily clean-price-return covariance for an archive.

    Prices are marked forward from the latest valid TRACE observation for up to
    ``max_price_age_days``. Daily simple returns use a shared weekday calendar;
    each covariance is calculated on dates where both bond returns are valid.
    Covariance is reported in squared basis points, correlation is unitless.
    """
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least two")
    if minimum_pair_observations < 2:
        raise ValueError("minimum_pair_observations must be at least two")
    if max_price_age_days < 0:
        raise ValueError("max_price_age_days must be non-negative")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    positions = archive.get("positions", [])
    unique_positions: list[dict[str, Any]] = []
    seen_isins: set[str] = set()
    for row in positions:
        isin = str(row.get("isin") or "").strip().upper()
        if not isin or isin in seen_isins:
            continue
        seen_isins.add(isin)
        unique_positions.append({**row, "isin": isin})
    if len(unique_positions) < 2:
        raise ValueError("The archived portfolio needs at least two unique bonds")

    window_start = as_of_date - timedelta(days=lookback_days)
    query_start = window_start - timedelta(days=max_price_age_days + 7)
    isins = [row["isin"] for row in unique_positions]
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT isin, trade_date, price FROM observations "
            "WHERE trade_date BETWEEN ? AND ? AND isin IN ("
            + ",".join("?" for _ in isins)
            + ") ORDER BY isin, trade_date",
            [query_start, as_of_date, *isins],
        ).fetchall()
    finally:
        connection.close()

    prices_by_isin: dict[str, list[tuple[date, float]]] = {isin: [] for isin in isins}
    for isin, observed_date, raw_price in rows:
        price = clean_trace_value("price", raw_price)
        if price is not None:
            prices_by_isin[isin].append((observed_date, price))

    mark_start = window_start - timedelta(days=max_price_age_days + 7)
    mark_dates = [stamp.date() for stamp in pd.bdate_range(mark_start, as_of_date)]
    window_dates = [mark_date for mark_date in mark_dates if mark_date >= window_start]
    returns_by_isin: dict[str, list[float | None]] = {}
    for index, position in enumerate(unique_positions, start=1):
        isin = position["isin"]
        observations = prices_by_isin[isin]
        observation_dates = [observed_date for observed_date, _price in observations]
        marked_prices: list[float | None] = []
        for mark_date in mark_dates:
            observation_index = bisect_right(observation_dates, mark_date) - 1
            if observation_index < 0:
                marked_prices.append(None)
                continue
            observed_date, price = observations[observation_index]
            marked_prices.append(price if (mark_date - observed_date).days <= max_price_age_days else None)
        daily_returns: list[float | None] = []
        for prior, current in zip(marked_prices, marked_prices[1:]):
            daily_returns.append(
                current / prior - 1.0
                if prior is not None and current is not None and prior > 0 else None
            )
        # The first mark has no one-day return; align the remaining points to
        # their ending weekday and retain only the requested lookback window.
        aligned_returns = {
            mark_dates[offset + 1]: value
            for offset, value in enumerate(daily_returns)
            if mark_dates[offset + 1] >= window_start
        }
        returns_by_isin[isin] = [aligned_returns.get(mark_date) for mark_date in window_dates]
        if on_progress is not None:
            on_progress(index, len(unique_positions))

    count = len(unique_positions)
    covariance = np.full((count, count), np.nan, dtype=float)
    correlation = np.full((count, count), np.nan, dtype=float)
    observation_counts = np.zeros((count, count), dtype=int)
    for left in range(count):
        left_values = returns_by_isin[unique_positions[left]["isin"]]
        for right in range(left, count):
            right_values = returns_by_isin[unique_positions[right]["isin"]]
            paired = [
                (a, b) for a, b in zip(left_values, right_values)
                if a is not None and b is not None
            ]
            n = len(paired)
            observation_counts[left, right] = observation_counts[right, left] = n
            if n < minimum_pair_observations:
                continue
            x = np.asarray([pair[0] for pair in paired], dtype=float)
            y = np.asarray([pair[1] for pair in paired], dtype=float)
            cov = float(np.cov(x, y, ddof=1)[0, 1]) * 100_000_000.0
            covariance[left, right] = covariance[right, left] = cov
            std_x, std_y = float(np.std(x, ddof=1)), float(np.std(y, ddof=1))
            if std_x > 0 and std_y > 0:
                corr = float(np.corrcoef(x, y)[0, 1])
                correlation[left, right] = correlation[right, left] = corr

    descriptors = []
    labels = []
    for row in unique_positions:
        maturity_bucket = _maturity_bucket(row.get("maturity_date"), as_of_date)
        issuer = str(row.get("issuer_name") or "Unknown issuer")
        sector = str(row.get("sector") or "Unclassified")
        label = str(row.get("bond_ticker") or row.get("isin"))
        labels.append(label)
        descriptors.append({
            "isin": row["isin"],
            "label": label,
            "issuer_name": issuer,
            "sector": sector,
            "maturity_bucket": maturity_bucket,
            "weight": row.get("weight"),
        })
    duplicates: dict[str, int] = {}
    for label in labels:
        duplicates[label] = duplicates.get(label, 0) + 1
    for descriptor in descriptors:
        if duplicates[descriptor["label"]] > 1:
            descriptor["label"] = f"{descriptor['label']} ({descriptor['isin'][-4:]})"

    pair_counts = [int(value) for value in observation_counts[np.triu_indices(count, k=0)]]
    eligible_counts = [value for value in pair_counts if value >= minimum_pair_observations]
    return {
        "archive_id": archive.get("archive_id", ""),
        "as_of_date": as_of_date.isoformat(),
        "window_start_date": window_start.isoformat(),
        "window_end_date": as_of_date.isoformat(),
        "lookback_days": lookback_days,
        "minimum_pair_observations": minimum_pair_observations,
        "max_price_age_days": max_price_age_days,
        "return_definition": "daily simple clean-price return from forward-carried weekday marks; coupon/carry excluded",
        "marking_definition": "latest valid TRACE price carried forward for up to max_price_age_days; older marks are missing",
        "partition_average_definition": "mean of available bond-pair estimates for each partition block; diagonal blocks include each bond's variance or self-correlation",
        "series_count": count,
        "weekday_observations": len(window_dates),
        "eligible_pair_count": len(eligible_counts),
        "total_pair_count": len(pair_counts),
        "minimum_eligible_pair_observations": min(eligible_counts) if eligible_counts else None,
        "median_eligible_pair_observations": float(np.median(eligible_counts)) if eligible_counts else None,
        "labels": [row["label"] for row in descriptors],
        "positions": descriptors,
        "covariance_bps_squared": [
            [float(value) if np.isfinite(value) else None for value in row] for row in covariance
        ],
        "correlation": [
            [float(value) if np.isfinite(value) else None for value in row] for row in correlation
        ],
        "observation_counts": observation_counts.tolist(),
    }
