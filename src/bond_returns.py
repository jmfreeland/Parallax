"""Historical holding-period total return for fixed-rate corporate bonds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

import QuantLib as ql
import duckdb

from .trace_cleaning import clean_trace_value
from .trace_history import DATABASE_PATH


@dataclass(frozen=True)
class BondTotalReturn:
    """Holding-period return and the observations/cash flows behind it."""

    isin: str
    requested_start_date: date
    requested_end_date: date
    start_price_date: date
    end_price_date: date | None
    effective_end_date: date
    start_clean_price_per_100: float
    end_clean_price_per_100: float | None
    start_accrued_interest_per_100: float
    end_accrued_interest_per_100: float
    start_dirty_price_per_100: float
    end_dirty_value_per_100: float
    cashflows_received_per_100: float
    coupon_cashflows_received_per_100: float
    principal_repayment_per_100: float
    total_return: float
    total_return_pct: float
    start_price_age_days: int
    end_price_age_days: int | None
    coupon_frequency: int
    coupon_schedule_assumption: str = "semiannual schedule rolled back from maturity"
    coupon_reinvestment_assumption: str = "none; interim cash flows earn zero"


def _ql_date(value: date) -> ql.Date:
    return ql.Date(value.day, value.month, value.year)


def _python_date(value: ql.Date) -> date:
    return date(value.year(), value.month(), value.dayOfMonth())


def _bond_for_window(
    *,
    start_date: date,
    maturity_date: date,
    coupon_pct: float,
    coupon_frequency: int,
) -> ql.FixedRateBond:
    coupon_months = 12 // coupon_frequency
    maturity = _ql_date(maturity_date)
    start = maturity
    period = ql.Period(coupon_months, ql.Months)
    while start >= _ql_date(start_date):
        start -= period
    schedule = ql.Schedule(
        start,
        maturity,
        period,
        ql.UnitedStates(ql.UnitedStates.GovernmentBond),
        ql.Unadjusted,
        ql.Unadjusted,
        ql.DateGeneration.Backward,
        False,
    )
    return ql.FixedRateBond(
        0,
        100.0,
        schedule,
        [coupon_pct / 100.0],
        ql.ActualActual(ql.ActualActual.Bond),
        ql.Unadjusted,
        100.0,
        start,
    )


def calculate_modified_duration_from_yield(
    *,
    as_of_date: date,
    maturity_date: date,
    coupon_pct: float,
    yield_pct: float,
    coupon_frequency: int = 2,
) -> float:
    """Calculate point-in-time modified duration from the bond's observed yield."""
    if maturity_date <= as_of_date:
        raise ValueError("maturity_date must be after as_of_date")
    if not 0 <= coupon_pct < 100:
        raise ValueError("coupon_pct must be between 0 and 100")
    if not -100 < yield_pct < 1000:
        raise ValueError("yield_pct is outside the supported range")
    if coupon_frequency not in {1, 2, 4, 12}:
        raise ValueError("coupon_frequency must be 1, 2, 4, or 12")
    bond = _bond_for_window(
        start_date=as_of_date,
        maturity_date=maturity_date,
        coupon_pct=coupon_pct,
        coupon_frequency=coupon_frequency,
    )
    settings = ql.Settings.instance()
    previous_evaluation_date = settings.evaluationDate
    try:
        settings.evaluationDate = _ql_date(as_of_date)
        return float(ql.BondFunctions.duration(
            bond,
            yield_pct / 100.0,
            ql.ActualActual(ql.ActualActual.Bond),
            ql.Compounded,
            coupon_frequency,
            ql.Duration.Modified,
        ))
    finally:
        settings.evaluationDate = previous_evaluation_date


