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

**Update Treasury curve** fetches the U.S. Treasury XML feed in one request per
calendar-year batch. The progress bar and status show the current year, batches
completed, returned date counts, elapsed time, and an approximate remaining
download time after the first batch. CSV, DuckDB, and manifest writes are shown
as final progress steps.

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

The launcher **Signals** tab lists implemented signal components and their
validation status. Use the **Strategies** tab to evaluate individual signal
components or open **Define strategy graph** to create and save a reusable
combination. Choose **Evaluate signal components** to compare the
Nelson-Siegel residual with historical partition percentile and z-score signals over a selected
start/end range, and choose **Yield**, **G-spread**, or **Price** as the signal
space. Signals and forward outcomes use the same selected space. G-spread mode
uses and records the latest immutable spread snapshot. Price mode uses clean
price log returns; fitting a price curve is exploratory because coupon and
structure differences also affect price. Select **Cleaned Parquet**, **Preprocessed Parquet**, or **Raw
DuckDB history** as the input source; cleaned data is the default and the
latest cleaned dataset is selected automatically. The report records the
selected source and path. The cleaner's carried marks and reversal flags are
separate from observed values; the evaluator reads the observed columns and
ignores those marks and retrospective flags. On every eligible observation
date in the selected space, the evaluator fits the ticker curve in the selected
space or computes a partition percentile or z-score. Both partition signals
use the selected partition's median level against strictly prior observations
in the trailing history window. The percentile is self-limiting to 0–100; the
z-score reports standard deviations from the historical mean using the sample
standard deviation, without winsorization. From each signal date, it measures
post-signal changes and accumulates them by horizon.
For Nelson-Siegel residuals, it subtracts the cross-sectional mean change on
each trade date. If a signal uses a stale observation from before T, its first
later print is excluded from forward changes because that move spans
time before the signal; changes resume after two post-signal prints. For
Nelson-Siegel residuals, dates with fewer than five eligible bond changes are
omitted. The signal-to-change correlations reverse
the outcome sign so a positive result is favorable to a high cheapness score.
**Horizon performance** ranks the full daily signal
cross-section into ten equal-count groups, then plots each group's average
cumulative side-adjusted selected-space change in basis points relative to that date's
population average from T+1 through T+end. It does not apply duration scaling
or long/short tail cutoffs.
The separate **Signal correlations** panel shows daily-averaged and pooled
Pearson and Spearman IC. Signal staleness, maximum gap between selected-space changes,
minimum curve size, price age, horizon, and long-short tail quantile are
configurable.

Use **Define strategy graph** on the launcher's **Strategies** tab to
create, validate, open, and save reusable strategy JSON in a node canvas.
Strategies default to the repository's `data/strategies/` folder. Each
signal is a branch into the shared combination node, with its transforms shown
in order; final transforms sit after combination. Nodes can be dragged to
arrange the canvas, and the canvas supports wheel zoom, drag-to-pan, and a
**Fit graph** control. Selecting a node opens its settings. Cards summarize
the predictor, signal space, group, weight, transform parameters, or combination
rule; node cards also include a short explanation of their operation, and arrows
show the score flow. **Global strategy settings** contain the strategy name;
**Selected node settings** show only the controls for the clicked block, with the
combination method on the combiner node and transformer type/parameters on each
transformer node. Signal grouping uses a dropdown populated from the selected
reference bond-list CSV, so fields absent from that universe are flagged rather
than silently accepted. A strategy contains uniquely named signal
components, independent transformer nodes connected between signal branches
and the combiner, and a combiner node with its own method. Supported
combinations are weighted mean, equal mean, weighted sum, and rank mean.
Supported cross-sectional transforms are winsorization, z-score normalization,
percentile rank, and min-max scaling. A transformer applies to the upstream
signal or combined score feeding its input. Missing component scores are
omitted and mean weights are renormalized over the available components. The
editor starts with an empty graph and three block types: signal, transformer,
and combiner. Add signal blocks, insert transformer blocks on a branch, and
select the combiner node to set how branches are aggregated. Definitions can
be saved independently of a test run.

Use **Calculate inputs** in the strategy editor to evaluate and cache the
individual bond/date scores for the selected date range and TRACE source. The
cache is stored under `data/strategies/runs/`. Recalculating with the same
date range and data source reuses unchanged signal components; newly added or
edited signal components are calculated, while a changed date range or source
refreshes all inputs. **Run strategy** applies the current graph to those
cached inputs and saves the combined dated bond scores alongside the cache.
Changing only a combiner or transformer does not require recalculating signal
inputs. **Export latest scores** writes a CSV for the latest date present in
the cache, including component scores, the combined score, bond details from
the reference list (ticker, coupon, maturity, sector, issuer, and rating where
available), the values entering each combiner, and a descending score rank.
The CSV includes both raw component scores and transformed graph-node inputs
so the path into the final score can be inspected. This is a ranked signal
output; it does not assign position sizes.

