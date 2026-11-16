# Treasury curves and spread snapshots

`src.treasury_curves` retrieves the official U.S. Treasury daily nominal par
curve, appends a retrieval vintage to DuckDB, and writes a CSV export plus a
manifest. Curve runs are recorded in `treasury_curve_runs`; individual date
and tenor nodes are stored in `treasury_curve_observations`. Repeated downloads
do not overwrite earlier vintages.

```powershell
uv run python -m src.treasury_curves
uv run python -m src.treasury_curves --start 2025-01-01 --end 2025-12-31
uv run python -m src.treasury_curves --import-csv data/treasury/nominal_par_curve.csv
```

With no dates supplied, the first download covers the stored TRACE observation
history through today. Later downloads begin ten calendar days before the
latest Treasury observation and run through today, allowing recent source
corrections to be captured as a new vintage. The launcher exposes this as
**Update Treasury curve**. The launcher opens a chart of the latest stored
curve with tenor nodes, observation date, and retrieval time. After downloading
a new vintage, the chart refreshes to display the latest curve.

TRACE can be updated separately with **Update TRACE to current**. That action
fetches from the latest stored TRACE trade date with a ten-day overlap. Each
run archives the exact holdings snapshot and records its SHA-256. TRACE
retrievals are append-only in `trace_observation_versions`; the legacy
`observations` table is maintained as a latest-value compatibility table for
older readers. A completed run export selects the latest observation version
available at that run's completion time and saves to a unique as-of directory.

**Preprocess TRACE history** creates an immutable clean output using the
selected run's archived holdings snapshot and the observation versions known
at its completion time. An incremental update therefore produces a cumulative
as-of dataset without redownloading the full TRACE history. Its diagnostics
record source run, cutoff time, snapshot hash, and cleaning policy version.

**Build G-spread snapshot** binds a TRACE run and a Treasury curve run. It
interpolates between the nearest published CMT par-yield nodes at each bond's
remaining maturity, then calculates
`(clean TRACE last-sale yield - interpolated Treasury par yield) * 100` basis
points. The append-only `spread_snapshot_runs` and
`bond_spread_observations` tables plus a Parquet file and manifest retain both
input vintages, hashes, interpolation policy, and output hash. The action
defaults to the latest completed TRACE run and latest Treasury curve vintage;
the command line also accepts explicit `--trace-run-id` and `--curve-run-id`.
The builder runs the versioned TRACE preprocessor and series cleaner for that
run. The G-spread window lets you set the minimum isolated yield move flagged
as a reversal (default 50 bp); flagged observations remain in the audit data
but are excluded from matched G-spreads. The snapshot manifest records the
threshold, cleaning run IDs, excluded count, and cleaned Parquet hash. Bond
The build window reports each historical year as it calculates and stores
spreads, then reports the Parquet export stage. Bond performance charting uses
the latest built snapshot by default: for
each bond, its latest G-spread known at the start of each interval is added to
the short-rate funding series. The G-spread's date and age are shown in the
time-series table, and the result summary records the snapshot ID. Disable the
G-spread checkbox to use only the short-rate series and any manually entered
additional spread.

Treasury curve rows are published only on Treasury observation dates. The
spread builder joins by exact date and leaves unavailable or invalid spreads
null; it does not forward-fill rates. Linear interpolation is performed on
Treasury's published par yields and is not a zero-coupon spot curve. A simple
G-spread does not adjust for coupon, optionality, liquidity, or market timing.

Treasury's official curve is estimated with a monotone-convex method; its
methodology changed on December 6, 2021. This project preserves the published
nodes and uses linear interpolation only to map a bond's maturity.

- [Daily Treasury Par Yield Curve Rates](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=yield)
- [Treasury Yield Curve Methodology](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology)
- [Treasury XML feed documentation](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed)

The original Treasury CSV from the initial curve build can be imported once
with `--import-csv` to seed the versioned DuckDB tables without another fetch.
