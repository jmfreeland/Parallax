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

In **Download TRACE history**, use **Refetch + fill price/yield gaps** to
re-request bonds with missing or invalid observations in the selected run's
date window. Existing valid raw values are preserved. A fetched value replaces
an existing invalid/missing value only when it passes the TRACE cleaning
policy; each replacement is recorded in `observation_augmentation_fields`,
with run-level counts in `observation_augmentation_runs`. The run's Parquet
export is refreshed when values or rows are added.

## Cleaning and inspection

```bash
uv run python -m src.trace_preprocessor --run-id <run-id>
uv run python -m src.trace_history_inspector
```

The preprocessor writes clean Parquet and diagnostics beneath `data/trace/preprocessed/`; it never modifies raw observations. The inspector displays individual bonds or equal-bond-weighted group levels. Its volatility and momentum columns are changes in a plotted level, not an investable return signal. See [signal methodology](signal_methodology.tex).

## Exploratory portfolios

Open the **Portfolio tooling** tab in the launcher and choose **Build exploratory portfolio**. Choose a bond-list CSV
with an `isin` column, an as-of date, and a weighting method. A bond enters the
date-specific universe only if at least one valid TRACE price or yield exists
on or before that date. The table shows when each bond's valid history began
and its latest price and yield observations with their separate dates; bonds
already matured by the portfolio date are excluded. Select a table row to see
separate clean-price and yield scatter plots from 90 days before through 90 days
after the portfolio date. The post-date chart points are visual context only;
portfolio membership and weights use no observations after the selected date.
Export the selected portfolio and its weights as CSV for further analysis. The
supplied bond list controls the candidate universe, so the tool does not
recreate historical index membership.

Use **Save to archive** in the builder to save a self-contained JSON record under
`data/portfolio_archive/`. The record retains its creation timestamp, as-of date,
bond-list path, weighting methodology and parameters, universe/exclusion counts,
and every portfolio position and weight. Review saved records and methodology
details in the launcher's **Portfolio tooling** tab. CSV export remains available
for analysis outside Parallax.

Equal weight assigns the same weight to every eligible bond. Inverse
price-return variance uses consecutive observed log clean-price changes over
the trailing calendar window and excludes bonds without the requested minimum
number of observations. These are exploratory price-only weights; TRACE
observations are irregular and this method is not a total-return or
liquidity-adjusted risk estimate.

The optional **Require valid yield by date** filter excludes bonds with valid
price history but no valid yield observation available by the selected date.

### Nelson-Siegel ticker curves

In **Portfolio tooling**, choose **Explore Nelson-Siegel curves**. For each
mapped equity issuer ticker, the tool takes each unmatured bond's latest valid
TRACE yield on or before the selected date and fits a four-parameter
Nelson-Siegel curve to yield versus remaining maturity. The decay parameter is
selected from a fixed 100-point log-spaced grid from 0.25 to 15 years; the three
loadings are least-squares estimates. A ticker needs at least five bonds by
default and four distinct maturities. Tickers with insufficient observations
are listed with the reason.

The output reports fitted yield and observed-minus-fitted yield residual in
percentage points. A positive residual is labelled cheap versus that ticker's
curve; a negative residual is labelled rich. This is a cross-sectional
relative-value signal candidate, not a validated future-return forecast. The
fit uses equity issuer ticker because rating data are not yet available; it
does not control for rating, seniority, coupon, optionality, liquidity, or
TRACE observation age. Export results to CSV to preserve the date-specific
fits and diagnostics.

After fitting, use the **Curve explorer** tab to choose a fitted ticker. The
upper chart overlays observed bond yields with the fitted curve; the lower chart
shows each bond's observed-minus-fitted residual by remaining maturity. The
table beneath it gives the exact ISIN, yield date, fitted yield, and residual.
The **All results** tab includes tickers that could not be fitted and their
minimum-data reason.

## Portfolio horizon P&L

In the launcher's **Portfolio tooling** tab, choose **Analyze archived portfolio**
to calculate a saved portfolio's horizon P&L through a selected end date. The
view includes bond, sector, and maturity-bucket attribution for clean-price
movement, coupon cash plus accrued-interest change, and principal repayment;
it also summarizes sector and maturity exposure, coupon/yield coverage,
remaining maturity, issuer concentration, and priced-weight coverage. Use the
price-age control to set the maximum age of boundary prices. Unpriced positions
remain uncovered rather than being reweighted; both covered P&L contribution
and the return normalized to priced positions are shown. The summary includes
weighted modified duration on positions with reported yields. Bond attribution
can be exported as CSV. The calculation holds the archived weights constant
without rebalancing, and coupon or redemption proceeds earn zero after receipt.
The **Time series** tab plots cumulative P&L at monthly month-end marks and at
the selected horizon. Partition attribution tables and P&L charts show the
configurable largest contributors by absolute P&L, so both gains and losses can
appear in the ranking.

## Bond total return

`src.bond_returns.calculate_historical_bond_total_return` calculates an
individual fixed-rate bond's holding-period return from TRACE clean prices,
accrued interest, and coupon/redemption cash flows. Pass the bond's ISIN,
coupon, maturity, and requested dates. It returns the actual selected price
dates and the return as both a decimal and a percentage. By default, each
boundary price must be no more than five calendar days old.

```python
from datetime import date
from src.bond_returns import calculate_historical_bond_total_return

result = calculate_historical_bond_total_return(
    isin="US001055BJ00",
    start_date=date(2025, 1, 2),
    end_date=date(2025, 12, 31),
    coupon_pct=3.6,
    maturity_date=date(2030, 4, 1),
)
print(result.total_return_pct)
```

Coupon dates are estimated by rolling a semiannual schedule back from maturity
because actual issue/coupon dates are not in the snapshot. Coupon cash is not
reinvested; fees, taxes, defaults, and settlement lag are not included.

## Reproducibility

Record the Git commit, run ID, population snapshot hash/date, requested date range, source and retrieval timestamps, cleaning-policy version, and diagnostics path with every analysis. Do not refresh a dataset in place without retaining the prior vintage.
