"""Download the latest LQD holdings from iShares into a pandas DataFrame."""

from __future__ import annotations

import csv
from io import StringIO

import pandas as pd
import requests


HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239566/"
    "ishares-iboxx-investment-grade-corporate-bond-etf/latest-holdings.csv"
)


def get_lqd_holdings(isin_only: bool = True) -> pd.DataFrame:
    """Fetch LQD holdings; by default, keep rows with an ISIN.

    The iShares CSV includes metadata lines before its table. The table header
    is located dynamically so parsing is not tied to a fixed row count.
    """
    response = requests.get(
        HOLDINGS_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LQD-holdings-fetcher/1.0)"},
        timeout=30,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    if response.text.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise ValueError(
            "iShares returned an HTML page instead of the holdings CSV. "
            "The download endpoint may have changed or access may be blocked."
        )
    records = csv.reader(StringIO(response.text))
    header = None
    rows: list[list[str]] = []
    source_as_of_date = None
    for record in records:
        normalized = [cell.strip().casefold() for cell in record]
        if len(record) >= 2 and normalized[0] == "fund holdings as of":
            source_as_of_date = record[1].strip()
        if header is None:
            if "isin" in normalized and "name" in normalized:
                header = [cell.strip() for cell in record]
            continue

        # iShares includes non-table metadata/footer records in some responses.
        # Ignore them, along with malformed rows, while retaining table-shaped rows.
        if len(record) == len(header) and any(cell.strip() for cell in record):
            rows.append(record)

    if header is None:
        raise ValueError("Could not find the holdings table header in iShares response.")
    if not rows:
        raise ValueError("Found the holdings table header, but no valid holding rows.")

    holdings = pd.DataFrame(rows, columns=header)
    holdings.attrs["source_as_of_date"] = source_as_of_date

    if isin_only:
        isin_column = next(
            (column for column in holdings.columns if column.strip().upper() == "ISIN"),
            None,
        )
        if isin_column is None:
            raise ValueError("The iShares holdings table did not contain an ISIN column.")
        holdings = holdings[holdings[isin_column].notna()]

    return holdings.reset_index(drop=True)


if __name__ == "__main__":
    df = get_lqd_holdings()
    print(f"Loaded {len(df):,} holdings with ISINs")
    print(df.head())
