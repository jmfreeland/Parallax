"""Horizon P&L, attribution, and characteristics for archived portfolios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
from pathlib import Path
import statistics
from typing import Any, Callable

import duckdb

from .bond_returns import calculate_historical_bond_total_return
from .historical_bond_risk import calculate_bond_risk
from .trace_history import DATABASE_PATH


@dataclass(frozen=True)
class PortfolioHorizonAnalysis:
    archive_id: str
    portfolio_as_of_date: date
    horizon_end_date: date
    starting_nav: float
    max_price_age_days: int | None
    summary: dict[str, Any]
    positions: tuple[dict[str, Any], ...]
    sector_attribution: tuple[dict[str, Any], ...]
    issuer_attribution: tuple[dict[str, Any], ...]
    maturity_attribution: tuple[dict[str, Any], ...]
    time_series: tuple[dict[str, Any], ...]


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maturity_bucket(maturity_date: date | None, as_of_date: date) -> tuple[str, float | None]:
    if maturity_date is None:
        return "Unknown", None
    years = max(0.0, (maturity_date - as_of_date).days / 365.25)
    if years < 3.0:
        return "0-3 years", years
    if years < 7.0:
        return "3-7 years", years
    return "7+ years", years


def _performance_statistics(
    time_series: list[dict[str, Any]], starting_nav: float
) -> dict[str, Any]:
    """Summarize daily NAV returns; annualization assumes 252 weekdays/year."""
    nav_points = [
        (row["date"], starting_nav + float(row["cumulative_pnl"]))
        for row in time_series
    ]
    daily_returns: list[tuple[date, float]] = []
    for (previous_date, previous_nav), (current_date, current_nav) in zip(
        nav_points, nav_points[1:]
    ):
        if previous_nav > 0:
            daily_returns.append((current_date, current_nav / previous_nav - 1.0))

    result: dict[str, Any] = {
        "performance_observation_count": len(daily_returns),
        "cumulative_return_pct": None,
        "annualized_return_pct": None,
        "annualized_volatility_pct": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "max_drawdown_pct": None,
        "max_drawdown_peak_date": None,
        "max_drawdown_trough_date": None,
        "max_drawdown_duration_days": None,
        "calmar_ratio": None,
        "positive_day_pct": None,
        "best_day_return_pct": None,
        "worst_day_return_pct": None,
        "historical_var_95_pct": None,
        "historical_cvar_95_pct": None,
    }
    if not nav_points or starting_nav <= 0:
        return result

    final_nav = nav_points[-1][1]
    cumulative_return = final_nav / starting_nav - 1.0
    result["cumulative_return_pct"] = 100.0 * cumulative_return
    if daily_returns:
        returns = [value for _, value in daily_returns]
        mean_daily = statistics.fmean(returns)
        annualized_arithmetic_return = mean_daily * 252.0
        if final_nav > 0:
            result["annualized_return_pct"] = (
                100.0 * ((final_nav / starting_nav) ** (252.0 / len(returns)) - 1.0)
            )
        if len(returns) > 1:
            annualized_volatility = statistics.stdev(returns) * math.sqrt(252.0)
            result["annualized_volatility_pct"] = 100.0 * annualized_volatility
            if annualized_volatility > 0:
                # No historical risk-free series is wired into the tool yet.
                result["sharpe_ratio"] = annualized_arithmetic_return / annualized_volatility
        downside_deviation = math.sqrt(
            sum(min(value, 0.0) ** 2 for value in returns) / len(returns)
        ) * math.sqrt(252.0)
        if downside_deviation > 0:
            result["sortino_ratio"] = annualized_arithmetic_return / downside_deviation
        result["positive_day_pct"] = 100.0 * sum(value > 0 for value in returns) / len(returns)
        result["best_day_return_pct"] = 100.0 * max(returns)
        result["worst_day_return_pct"] = 100.0 * min(returns)
        sorted_returns = sorted(returns)
        tail_count = max(1, math.ceil(0.05 * len(sorted_returns)))
        var_index = min(len(sorted_returns) - 1, math.floor(0.05 * (len(sorted_returns) - 1)))
        result["historical_var_95_pct"] = -100.0 * sorted_returns[var_index]
        result["historical_cvar_95_pct"] = -100.0 * statistics.fmean(sorted_returns[:tail_count])

    peak_nav = nav_points[0][1]
    peak_date = nav_points[0][0]
    worst_drawdown = 0.0
    worst_peak_date = peak_date
    worst_trough_date = peak_date
    worst_duration = 0
    for mark_date, nav in nav_points:
        if nav > peak_nav:
            peak_nav, peak_date = nav, mark_date
        drawdown = nav / peak_nav - 1.0 if peak_nav > 0 else 0.0
        if drawdown < worst_drawdown:
            worst_drawdown = drawdown
            worst_peak_date = peak_date
            worst_trough_date = mark_date
            worst_duration = (mark_date - peak_date).days
    result["max_drawdown_pct"] = 100.0 * worst_drawdown
    result["max_drawdown_peak_date"] = worst_peak_date
    result["max_drawdown_trough_date"] = worst_trough_date
    result["max_drawdown_duration_days"] = worst_duration
    if worst_drawdown < 0:
        annualized_return_pct = result["annualized_return_pct"]
        if annualized_return_pct is not None:
            result["calmar_ratio"] = (annualized_return_pct / 100.0) / abs(worst_drawdown)
    return result


def _aggregate_groups(
    positions: list[dict[str, Any]],
    group_key: str,
    total_weight: float,
    starting_nav: float,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for position in positions:
        name = str(position.get(group_key) or "Unclassified")
        group = grouped.setdefault(
            name,
            {
                "name": name,
                "positions": 0,
                "exposure_weight": 0.0,
                "covered_weight": 0.0,
                "price_move_pnl": 0.0,
                "carry_pnl": 0.0,
                "principal_pnl": 0.0,
                "pnl": 0.0,
            },
        )
        weight = float(position["weight"])
        group["positions"] += 1
        group["exposure_weight"] += weight
        if position["status"] != "priced":
            continue
        group["covered_weight"] += weight
        group["price_move_pnl"] += float(position["price_move_pnl"])
        group["carry_pnl"] += float(position["carry_pnl"])
        group["principal_pnl"] += float(position["principal_pnl"])
        group["pnl"] += float(position["pnl"])

    result = []
    for item in grouped.values():
        exposure = item["exposure_weight"]
        item["exposure_pct"] = 100.0 * exposure / total_weight if total_weight else 0.0
        item["covered_weight_pct"] = 100.0 * item["covered_weight"] / total_weight if total_weight else 0.0
        item["coverage_pct"] = 100.0 * item["covered_weight"] / exposure if exposure else 0.0
        item["price_move_pnl_pct_nav"] = 100.0 * item["price_move_pnl"] / starting_nav
        item["carry_pnl_pct_nav"] = 100.0 * item["carry_pnl"] / starting_nav
        item["principal_pnl_pct_nav"] = 100.0 * item["principal_pnl"] / starting_nav
        item["pnl_pct_nav"] = 100.0 * item["pnl"] / starting_nav
        result.append(item)
    return tuple(sorted(result, key=lambda item: item["pnl"], reverse=True))


def analyze_portfolio_horizon(
    archive: dict[str, Any],
    *,
    horizon_end_date: date,
    starting_nav: float = 1_000_000.0,
    max_price_age_days: int | None = 30,
    database_path: Path = DATABASE_PATH,
    on_progress: Callable[[int, int], None] | None = None,
) -> PortfolioHorizonAnalysis:
    """Calculate covered-weight P&L and return attribution for an archive.

    Returns are measured from the portfolio's archived as-of date to the
    requested horizon. Original weights are kept: unavailable bond returns are
    not silently dropped and redistributed. Their weight is reported as
    uncovered, while a normalized return for only the priced subset is shown
    separately.
    """
    as_of_date = _as_date(archive.get("as_of_date"))
    if as_of_date is None:
        raise ValueError("portfolio archive has no valid as_of_date")
    if horizon_end_date <= as_of_date:
        raise ValueError("horizon_end_date must be after the portfolio as-of date")
    if starting_nav <= 0:
        raise ValueError("starting_nav must be positive")
    if max_price_age_days is not None and max_price_age_days < 0:
        raise ValueError("max_price_age_days must be non-negative or None")
    archived_positions = archive.get("positions") or []
    if not archived_positions:
        raise ValueError("portfolio archive contains no positions")

    if not Path(database_path).exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    isins = list(
        dict.fromkeys(
            isin
            for row in archived_positions
            if (isin := str(row.get("isin") or "").strip().upper())
        )
    )
    if not isins:
        raise ValueError("portfolio archive contains no valid ISINs")
    query_start = (
        as_of_date - timedelta(days=max_price_age_days)
        if max_price_age_days is not None
        else date.min
    )
    placeholders = ", ".join("?" for _ in isins)
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        raw_prices = connection.execute(
            f"""SELECT isin, trade_date, price FROM observations
                WHERE isin IN ({placeholders}) AND trade_date BETWEEN ? AND ?
                ORDER BY isin, trade_date""",
            [*isins, query_start, horizon_end_date],
        ).fetchall()
    finally:
        connection.close()
    prices_by_isin: dict[str, list[tuple[date, float | None]]] = {}
    for isin, trade_date, raw_price in raw_prices:
        prices_by_isin.setdefault(isin, []).append((trade_date, raw_price))

    positions: list[dict[str, Any]] = []
    for index, row in enumerate(archived_positions, start=1):
        isin = str(row.get("isin") or "").strip().upper()
        weight = _number(row.get("weight"))
        maturity_date = _as_date(row.get("maturity_date"))
        coupon_pct = _number(row.get("coupon_pct"))
        sector = str(row.get("sector") or "Unclassified")
        issuer = str(row.get("issuer_name") or "Unknown issuer")
        bucket, maturity_years = _maturity_bucket(maturity_date, as_of_date)
        if not isin or weight is None or weight < 0:
            raise ValueError("each archived position needs an ISIN and non-negative weight")
        position: dict[str, Any] = {
            "isin": isin,
            "issuer_name": issuer,
            "sector": sector,
            "maturity_date": maturity_date,
            "maturity_bucket": bucket,
            "maturity_years": maturity_years,
            "coupon_pct": coupon_pct,
            "weight": weight,
            "status": "unpriced",
            "error": "",
        }
        if coupon_pct is None or maturity_date is None:
            position["error"] = "Missing coupon or maturity terms in archived position"
            positions.append(position)
            if on_progress is not None:
                on_progress(index, len(archived_positions))
            continue
        try:
            result = calculate_historical_bond_total_return(
                isin=isin,
                start_date=as_of_date,
                end_date=horizon_end_date,
                coupon_pct=coupon_pct,
                maturity_date=maturity_date,
                database_path=database_path,
                max_price_age_days=max_price_age_days,
                price_observations=prices_by_isin.get(isin, []),
                value_on_requested_dates=True,
                carry_forward_stale_prices=True,
            )
        except Exception as error:
            position["error"] = str(error)
            positions.append(position)
            if on_progress is not None:
                on_progress(index, len(archived_positions))
            continue

        end_clean_price = float(result.end_clean_price_per_100 or 0.0)
        price_move_per_100 = end_clean_price - result.start_clean_price_per_100
        carry_per_100 = (
            result.coupon_cashflows_received_per_100
            + result.end_accrued_interest_per_100
            - result.start_accrued_interest_per_100
        )
        principal_per_100 = result.principal_repayment_per_100
        pnl_per_100 = price_move_per_100 + carry_per_100 + principal_per_100
        pnl = starting_nav * weight * result.total_return
        position.update(
            {
                "status": "priced",
                "error": "",
                "start_price_date": result.start_price_date,
                "end_price_date": result.end_price_date,
                "effective_end_date": result.effective_end_date,
                "start_price_age_days": result.start_price_age_days,
                "end_price_age_days": result.end_price_age_days,
                "start_dirty_price_per_100": result.start_dirty_price_per_100,
                "end_dirty_value_per_100": result.end_dirty_value_per_100,
                "price_move_per_100": price_move_per_100,
                "carry_per_100": carry_per_100,
                "principal_per_100": principal_per_100,
                "pnl_per_100": pnl_per_100,
                "bond_total_return_pct": result.total_return_pct,
                "price_move_pnl": starting_nav * weight * price_move_per_100 / result.start_dirty_price_per_100,
                "carry_pnl": starting_nav * weight * carry_per_100 / result.start_dirty_price_per_100,
                "principal_pnl": starting_nav * weight * principal_per_100 / result.start_dirty_price_per_100,
                "pnl": pnl,
            }
        )
        positions.append(position)
        if on_progress is not None:
            on_progress(index, len(archived_positions))

    total_weight = sum(float(position["weight"]) for position in positions)
    if total_weight <= 0:
        raise ValueError("archived position weights sum to zero")

    # Mark every weekday using the latest eligible TRACE price and accrued
    # interest/cash flows through that date.
    time_series_dates: list[date] = []
    point_date = as_of_date + timedelta(days=1)
    while point_date <= horizon_end_date:
        if point_date.weekday() < 5:
            time_series_dates.append(point_date)
        point_date += timedelta(days=1)
    if not time_series_dates or time_series_dates[-1] != horizon_end_date:
        time_series_dates.append(horizon_end_date)
    initial_covered_weight = sum(
        float(position["weight"]) for position in positions if position["status"] == "priced"
    )
    initial_stale_weight = sum(
        float(position["weight"])
        for position in positions
        if position["status"] == "priced"
        and max_price_age_days is not None
        and position.get("start_price_age_days") is not None
        and position["start_price_age_days"] > max_price_age_days
    )
    time_series: list[dict[str, Any]] = [
        {
            "date": as_of_date,
            "cumulative_pnl": 0.0,
            "return_contribution_pct_nav": 0.0,
            "covered_weight": initial_covered_weight,
            "coverage_pct": 100.0 * initial_covered_weight / total_weight,
            "stale_weight_pct": 100.0 * initial_stale_weight / total_weight,
        }
    ]
    for point_index, point_date in enumerate(time_series_dates, start=1):
        point_pnl = 0.0
        point_covered_weight = 0.0
        point_stale_weight = 0.0
        for position in positions:
            if position["coupon_pct"] is None or position["maturity_date"] is None:
                continue
            try:
                point_return = calculate_historical_bond_total_return(
                    isin=position["isin"],
                    start_date=as_of_date,
                    end_date=point_date,
                    coupon_pct=float(position["coupon_pct"]),
                    maturity_date=position["maturity_date"],
                    database_path=database_path,
                    max_price_age_days=max_price_age_days,
                    price_observations=prices_by_isin.get(position["isin"], []),
                    value_on_requested_dates=True,
                    carry_forward_stale_prices=True,
                )
            except Exception:
                continue
            weight = float(position["weight"])
            point_pnl += starting_nav * weight * point_return.total_return
            point_covered_weight += weight
            if (
                max_price_age_days is not None
                and point_return.end_price_age_days is not None
                and point_return.end_price_age_days > max_price_age_days
            ):
                point_stale_weight += weight
        time_series.append(
            {
                "date": point_date,
                "cumulative_pnl": point_pnl,
                "return_contribution_pct_nav": 100.0 * point_pnl / starting_nav,
                "covered_weight": point_covered_weight,
                "coverage_pct": 100.0 * point_covered_weight / total_weight,
                "stale_weight_pct": 100.0 * point_stale_weight / total_weight,
            }
        )
        if on_progress is not None:
            on_progress(
                len(archived_positions) + point_index,
                len(archived_positions) + len(time_series_dates),
            )
    covered = [position for position in positions if position["status"] == "priced"]
    covered_weight = sum(float(position["weight"]) for position in covered)
    stale_mark_weight = sum(
        float(position["weight"])
        for position in covered
        if max_price_age_days is not None
        and position.get("end_price_age_days") is not None
        and position["end_price_age_days"] > max_price_age_days
    )
    pnl = sum(float(position.get("pnl", 0.0)) for position in covered)
    covered_return_pct_nav = 100.0 * pnl / starting_nav
    covered_subset_return_pct = (
        100.0 * pnl / (starting_nav * covered_weight) if covered_weight else None
    )

    sector_exposure: dict[str, float] = {}
    maturity_exposure: dict[str, float] = {}
    issuer_exposure: dict[str, float] = {}
    weighted_coupon = weighted_coupon_weight = 0.0
    weighted_maturity = weighted_maturity_weight = 0.0
    weighted_yield = weighted_yield_weight = 0.0
    weighted_duration = weighted_duration_weight = 0.0
    for position, archived in zip(positions, archived_positions):
        weight = float(position["weight"])
        sector_exposure[position["sector"]] = sector_exposure.get(position["sector"], 0.0) + weight
        maturity_exposure[position["maturity_bucket"]] = maturity_exposure.get(position["maturity_bucket"], 0.0) + weight
        issuer_exposure[position["issuer_name"]] = issuer_exposure.get(position["issuer_name"], 0.0) + weight
        if position["coupon_pct"] is not None:
            weighted_coupon += weight * float(position["coupon_pct"])
            weighted_coupon_weight += weight
        if position["maturity_years"] is not None:
            weighted_maturity += weight * float(position["maturity_years"])
            weighted_maturity_weight += weight
        ytm = _number(archived.get("latest_yield_pct"))
        if ytm is not None:
            weighted_yield += weight * ytm
            weighted_yield_weight += weight
            if position["coupon_pct"] is not None and position["maturity_date"] is not None:
                try:
                    risk = calculate_bond_risk(
                        valuation_date=as_of_date,
                        maturity_date=position["maturity_date"],
                        coupon_pct=position["coupon_pct"],
                        yield_to_maturity_pct=ytm,
                    )
                    weighted_duration += weight * risk.modified_duration_years
                    weighted_duration_weight += weight
                except Exception:
                    pass

    summary = {
        "position_count": len(positions),
        "priced_position_count": len(covered),
        "unpriced_position_count": len(positions) - len(covered),
        "total_weight": total_weight,
        "covered_weight": covered_weight,
        "stale_mark_weight": stale_mark_weight,
        "stale_mark_weight_pct": 100.0 * stale_mark_weight / total_weight,
        "uncovered_weight": max(0.0, total_weight - covered_weight),
        "coverage_pct": 100.0 * covered_weight / total_weight,
        "starting_nav": starting_nav,
        "covered_pnl": pnl,
        "covered_return_contribution_pct_nav": covered_return_pct_nav,
        "return_on_covered_positions_pct": covered_subset_return_pct,
        "weighted_average_coupon_pct": weighted_coupon / weighted_coupon_weight if weighted_coupon_weight else None,
        "coupon_weight_coverage_pct": 100.0 * weighted_coupon_weight / total_weight,
        "weighted_average_reported_yield_pct": weighted_yield / weighted_yield_weight if weighted_yield_weight else None,
        "yield_weight_coverage_pct": 100.0 * weighted_yield_weight / total_weight,
        "weighted_average_modified_duration_years": weighted_duration / weighted_duration_weight if weighted_duration_weight else None,
        "duration_weight_coverage_pct": 100.0 * weighted_duration_weight / total_weight,
        "weighted_average_maturity_years": weighted_maturity / weighted_maturity_weight if weighted_maturity_weight else None,
        "number_of_sectors": len(sector_exposure),
        "number_of_issuers": len(issuer_exposure),
        "largest_sector": max(sector_exposure.items(), key=lambda item: item[1])[0],
        "largest_sector_weight_pct": 100.0 * max(sector_exposure.values()) / total_weight,
        "largest_issuer": max(issuer_exposure.items(), key=lambda item: item[1])[0],
        "largest_issuer_weight_pct": 100.0 * max(issuer_exposure.values()) / total_weight,
    }
    summary.update(_performance_statistics(time_series, starting_nav))
    return PortfolioHorizonAnalysis(
        archive_id=str(archive.get("archive_id") or ""),
        portfolio_as_of_date=as_of_date,
        horizon_end_date=horizon_end_date,
        starting_nav=starting_nav,
        max_price_age_days=max_price_age_days,
        summary=summary,
        positions=tuple(positions),
        sector_attribution=_aggregate_groups(positions, "sector", total_weight, starting_nav),
        issuer_attribution=_aggregate_groups(positions, "issuer_name", total_weight, starting_nav),
        maturity_attribution=_aggregate_groups(positions, "maturity_bucket", total_weight, starting_nav),
        time_series=tuple(time_series),
    )
