# LQD Holdings

Fetches the latest iShares LQD ETF holdings into a pandas DataFrame. By
default, rows without an ISIN (such as some cash positions) are excluded.

## Setup

Install dependencies into the active Python environment:

```powershell
python -m pip install -r requirements.txt
```

## Use

From the project directory:

```powershell
python -c "from src.lqd_holdings import get_lqd_holdings; df = get_lqd_holdings(); print(df.head())"
```

To include rows that do not have an ISIN, call `get_lqd_holdings(isin_only=False)`.
The returned DataFrame includes the other columns supplied by iShares as well.

## Inspect

Open a searchable desktop table of the latest holdings:

```powershell
python -m src.lqd_inspector
```

Use the search box to filter across columns or **Refresh** to download the
latest iShares holdings again. Select **Built bond snapshot** to inspect the
locally generated `data/snapshots/lqd_bonds.csv` in the same viewer; use
**Refresh** there to reload it after building a new snapshot. Snapshot mode
also shows how many bond rows have a bond ticker, an equity ticker, and both.

## Build a Parallax snapshot

The normalized bond snapshot generator is documented in `data/README.md`.
It leaves issuer equity tickers blank unless a reviewed mapping is present in
`data/issuer_ticker_map.csv`.
