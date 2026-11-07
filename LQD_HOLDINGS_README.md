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

Print the latest holdings in a readable table, followed by row counts and any
available asset class, security type, sector, rating, portfolio weight, and
market value summaries:

```powershell
python -m src.lqd_inspector
```

## Build a Parallax fixture

The normalized bond snapshot generator is documented in `data/README.md`.
It leaves issuer equity tickers blank unless a reviewed mapping is present in
`data/issuer_ticker_map.csv`.
