"""Print a readable inspection of the latest iShares LQD holdings."""

from __future__ import annotations

import pandas as pd

try:  # Project-root invocation: python -m src.lqd_inspector
    from .lqd_holdings import get_lqd_holdings
except ImportError:  # src-directory invocation: python -m lqd_inspector
    from lqd_holdings import get_lqd_holdings


def _find_column(frame: pd.DataFrame, *names: str) -> str | None:
    """Find a column without depending on capitalization or surrounding spaces."""
    normalized = {str(column).strip().casefold(): column for column in frame.columns}
    for name in names:
        if name.casefold() in normalized:
            return normalized[name.casefold()]
    return None


def _print_counts(frame: pd.DataFrame, column: str, label: str) -> None:
    values = frame[column].fillna("(missing)").astype(str).str.strip()
    counts = values.value_counts(dropna=False)
    print(f"\n{label} ({column})")
    for value, count in counts.items():
        print(f"  {value}: {count:,}")


def print_inspection(holdings: pd.DataFrame) -> None:
    """Print the holdings and a compact set of descriptive summary statistics."""
    if holdings.empty:
        print("No LQD holdings were returned.")
        return

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 180)
    pd.set_option("display.max_colwidth", 42)

    print("LQD holdings")
    print("=" * 120)
    print(holdings.to_string(index=False, justify="left"))

    print("\nSummary")
    print("=" * 120)
    isin_column = _find_column(holdings, "ISIN")
    asset_class_column = _find_column(holdings, "Asset Class", "AssetClass")
    if asset_class_column:
        total_bonds = int(
            holdings[asset_class_column]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.casefold()
            .eq("fixed income")
            .sum()
        )
    else:
        total_bonds = int(holdings[isin_column].notna().sum()) if isin_column else len(holdings)
    print(f"Total bonds: {total_bonds:,}")
    print(f"Total rows:  {len(holdings):,}")

    for names, label in (
        (("Asset Class", "AssetClass"), "Holdings by asset class"),
        (("Type", "Security Type", "SecurityType"), "Holdings by type"),
        (("Sector",), "Holdings by sector"),
        (("Rating", "Rating (Moody's)", "Effective Rating"), "Holdings by rating"),
    ):
        column = _find_column(holdings, *names)
        if column:
            _print_counts(holdings, column, label)

    weight_column = _find_column(holdings, "Weight (%)", "Weight")
    if weight_column:
        weights = pd.to_numeric(
            holdings[weight_column].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False),
            errors="coerce",
        )
        if weights.notna().any():
            print(f"\nTotal portfolio weight: {weights.sum():,.2f}%")

    market_value_column = _find_column(holdings, "Market Value", "Market Value ($)")
    if market_value_column:
        values = pd.to_numeric(
            holdings[market_value_column].astype(str).str.replace(r"[$,]", "", regex=True),
            errors="coerce",
        )
        if values.notna().any():
            print(f"Total market value: ${values.sum():,.0f}")


def main() -> None:
    holdings = get_lqd_holdings()
    print_inspection(holdings)


if __name__ == "__main__":
    main()