**Inspect strategy** opens a date-selectable heatmap table. It shows bond
metadata, each raw signal, each non-signal value entering a combiner, and the
final strategy score. Cells are colored from low (red) to high (green) within
each date and column; columns can be sorted to find the largest and smallest
scores.

Use **Signal vs. outcome** to inspect a bond-level cross-section. After an
evaluation completes, change the signal date or forward horizon directly in
that tab; the app redraws from the cached outcomes for every tested horizon and
date, without rerunning the evaluation. Compare each signal value with its
cumulative side-adjusted selected-space outperformance versus the eligible
population to inspect outliers and the shape of the signal relationship.

For the percentile predictor, choose any field in the bond-list CSV as the
partition (for example sector, issuer ticker, bond ticker, or maturity year).
Each partition's latest eligible median in the selected space is ranked against
strictly prior partition medians in a trailing window; minimum history and
lookback are configurable. Scores are oriented so higher means cheaper; bonds
within selected partitions are equally weighted. Percentile outcomes use raw,
non-demeaned changes so market-wide moves are retained. Yield changes remain a
Treasury-plus-credit proxy; G-spread mode uses the stored benchmark-relative
series. The selected partition uses the current bond-list snapshot, not
point-in-time membership.

In Nelson-Siegel mode, each signal date's portfolio goes long the top residual quantile
(cheap versus its ticker curve) and shorts the bottom quantile (rich). The
maximum-horizon date report includes the equal-weighted long-short historical
total return when every selected position has valid boundary prices; it also
reports coverage and does not silently reweight missing positions. Total return
uses the current synthetic coupon schedule and excludes costs, taxes, defaults,
and coupon reinvestment.

The **Rebalanced portfolio** tab compares holdings selected at each signal
date and held through the next observed signal date. It plots the raw
long-short total-return contribution, the long-only leg gross and net of
financing, and the selected-space change proxy. Yield and spread changes use
modified duration; price changes use clean-price log returns. Portfolio returns
are not cross-sectionally de-meaned. The financing proxy accrues the
latest 1-month Treasury rate plus each bond's G-spread from the latest spread
snapshot using ACT/365; a checkbox can disable this adjustment. Annualized
volatility is calculated from the raw non-overlapping holding-period returns.
Unpriced bond returns contribute zero and coverage is reported. Yield changes
have no Treasury benchmark adjustment, so those remain proxies; the explicit
G-spread series is benchmark-relative. Signal
dates use the supplied bond list rather than historical index membership. The
TRACE store does not retain immutable observation vintages, so later
corrections can affect this retrospective test. Event windows overlap, so
horizon averages are descriptive and are not independent or annualized
performance estimates. Export the date report JSON and bond-date observation
CSV for further analysis.

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

Use **Analyze bond return series** on the launcher's **Data** page to create
business-day bond total-return and financing-excess series for one ISIN. Select
a bond by typing part of its ISIN, issuer, coupon, or maturity; each entry shows
all four identifying details. Select a Treasury curve tenor from the history
stored in the shared DuckDB when using the unhedged financing mode. The
default-on **Treasury-hedged total return** subtracts a duration-matched Treasury
benchmark return from the bond's realized total return. The benchmark includes
par-yield carry and a duration-scaled price move using Treasury yields stored
with the bond's G-spread observations. Hedge duration is recomputed with
QuantLib from each point-in-time observed bond yield, rather than using today's
duration for the full history. The actual bond total return already
includes coupon carry; the G-spread carry and spread-price estimates are shown
separately as diagnostics, not added a second time. The unhedged bond total
return remains visible in either mode. Financing
controls and financing-excess results are hidden while the hedge is on. Turn
the hedge off to use Treasury-rate financing: the latest rate known at each
interval start accrues using ACT/365, and the negative funding-cost line assumes
the full unhedged bond notional is financed. Financing cost is not charged to
the G-spread-hedged series, treated as approximately cash neutral; residual
cash, repo, and margin financing are not modeled. The hedge mode's chart shows
cumulative G-spread carry; the unhedged mode's chart shows funding cost and log
excess return. A lower panel plots actual G-spread snapshot observations in
either mode. The log excess return is
`log(1 + bond total return) - log(1 + financing accrual)`. TRACE prices carry
forward between prints and their age is reported. The output includes interval
log excess returns and cumulative bond, financing, and excess indices, with CSV
and summary JSON export. Treasury observations are read directly from the same
DuckDB used for TRACE history and are selected by tenor.

## Reproducibility

Record the Git commit, run ID, population snapshot hash/date, requested date range, source and retrieval timestamps, cleaning-policy version, and diagnostics path with every analysis. Do not refresh a dataset in place without retaining the prior vintage.