def calculate_historical_bond_total_return(
    *,
    isin: str,
    start_date: date,
    end_date: date,
    coupon_pct: float,
    maturity_date: date,
    database_path: Path = DATABASE_PATH,
    coupon_frequency: int = 2,
    max_price_age_days: int | None = 5,
    price_observations: Sequence[tuple[date, float | None]] | None = None,
    value_on_requested_dates: bool = False,
    carry_forward_stale_prices: bool = False,
) -> BondTotalReturn:
    """Calculate a bond's total return over a requested historical window.

    The function selects the latest valid TRACE clean price on or before each
    requested boundary. Prices must be no older than ``max_price_age_days``
    unless that argument is ``None``. The reported effective dates are the
    actual selected observation dates. By default accrued interest and coupon/redemption
    cash flows are calculated with a synthetic schedule rolled back from
    maturity because the current security snapshot lacks actual coupon dates.
    Valuation uses each observation date with zero settlement-day lag.

    When ``value_on_requested_dates`` is true, the selected clean prices are
    marked on the requested dates, accrued interest is measured on those dates,
    and cash flows are accumulated through the requested end (or maturity).

    If the bond matures during the requested window, its coupon and redemption
    cash flows are treated as the terminal proceeds and held as cash at zero
    return through the requested end date. This calculation excludes fees,
    taxes, defaults, and coupon reinvestment.
    """
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")
    if maturity_date <= start_date:
        raise ValueError("maturity_date must be after start_date")
    if not 0 <= coupon_pct < 100:
        raise ValueError("coupon_pct must be between 0 and 100")
    if coupon_frequency not in {1, 2, 4, 12}:
        raise ValueError("coupon_frequency must be 1, 2, 4, or 12")
    if max_price_age_days is not None and max_price_age_days < 0:
        raise ValueError("max_price_age_days must be non-negative or None")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    query_start = (
        start_date - timedelta(days=max_price_age_days)
        if max_price_age_days is not None
        else date.min
    )
    query_end = min(end_date, maturity_date)
    if price_observations is None:
        connection = duckdb.connect(str(database_path), read_only=True)
        try:
            rows = connection.execute(
                """SELECT trade_date, price FROM observations
                   WHERE isin = ? AND trade_date BETWEEN ? AND ?
                   ORDER BY trade_date""",
                [isin.strip().upper(), query_start, query_end],
            ).fetchall()
        finally:
            connection.close()
    else:
        rows = [
            (trade_date, raw_price)
            for trade_date, raw_price in price_observations
            if query_start <= trade_date <= query_end
        ]

    prices = [
        (trade_date, clean_price)
        for trade_date, raw_price in rows
        if (clean_price := clean_trace_value("price", raw_price)) is not None
    ]
    start_points = [point for point in prices if point[0] <= start_date]
    if not start_points:
        raise ValueError(f"No valid TRACE price on or before {start_date} for {isin}")
    start_price_date, start_clean_price = start_points[-1]
    start_age = (start_date - start_price_date).days
    if max_price_age_days is not None and start_age > max_price_age_days:
        raise ValueError(
            f"Start price for {isin} is {start_age} days old; limit is {max_price_age_days}"
        )
    if start_clean_price <= 0:
        raise ValueError("start clean price must be positive")

    matures_in_window = maturity_date <= end_date
    if matures_in_window:
        effective_end_date = maturity_date
        end_price_date = None
        end_clean_price = None
        end_price_age = None
    else:
        end_points = [point for point in prices if point[0] <= end_date]
        if not end_points:
            raise ValueError(f"No valid TRACE price on or before {end_date} for {isin}")
        end_price_date, end_clean_price = end_points[-1]
        end_price_age = (end_date - end_price_date).days
        if (
            max_price_age_days is not None
            and end_price_age > max_price_age_days
            and not carry_forward_stale_prices
        ):
            raise ValueError(
                f"End price for {isin} is {end_price_age} days old; limit is {max_price_age_days}"
            )
        if not value_on_requested_dates and end_price_date <= start_price_date:
            raise ValueError("start and end boundaries resolve to the same price observation")
        effective_end_date = end_price_date

    valuation_start_date = start_date if value_on_requested_dates else start_price_date
    valuation_end_date = (
        min(end_date, maturity_date) if value_on_requested_dates else effective_end_date
    )
    if value_on_requested_dates:
        effective_end_date = valuation_end_date
    if valuation_end_date <= valuation_start_date:
        raise ValueError("effective return window is empty")

    settings = ql.Settings.instance()
    previous_evaluation_date = settings.evaluationDate
    try:
        bond = _bond_for_window(
            start_date=start_price_date,
            maturity_date=maturity_date,
            coupon_pct=coupon_pct,
            coupon_frequency=coupon_frequency,
        )
        settings.evaluationDate = _ql_date(valuation_start_date)
        start_accrued = float(bond.accruedAmount())
        start_dirty = start_clean_price + start_accrued

        if matures_in_window:
            end_accrued = 0.0
            end_dirty = 0.0
        else:
            settings.evaluationDate = _ql_date(valuation_end_date)
            end_accrued = float(bond.accruedAmount())
            end_dirty = float(end_clean_price) + end_accrued

        cashflows = sum(
            float(cashflow.amount())
            for cashflow in bond.cashflows()
            if valuation_start_date < _python_date(cashflow.date()) <= valuation_end_date
        )
        principal_repayment = 100.0 if matures_in_window else 0.0
        coupon_cashflows = cashflows - principal_repayment
        total_return = (end_dirty + cashflows - start_dirty) / start_dirty
    finally:
        settings.evaluationDate = previous_evaluation_date

    return BondTotalReturn(
        isin=isin.strip().upper(),
        requested_start_date=start_date,
        requested_end_date=end_date,
        start_price_date=start_price_date,
        end_price_date=end_price_date,
        effective_end_date=effective_end_date,
        start_clean_price_per_100=float(start_clean_price),
        end_clean_price_per_100=float(end_clean_price) if end_clean_price is not None else None,
        start_accrued_interest_per_100=start_accrued,
        end_accrued_interest_per_100=end_accrued,
        start_dirty_price_per_100=start_dirty,
        end_dirty_value_per_100=end_dirty,
        cashflows_received_per_100=cashflows,
        coupon_cashflows_received_per_100=coupon_cashflows,
        principal_repayment_per_100=principal_repayment,
        total_return=total_return,
        total_return_pct=total_return * 100.0,
        start_price_age_days=start_age,
        end_price_age_days=end_price_age,
        coupon_frequency=coupon_frequency,
    )
