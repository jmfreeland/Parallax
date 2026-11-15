"""Trailing covariance estimates for prices, yields, spreads, and other series."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd


SeriesTransform = Literal[
    "level",
    "difference",
    "simple_return",
    "log_return",
    "difference_bps",
    "difference_bps_from_pct",
]
Shrinkage = (
    Literal["none", "diagonal", "identity"]
    | float
    | None
    | Callable[[pd.DataFrame], pd.DataFrame]
)


@dataclass(frozen=True)
class TrailingCovarianceResult:
    """Covariance matrix plus the window and transformations used."""

    covariance: pd.DataFrame
    as_of_date: pd.Timestamp
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    observations: int
    transformations: dict[str, str]
    shrinkage: str


def _apply_transform(series: pd.Series, transform: SeriesTransform | Callable[[pd.Series], pd.Series]) -> pd.Series:
    if callable(transform):
        transformed = transform(series.copy())
        if not isinstance(transformed, pd.Series):
            raise TypeError("a custom series transform must return a pandas Series")
        if not transformed.index.equals(series.index):
            raise ValueError("a custom series transform must preserve the input index")
        return pd.to_numeric(transformed, errors="raise")

    if transform == "level":
        return series
    if transform == "difference":
        return series.diff()
    if transform == "simple_return":
        return series.pct_change(fill_method=None)
    if transform == "log_return":
        positive = series.where(series > 0)
        return np.log(positive).diff()
    if transform == "difference_bps":
        # For input rates stored as decimals: 0.0001 equals one basis point.
        return series.diff() * 10_000.0
    if transform == "difference_bps_from_pct":
        # For input rates stored in percentage points: 0.01 equals one basis point.
        return series.diff() * 100.0
    raise ValueError(f"unsupported series transformation: {transform!r}")


def _shrink_covariance(
    covariance: pd.DataFrame,
    shrinkage: Shrinkage,
) -> tuple[pd.DataFrame, str]:
    values = covariance.to_numpy(dtype=float)
    diagonal = np.diag(np.diag(values))
    if shrinkage is None or shrinkage == "none":
        shrunk = values
        label = "none"
    elif isinstance(shrinkage, str):
        if shrinkage == "diagonal":
            shrunk = diagonal
            label = "diagonal (full)"
        elif shrinkage == "identity":
            target_variance = float(np.trace(values) / len(values))
            shrunk = np.eye(len(values)) * target_variance
            label = "identity (full)"
        else:
            raise ValueError("shrinkage must be 'none', 'diagonal', 'identity', a float, or callable")
    elif callable(shrinkage):
        result = shrinkage(covariance.copy())
        if not isinstance(result, pd.DataFrame):
            raise TypeError("a custom covariance shrinkage must return a pandas DataFrame")
        if not result.index.equals(covariance.index) or not result.columns.equals(covariance.columns):
            raise ValueError("custom shrinkage must preserve covariance labels and order")
        shrunk = result.to_numpy(dtype=float)
        label = getattr(shrinkage, "__name__", "custom")
    elif isinstance(shrinkage, (int, float)) and not isinstance(shrinkage, bool):
        alpha = float(shrinkage)
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("numeric shrinkage strength must be between 0 and 1")
        shrunk = (1.0 - alpha) * values + alpha * diagonal
        label = f"diagonal ({alpha:g})"
    else:
        raise TypeError("unsupported shrinkage specification")

    if not np.isfinite(shrunk).all():
        raise ValueError("shrunk covariance contains non-finite values")
    if not np.allclose(shrunk, shrunk.T, rtol=1e-10, atol=1e-12):
        raise ValueError("shrunk covariance must be symmetric")
    shrunk = (shrunk + shrunk.T) / 2.0
    eigenvalues = np.linalg.eigvalsh(shrunk)
    tolerance = 1e-10 * max(1.0, float(np.abs(eigenvalues).max()))
    if eigenvalues.min() < -tolerance:
        raise ValueError("shrunk covariance must be positive semidefinite")
    return pd.DataFrame(shrunk, index=covariance.index, columns=covariance.columns), label


def trailing_covariance(
    data: pd.DataFrame,
    *,
    as_of_date: date | datetime | str,
    lookback_observations: int = 252,
    min_observations: int = 60,
    transformations: SeriesTransform | Mapping[
        str, SeriesTransform | Callable[[pd.Series], pd.Series]
    ] | Callable[[pd.Series], pd.Series] = "difference",
    shrinkage: Shrinkage = "none",
) -> TrailingCovarianceResult:
    """Estimate trailing sample covariance with per-series transforms/shrinkage.

    Rows are observations and columns are series. The input must have a unique,
    date-like index and numeric columns. Data after ``as_of_date`` is ignored.
    Transformations run before the trailing observation window is selected, so
    return/difference transforms can use the prior point at the window edge.
    Complete rows are used to keep the estimated covariance matrix coherent.

    ``difference_bps`` expects decimal rates; ``difference_bps_from_pct``
    expects percentage-point rates. Prices commonly use ``simple_return`` or
    ``log_return``. A callable transformation receives one column as a Series.
    Numeric shrinkage is the diagonal-target strength in [0, 1]; a callable
    shrinkage receives and returns a labelled covariance DataFrame.
    """
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise ValueError("data must be a non-empty pandas DataFrame")
    if not isinstance(data.index, pd.DatetimeIndex):
        try:
            frame = data.copy()
            frame.index = pd.to_datetime(frame.index, errors="raise")
        except (TypeError, ValueError) as error:
            raise TypeError("data index must contain date-like values") from error
    else:
        frame = data.copy()
    if frame.index.has_duplicates:
        raise ValueError("data index must not contain duplicate timestamps")
    if not frame.columns.is_unique:
        raise ValueError("data columns must be unique")
    if not isinstance(lookback_observations, int) or lookback_observations < 2:
        raise ValueError("lookback_observations must be an integer of at least 2")
    if not isinstance(min_observations, int) or min_observations < 2:
        raise ValueError("min_observations must be an integer of at least 2")
    if min_observations > lookback_observations:
        raise ValueError("min_observations cannot exceed lookback_observations")

    cutoff = pd.Timestamp(as_of_date)
    if frame.index.tz is not None:
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(frame.index.tz)
        else:
            cutoff = cutoff.tz_convert(frame.index.tz)
    elif cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    frame = frame.loc[frame.index <= cutoff].sort_index()
    if frame.empty:
        raise ValueError("no observations are on or before as_of_date")

    frame = frame.apply(pd.to_numeric, errors="raise")
    if isinstance(transformations, Mapping):
        unknown = set(transformations).difference(frame.columns)
        if unknown:
            raise ValueError(f"transformations contain unknown columns: {sorted(unknown)}")
        transform_by_column = {
            column: transformations.get(column, "difference") for column in frame.columns
        }
    else:
        transform_by_column = {column: transformations for column in frame.columns}
    transformed = pd.DataFrame(
        {
            column: _apply_transform(frame[column], transform_by_column[column])
            for column in frame.columns
        },
        index=frame.index,
    ).replace([np.inf, -np.inf], np.nan)
    window = transformed.dropna(how="any").tail(lookback_observations)
    if len(window) < min_observations:
        raise ValueError(
            f"only {len(window)} complete transformed observations; "
            f"need at least {min_observations}"
        )
    sample_covariance = window.cov(ddof=1)
    covariance, shrinkage_label = _shrink_covariance(sample_covariance, shrinkage)
    return TrailingCovarianceResult(
        covariance=covariance,
        as_of_date=cutoff,
        window_start=window.index[0],
        window_end=window.index[-1],
        observations=len(window),
        transformations={
            column: getattr(transform_by_column[column], "__name__", str(transform_by_column[column]))
            for column in frame.columns
        },
        shrinkage=shrinkage_label,
    )
