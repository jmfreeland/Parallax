# Using Parallax

Parallax is a local research workbench for building an auditable daily TRACE history for a selected corporate-bond universe. It is not yet a portfolio optimizer, execution system, or production alpha model.

## Setup

Use Python 3.11 and install dependencies:

```bash
uv pip install -r requirements.txt
uv run python -m src.launcher
```

The launcher starts the current workflows: inspect LQD holdings, refresh the LQD snapshot, review issuer-to-equity mappings, and download or inspect FINRA TRACE history.

## LQD snapshot and mappings

The LQD holdings file is a convenient pilot population, not a security master or credit benchmark. Refresh it deliberately:

```bash
uv run python -m src.build_lqd_snapshot
```

This fetches the iShares holdings export, normalizes fixed-income instruments with an ISIN, applies reviewed issuer-to-equity mappings, and writes `data/snapshots/lqd_bonds.csv`. The source date is retained in each row. A mapping is effective only after it is saved and the snapshot is rebuilt.

To prepare mapping candidates:

```bash
export SEC_USER_AGENT='Parallax research contact@example.com'
uv run python -m src.resolve_lqd_tickers
```

Automatic mappings are limited to unique, exact legal-entity matches. Parent and ambiguous matches are review candidates, never automatic assignments.

## TRACE history

Create or resume a run in **Download TRACE history**. The downloader derives a CUSIP only from US ISINs; correct or supply other CUSIPs in the GUI before retrying. It stores run status and normalized raw observations in `data/trace/history.duckdb`, then writes a run export under `data/trace/parquet/`.

Before treating a run as research-ready, check feed lag, zero/sentinel values, the review status, LQD population date versus TRACE retrieval date, and FINRA/OpenBB usage rights.

## Cleaning and inspection

```bash
uv run python -m src.trace_preprocessor --run-id <run-id>
uv run python -m src.trace_history_inspector
```

The preprocessor writes clean Parquet and diagnostics beneath `data/trace/preprocessed/`; it never modifies raw observations. The inspector displays individual bonds or equal-bond-weighted group levels. Its volatility and momentum columns are changes in a plotted level, not an investable return signal. See [signal methodology](signal_methodology.tex).

## Reproducibility

Record the Git commit, run ID, population snapshot hash/date, requested date range, source and retrieval timestamps, cleaning-policy version, and diagnostics path with every analysis. Do not refresh a dataset in place without retaining the prior vintage.
