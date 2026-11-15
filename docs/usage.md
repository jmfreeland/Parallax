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

The preprocessor writes policy-cleaned Parquet and diagnostics beneath
`data/trace/preprocessed/`; it never modifies raw observations. Policy cleaning
rejects invalid values such as zero/sentinel entries. To build a separate
time-series-cleaned version, use **Clean preprocessed TRACE data** in the
launcher and select an `observations.parquet` file from a preprocessed run.
This explicit step writes a new versioned dataset beneath the selected
preprocessed folder, plus a diagnostics JSON; it does not change the preprocessed
source. The signal evaluator can select this output and defaults to the latest
cleaned dataset. It creates missing business-day mark columns by carrying the last observed value forward for up to
the configured age (default three business days). The observed `clean_price`
and `clean_last_sale_yield` columns remain unchanged; carried marks and their
ages are separate fields. Large isolated moves that promptly reverse near the
prior level are flagged per field, while their observed values remain unchanged
because the flag requires a later print. Those flags are retrospective. The
signal evaluator reads the unchanged observed columns and ignores mark and flag
columns. The inspector displays
individual bonds or equal-bond-weighted group levels. Its volatility and
momentum columns are changes in a plotted level, not an investable return
signal. See [signal methodology](signal_methodology.tex).

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
Nelson-Siegel curve to yield versus remaining maturity. The three loadings are
least-squares estimates. The decay parameter is selected by leave-one-bond-out
fit error from 100 log-spaced candidates, bounded by the cross-section's
maturity range (with a 0.5-year floor and a 15-year cap). A ticker needs at least five bonds by
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
The curve is drawn only across maturities represented by the ticker's bonds, so
the plot does not imply a fitted value at zero maturity when there is no short
bond observation. The displayed leave-one-out error is the tau selection score,
not an independent out-of-sample forecast evaluation. The **All results** tab
includes tickers that could not be fitted and their minimum-data reason.

## Signal inventory and evaluation

The launcher **Signal inventory** tab lists implemented signals and their
validation status. Choose **Evaluate credit predictors** to compare the
Nelson-Siegel residual with a historical partition percentile over a selected
start/end range. Select **Cleaned Parquet**, **Preprocessed Parquet**, or **Raw
DuckDB history** as the input source; cleaned data is the default and the
latest cleaned dataset is selected automatically. The report records the
selected source and path. The cleaner's carried marks and reversal flags are
separate from observed values; the evaluator reads the observed columns and
ignores those marks and retrospective flags. On every TRACE yield
date in that range, the evaluator fits the ticker curves using observations
available by that date and records each bond's yield residual. From each
signal date, it measures post-signal changes and accumulates them by horizon.
For Nelson-Siegel residuals, it subtracts the cross-sectional mean change on
each trade date. If a signal uses a stale yield observed before T, its first
later yield print is excluded from forward changes because that move spans
time before the signal; changes resume after two post-signal prints. For
Nelson-Siegel residuals, dates with fewer than five eligible bond changes are
omitted. The signal-to-change correlations reverse
the change sign so that positive correlation means the residual correctly
indicated tightening. **Horizon performance** ranks the full daily signal
cross-section into ten equal-count groups, then plots each group's average
cumulative duration-adjusted return proxy relative to that date's population
average from T+1 through T+end. It does not apply the long/short tail cutoffs.
The separate **Signal correlations** panel shows daily-averaged and pooled
Pearson and Spearman IC. Signal staleness, maximum gap between yield changes,
minimum curve size, price age, horizon, and long-short tail quantile are
configurable.

Use **Signal vs. return** to inspect a bond-level cross-section. After an
evaluation completes, change the signal date or forward horizon directly in
that tab; the app redraws from the cached outcomes for every tested horizon and
date, without rerunning the evaluation. Compare each signal value with its
cumulative side-adjusted yield change to inspect outliers and the shape of the
signal relationship.

