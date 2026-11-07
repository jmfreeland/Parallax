"""Export a normalized static snapshot of LQD bonds.

Issuer equity tickers are supplied through a reviewed issuer-name mapping. A
bond ISIN identifies the bond itself and is not treated as an equity ticker.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

try:  # Project-root invocation: python -m src.build_lqd_snapshot
    from .lqd_holdings import get_lqd_holdings
except ImportError:  # src-directory invocation: python -m build_lqd_snapshot
    from lqd_holdings import get_lqd_holdings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "snapshots" / "lqd_bonds.csv"
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
    "maturity_month",
    "maturity_year",
    "yield_to_maturity_pct",
    "modified_duration_years",
    "issuer_lei",
    "bond_ticker",
    "equity_exchange",
    "equity_country",
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


def _load_ticker_map(path: Path) -> dict[str, tuple[str, str, str, str, str]]:
    """Load mappings by LEI, falling back to normalized issuer name."""
    if not path.exists():
        return {}
    mappings = pd.read_csv(path, dtype="string").fillna("")
    required = {"issuer_name", "equity_ticker", "mapping_source", "review_status"}
    missing = required.difference(mappings.columns)
    if missing:
        raise ValueError(f"Ticker map is missing required columns: {', '.join(sorted(missing))}")

    result: dict[str, tuple[str, str, str, str, str]] = {}
    for row in mappings.to_dict(orient="records"):
        lei = row.get("issuer_lei", "").strip().upper()
        key = f"LEI:{lei}" if lei else f"NAME:{normalize_issuer_name(row['issuer_name'])}"
        ticker = row["equity_ticker"].strip().upper()
        status = row["review_status"].strip().casefold()
        if not key or not ticker or status not in {"reviewed", "automated_exact_entity_match"}:
            continue
        if key in result:
            previous = result[key]
            if previous[0] != ticker:
                raise ValueError(f"Conflicting ticker mappings for the same issuer identity: {row['issuer_name']!r}.")
            if status == "reviewed" and previous[2] != "reviewed":
                result[key] = (
                    ticker,
                    row["mapping_source"].strip(),
                    status,
                    row.get("equity_exchange", "").strip(),
                    row.get("equity_country", "").strip(),
                )
            continue
        result[key] = (
            ticker,
            row["mapping_source"].strip(),
            status,
            row.get("equity_exchange", "").strip(),
            row.get("equity_country", "").strip(),
        )
    return result


def build_snapshot(
    holdings: pd.DataFrame,
    ticker_map: dict[str, tuple[str, str, str, str, str]],
    issuer_lei_by_name: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Normalize the iShares table into bond-level snapshot rows."""
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
    result["issuer_lei"] = result["issuer_name"].map(issuer_lei_by_name or {}).fillna("")
    result["bond_ticker"] = bonds["Ticker"].astype("string").str.strip() if "Ticker" in bonds else ""
    identity_keys = result.apply(
        lambda row: f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}",
        axis=1,
    )
    matched = identity_keys.map(ticker_map)
    result["equity_ticker"] = matched.map(lambda item: item[0] if isinstance(item, tuple) else "")
    result["ticker_mapping_status"] = matched.map(
        lambda item: "reviewed" if isinstance(item, tuple) and item[2] == "reviewed"
        else "exact_entity_match" if isinstance(item, tuple)
        else "unmapped"
    )
    result["equity_exchange"] = matched.map(lambda item: item[3] if isinstance(item, tuple) else "")
    result["equity_country"] = matched.map(lambda item: item[4] if isinstance(item, tuple) else "")
    result["sector"] = bonds["Sector"].astype("string").str.strip()
    result["coupon_pct"] = _numeric(bonds["Coupon (%)"])
    maturity_dates = pd.to_datetime(bonds["Maturity"], errors="coerce")
    result["maturity_date"] = maturity_dates.dt.strftime("%Y-%m-%d")
    result["maturity_month"] = maturity_dates.dt.month
    result["maturity_year"] = maturity_dates.dt.year
    result["yield_to_maturity_pct"] = _numeric(bonds["YTM (%)"]) if "YTM (%)" in bonds else pd.NA
    result["modified_duration_years"] = _numeric(bonds["Mod. Duration"]) if "Mod. Duration" in bonds else pd.NA

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
    candidates_path = ROOT / "data" / "issuer_ticker_candidates.csv"
    lei_by_name: dict[str, str] = {}
    if candidates_path.exists():
        candidates = pd.read_csv(candidates_path, dtype="string").fillna("")
        lei_by_name = dict(zip(candidates["issuer_name"], candidates["issuer_lei"]))
    snapshot = build_snapshot(holdings, _load_ticker_map(args.ticker_map), lei_by_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    snapshot.to_csv(args.output, index=False, na_rep="")
    mapped = int(snapshot["equity_ticker"].ne("").sum())
    print(f"Wrote {len(snapshot):,} bond rows to {args.output}")
    print(f"Ticker mappings: {mapped:,}; unmapped: {len(snapshot) - mapped:,}")


if __name__ == "__main__":
    main()
