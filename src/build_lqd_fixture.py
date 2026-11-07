"""Export normalized LQD bonds as a compact, static CSV fixture.

Issuer equity tickers are supplied through a reviewed issuer-name mapping. A
bond ISIN identifies the bond itself and is not treated as an equity ticker.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

try:  # Project-root invocation: python -m src.build_lqd_fixture
    from .lqd_holdings import get_lqd_holdings
except ImportError:  # src-directory invocation: python -m build_lqd_fixture
    from lqd_holdings import get_lqd_holdings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "fixtures" / "lqd_bonds.csv"
DEFAULT_TICKER_MAP = ROOT / "data" / "issuer_ticker_map.csv"
OUTPUT_COLUMNS = [
    "source_as_of_date",
    "isin",
    "issuer_name",
    "equity_ticker",
    "ticker_mapping_status",
    "sector",
    "coupon_pct",
    "maturity_date",
    "yield_to_maturity_pct",
    "modified_duration_years",
    "market_value_usd",
    "portfolio_weight_pct",
]


def normalize_issuer_name(name: str) -> str:
    """Normalize common legal-name differences for curated ticker mapping."""
    value = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper())
    tokens = value.split()
    suffixes = {
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
        "LLC", "LTD", "LIMITED", "PLC", "LP", "LLP", "NV", "SA",
    }
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    if tokens and tokens[0] == "THE":
        tokens.pop(0)
    return " ".join(tokens)


def _numeric(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.replace(r"[$,%]", "", regex=True)
    cleaned = cleaned.str.replace(",", "", regex=False).replace({"-": pd.NA, "": pd.NA})
    return pd.to_numeric(cleaned, errors="coerce")


def _load_ticker_map(path: Path) -> dict[str, tuple[str, str, str]]:
    """Load reviewed and high-confidence exact entity mappings by issuer name."""
    if not path.exists():
        return {}
    mappings = pd.read_csv(path, dtype="string").fillna("")
    required = {"issuer_name", "equity_ticker", "mapping_source", "review_status"}
    missing = required.difference(mappings.columns)
    if missing:
        raise ValueError(f"Ticker map is missing required columns: {', '.join(sorted(missing))}")

    result: dict[str, tuple[str, str, str]] = {}
    for row in mappings.to_dict(orient="records"):
        key = normalize_issuer_name(row["issuer_name"])
        ticker = row["equity_ticker"].strip().upper()
        status = row["review_status"].strip().casefold()
        if not key or not ticker or status not in {"reviewed", "automated_exact_entity_match"}:
            continue
        if key in result:
            raise ValueError(f"Duplicate reviewed issuer mapping for {row['issuer_name']!r}.")
        result[key] = (ticker, row["mapping_source"].strip(), status)
    return result


def build_fixture(holdings: pd.DataFrame, ticker_map: dict[str, tuple[str, str]]) -> pd.DataFrame:
    """Normalize the iShares table into bond-level fixture rows."""
    required = {"Name", "ISIN", "Asset Class", "Sector", "Coupon (%)", "Maturity"}
    missing = required.difference(holdings.columns)
    if missing:
        raise ValueError(f"iShares holdings are missing required columns: {', '.join(sorted(missing))}")

    bonds = holdings.copy()
    bonds = bonds[bonds["Asset Class"].astype("string").str.strip().str.casefold() == "fixed income"]
    bonds = bonds[bonds["ISIN"].notna() & bonds["ISIN"].astype("string").str.strip().ne("")]

    result = pd.DataFrame(index=bonds.index)
    result["source_as_of_date"] = holdings.attrs.get("source_as_of_date") or ""
    result["isin"] = bonds["ISIN"].astype("string").str.strip().str.upper()
    result["issuer_name"] = bonds["Name"].astype("string").str.strip()
    normalized = result["issuer_name"].map(normalize_issuer_name)
    matched = normalized.map(ticker_map)
    result["equity_ticker"] = matched.map(lambda item: item[0] if isinstance(item, tuple) else "")
    result["ticker_mapping_status"] = matched.map(
        lambda item: "reviewed" if isinstance(item, tuple) and item[2] == "reviewed"
        else "exact_entity_match" if isinstance(item, tuple)
        else "unmapped"
    )
    result["sector"] = bonds["Sector"].astype("string").str.strip()
    result["coupon_pct"] = _numeric(bonds["Coupon (%)"])
    result["maturity_date"] = pd.to_datetime(bonds["Maturity"], errors="coerce").dt.strftime("%Y-%m-%d")
    result["yield_to_maturity_pct"] = _numeric(bonds["YTM (%)"]) if "YTM (%)" in bonds else pd.NA
    result["modified_duration_years"] = _numeric(bonds["Mod. Duration"]) if "Mod. Duration" in bonds else pd.NA
    result["market_value_usd"] = _numeric(bonds["Market Value"]) if "Market Value" in bonds else pd.NA
    result["portfolio_weight_pct"] = _numeric(bonds["Weight (%)"]) if "Weight (%)" in bonds else pd.NA

    result = result[OUTPUT_COLUMNS].sort_values("isin").reset_index(drop=True)
    if result["isin"].duplicated().any():
        duplicates = result.loc[result["isin"].duplicated(), "isin"].tolist()
        raise ValueError(f"Duplicate ISINs in LQD bond data: {duplicates[:5]}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV path to write")
    parser.add_argument("--ticker-map", type=Path, default=DEFAULT_TICKER_MAP, help="Reviewed issuer ticker map CSV")
    args = parser.parse_args()

    holdings = get_lqd_holdings(isin_only=False)
    fixture = build_fixture(holdings, _load_ticker_map(args.ticker_map))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fixture.to_csv(args.output, index=False, na_rep="")
    mapped = int(fixture["equity_ticker"].ne("").sum())
    print(f"Wrote {len(fixture):,} bond rows to {args.output}")
    print(f"Reviewed issuer tickers: {mapped:,}; unmapped: {len(fixture) - mapped:,}")


if __name__ == "__main__":
    main()