For the percentile predictor, choose any field in the bond-list CSV as the
partition (for example sector, issuer ticker, bond ticker, or maturity year).
Each partition's latest eligible median TRACE yield is ranked against strictly
prior partition medians in a trailing window; minimum history and lookback are
configurable. Higher percentiles select the high-yield partition tail for the
long leg, and lower percentiles select the short leg; bonds within selected
partitions are equally weighted. Its outcomes use raw, non-demeaned yield
changes so market-wide regime moves are retained. The current TRACE store does
not have benchmark-relative bond spreads, so yield is explicitly a spread proxy
and can also reflect Treasury-rate and maturity-mix changes. The selected
partition uses the current bond-list snapshot, not point-in-time membership.

In Nelson-Siegel mode, each signal date's portfolio goes long the top residual quantile
(cheap versus its ticker curve) and shorts the bottom quantile (rich). The
maximum-horizon date report includes the equal-weighted long-short historical
total return when every selected position has valid boundary prices; it also
reports coverage and does not silently reweight missing positions. Total return
uses the current synthetic coupon schedule and excludes costs, taxes, defaults,
and coupon reinvestment.

The **Rebalanced portfolio** tab plots two indices for holdings selected at
each signal date and held through the next observed signal date: a covered
total-return contribution index and a duration-adjusted yield-only proxy. The
proxy accumulates the intervening daily changes (cross-sectionally de-meaned
for Nelson-Siegel and raw for the percentile mode), then applies modified
duration and the long/short side.
Unpriced positions contribute zero to the covered total-return index and the
table reports price coverage. TRACE history has no Treasury benchmark, so
yield levels and changes are proxies, not benchmark-relative option-adjusted
spreads. Signal dates use the supplied bond
list rather than historical index membership. The TRACE store does not retain
immutable observation vintages, so later corrections can affect this
retrospective test. Event windows overlap, so horizon averages are descriptive
and are not independent or annualized performance estimates. Export the date
report JSON and bond-date observation CSV for further analysis.

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
The **Time series** tab plots cumulative P&L at daily weekday marks through
the selected horizon. It carries the latest valid clean price forward across
missing observations while accruing interest and coupon cash flows by mark
date; the stale marked weight is reported. Partition attribution tables and
P&L charts show the configurable largest contributors by absolute P&L, so both
gains and losses can appear in the ranking. The performance summary includes
cumulative and annualized return, annualized volatility, Sharpe and Sortino
ratios, maximum drawdown, Calmar ratio, positive-day share, and historical
95% VaR/CVaR. Annualization assumes 252 weekday marks; Sharpe uses a zero
risk-free rate and Sortino uses a zero return target.

## Portfolio covariance explorer

In **Analyze archived portfolio**, open **Covariance explorer** and calculate
the matrix for the selected archived holdings. The initial view shows
bond-to-bond sample covariance of daily clean-price returns in bps². Values
are based on shared weekday marks that carry the latest TRACE price forward
for up to the selected age; coupon and carry are excluded. Each bond pair uses
its own overlapping dates and must meet the selected minimum observation
count, so the pairwise matrix can be noisy and need not be positive
semidefinite. Switch to correlation or aggregate the bond-pair estimates by
sector, issuer, or maturity bucket without recalculating. Partition cells show
the mean available bond-pair estimate; diagonal blocks include each bond's
variance or self-correlation.
Set color lower and upper bounds manually or use **Auto scale**. Hover a cell
for its available pair count or mean shared-observation coverage.

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

Use **Analyze bond return series** in the launcher to create business-day bond
total-return and financing-excess series for one ISIN. Select a short-rate CSV
with a date field (`date`, `observation_date`, `trade_date`, or `effective_date`)
and a rate column expressed in percent. The user-entered annualized financing
spread is added in basis points. At each interval, the rate known at the
interval start is used, with ACT/365 simple accrual; the log excess return is
`log(1 + bond total return) - log(1 + financing accrual)`. TRACE prices carry
forward between prints and their age is reported. The output includes interval
log excess returns and cumulative bond, financing, and excess indices, with CSV
and summary JSON export. The local `main` branch currently has no tracked
government-yield history file, so provide the short-rate history CSV explicitly.

## Reproducibility

Record the Git commit, run ID, population snapshot hash/date, requested date range, source and retrieval timestamps, cleaning-policy version, and diagnostics path with every analysis. Do not refresh a dataset in place without retaining the prior vintage.
