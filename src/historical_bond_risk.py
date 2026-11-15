"""Historical fixed-rate bond risk from dated yields and security terms.

TRACE does not provide the coupon schedule needed by a cash-flow risk
calculation. This module uses the common US corporate-bond convention of
semiannual coupons, schedules rolled back from maturity, and Actual/Actual
(Bond) accrual. Results are estimates until issue and coupon dates are sourced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

import QuantLib as ql


@dataclass(frozen=True)
class BondRisk:
    """Yield-based risk measures and the assumptions used to calculate them."""

    valuation_date: date
    yield_to_maturity_pct: float
    clean_price_per_100: float
    dirty_price_per_100: float
    modified_duration_years: float
    macaulay_duration_years: float
    convexity_years_squared: float
    coupon_frequency: int
    schedule_method: str = "semiannual schedule rolled back from maturity"


@dataclass(frozen=True)
class BondTerms:
    """Security terms needed to create a fixed-rate bond cash-flow schedule."""

    isin: str
    coupon_pct: float
    maturity_date: date
    coupon_frequency: int = 2


@dataclass(frozen=True)
class PortfolioPosition:
    """A bond position expressed as par amount in the portfolio currency."""

    isin: str
    par_amount: float


@dataclass(frozen=True)
class YieldObservation:
    """A dated yield observation in percentage points."""

    isin: str
    observation_date: date
    yield_to_maturity_pct: float


@dataclass(frozen=True)
class PositionRisk:
    isin: str
    par_amount: float
    yield_observation_date: date
    risk: BondRisk
    market_value: float


@dataclass(frozen=True)
class PortfolioRisk:
    as_of_date: date
    total_market_value: float
    modified_duration_years: float | None
    macaulay_duration_years: float | None
    convexity_years_squared: float | None
    par_coverage_pct: float
    positions: tuple[PositionRisk, ...]
    unpriced_isins: tuple[str, ...]


def _ql_date(value: date) -> ql.Date:
    return ql.Date(value.day, value.month, value.year)


def _synthetic_schedule_start(
    valuation_date: ql.Date,
    maturity_date: ql.Date,
    coupon_months: int,
) -> ql.Date:
    """Return a coupon-aligned schedule date before valuation."""
    start = maturity_date
    coupon_period = ql.Period(coupon_months, ql.Months)
    while start >= valuation_date:
        start -= coupon_period
    return start


def calculate_bond_risk(
    *,
    valuation_date: date,
    maturity_date: date,
    coupon_pct: float,
    yield_to_maturity_pct: float,
    coupon_frequency: int = 2,
    face_value: float = 100.0,
) -> BondRisk:
    """Calculate yield-to-maturity duration and convexity as of a historical date.

    Inputs use percentage points (for example, ``5.25`` means 5.25%). The
    historical yield should be a cleaned dated bond yield, not a current yield
    carried backwards. Dates are interpreted on the valuation date with zero
    settlement lag. The schedule is synthetic because issue/coupon dates are
    absent from the current snapshot.
    """
    if coupon_frequency not in {1, 2, 4, 12}:
        raise ValueError("coupon_frequency must be 1, 2, 4, or 12")
    if face_value <= 0:
        raise ValueError("face_value must be positive")
    if not 0 <= coupon_pct < 100:
        raise ValueError("coupon_pct must be between 0 and 100")
    if yield_to_maturity_pct <= -100:
        raise ValueError("yield_to_maturity_pct must be greater than -100")
    if maturity_date <= valuation_date:
        raise ValueError("maturity_date must be after valuation_date")

    settings = ql.Settings.instance()
    previous_evaluation_date = settings.evaluationDate
    settings.evaluationDate = _ql_date(valuation_date)
    valuation = _ql_date(valuation_date)
    maturity = _ql_date(maturity_date)
    coupon_months = 12 // coupon_frequency
    calendar = ql.UnitedStates(ql.UnitedStates.GovernmentBond)
    start = _synthetic_schedule_start(valuation, maturity, coupon_months)
    schedule = ql.Schedule(
        start,
        maturity,
        ql.Period(coupon_months, ql.Months),
        calendar,
        ql.Unadjusted,
        ql.Unadjusted,
        ql.DateGeneration.Backward,
        False,
    )
    bond = ql.FixedRateBond(
        0,
        face_value,
        schedule,
        [coupon_pct / 100.0],
        ql.ActualActual(ql.ActualActual.Bond),
        ql.Unadjusted,
        100.0,
        start,
    )
    rate = ql.InterestRate(
        yield_to_maturity_pct / 100.0,
        ql.ActualActual(ql.ActualActual.Bond),
        ql.Compounded,
        coupon_frequency,
    )
    try:
        return BondRisk(
            valuation_date=valuation_date,
            yield_to_maturity_pct=yield_to_maturity_pct,
            clean_price_per_100=ql.BondFunctions.cleanPrice(bond, rate),
            dirty_price_per_100=ql.BondFunctions.dirtyPrice(bond, rate),
            modified_duration_years=ql.BondFunctions.duration(
                bond, rate, ql.Duration.Modified
            ),
            macaulay_duration_years=ql.BondFunctions.duration(
                bond, rate, ql.Duration.Macaulay
            ),
            convexity_years_squared=ql.BondFunctions.convexity(bond, rate),
            coupon_frequency=coupon_frequency,
        )
    finally:
        settings.evaluationDate = previous_evaluation_date


class HistoricalPortfolio:
    """Calculate market-value-weighted bond risk from historical yield data.

    For each position, the latest yield observation on or before ``as_of_date``
    is selected, subject to ``max_yield_age_days``. A limit of ``None`` allows
    arbitrarily old observations; the default is five calendar days. Positions
    without terms or a sufficiently recent yield are listed as unpriced and
    excluded from the risk aggregation, so callers can inspect coverage.
    """

    def __init__(
        self,
        *,
        as_of_date: date,
        bonds: Sequence[BondTerms],
        positions: Sequence[PortfolioPosition],
        yield_history: Sequence[YieldObservation],
        max_yield_age_days: int | None = 5,
    ) -> None:
        if max_yield_age_days is not None and max_yield_age_days < 0:
            raise ValueError("max_yield_age_days must be non-negative or None")
        self.as_of_date = as_of_date
        self.bonds = {bond.isin: bond for bond in bonds}
        if len(self.bonds) != len(bonds):
            raise ValueError("bonds must have unique ISINs")
        self.positions = tuple(positions)
        self.yield_history = tuple(yield_history)
        self.max_yield_age_days = max_yield_age_days
        position_isins = [position.isin for position in self.positions]
        if len(position_isins) != len(set(position_isins)):
            raise ValueError("positions must have unique ISINs")
        if any(position.par_amount <= 0 for position in self.positions):
            raise ValueError("position par_amounts must be positive")

    def calculate_risk(self) -> PortfolioRisk:
        """Price covered positions and aggregate duration and convexity."""
        latest_by_isin: dict[str, YieldObservation] = {}
        for observation in self.yield_history:
            if observation.observation_date > self.as_of_date:
                continue
            previous = latest_by_isin.get(observation.isin)
            if previous is None or observation.observation_date > previous.observation_date:
                latest_by_isin[observation.isin] = observation

        priced: list[PositionRisk] = []
        unpriced: list[str] = []
        total_par = sum(position.par_amount for position in self.positions)
        for position in self.positions:
            terms = self.bonds.get(position.isin)
            observation = latest_by_isin.get(position.isin)
            if terms is None or observation is None:
                unpriced.append(position.isin)
                continue
            age_days = (self.as_of_date - observation.observation_date).days
            if self.max_yield_age_days is not None and age_days > self.max_yield_age_days:
                unpriced.append(position.isin)
                continue
            risk = calculate_bond_risk(
                valuation_date=self.as_of_date,
                maturity_date=terms.maturity_date,
                coupon_pct=terms.coupon_pct,
                yield_to_maturity_pct=observation.yield_to_maturity_pct,
                coupon_frequency=terms.coupon_frequency,
                face_value=100.0,
            )
            market_value = position.par_amount * risk.dirty_price_per_100 / 100.0
            priced.append(
                PositionRisk(
                    isin=position.isin,
                    par_amount=position.par_amount,
                    yield_observation_date=observation.observation_date,
                    risk=risk,
                    market_value=market_value,
                )
            )

        total_market_value = sum(item.market_value for item in priced)
        if total_market_value > 0:
            modified_duration = sum(
                item.market_value * item.risk.modified_duration_years for item in priced
            ) / total_market_value
            macaulay_duration = sum(
                item.market_value * item.risk.macaulay_duration_years for item in priced
            ) / total_market_value
            convexity = sum(
                item.market_value * item.risk.convexity_years_squared for item in priced
            ) / total_market_value
        else:
            modified_duration = macaulay_duration = convexity = None
        coverage = 100.0 * sum(item.par_amount for item in priced) / total_par if total_par else 0.0
        return PortfolioRisk(
            as_of_date=self.as_of_date,
            total_market_value=total_market_value,
            modified_duration_years=modified_duration,
            macaulay_duration_years=macaulay_duration,
            convexity_years_squared=convexity,
            par_coverage_pct=coverage,
            positions=tuple(priced),
            unpriced_isins=tuple(unpriced),
        )
