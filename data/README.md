# LQD static fixture workflow

`src/build_lqd_fixture.py` normalizes the current iShares LQD holdings to a
compact bond-level schema at `data/fixtures/lqd_bonds.csv`. Run it from the
project root with:

```powershell
uv run python -m src.build_lqd_fixture
```

The output contains only `Fixed Income` records with an ISIN. Coupon, yield,
duration, market value, and weight retain their source units, with percentages
stored as percentage points (for example, `5.25` means 5.25%). Maturity is
ISO-formatted. Source holdings date is included on each row.

Issuer equity tickers are not inferred from bond ISINs. Add mappings to
`issuer_ticker_map.csv` only after review; unreviewed or missing mappings stay
blank and are marked `unmapped`. The sample Home Depot mapping uses ticker `HD`
as shown in its [SEC filing](https://www.sec.gov/Archives/edgar/data/354950/000162828026038247/hd-20260503.htm).

The generated CSV is an internal static snapshot for the Parallax project.
Its source URL and `source_as_of_date` are preserved for traceability. Refresh
it deliberately when a newer test snapshot is wanted; ordinary test runs
should consume the committed snapshot without making a network request.
