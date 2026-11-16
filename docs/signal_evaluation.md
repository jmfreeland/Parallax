# Signal evaluation guide

## Scope

Parallax now evaluates two exploratory credit predictors:

| Predictor | Score | Intended interpretation |
|---|---|---|
| Nelson–Siegel ticker residual | Observed level minus fitted issuer-ticker curve, oriented so higher is cheaper | Cross-sectional relative value |
| Historical partition percentile | Current partition median ranked against strictly earlier values | Regime / relative-level indicator |

They are research diagnostics, not validated alpha or executable strategies.

## Inputs and provenance

Choose exactly one signal space:

- **Yield** — cleaned last-sale yield in percent. A lower forward yield is favourable to a high-cheapness score.
- **G-spread** — `(TRACE yield - maturity-interpolated Treasury par yield) × 100`, in bp. Build a G-spread snapshot first.
- **Price** — clean-price log return. This is structurally less comparable across bonds because coupon, duration, and pull-to-par are not controlled.

The evaluator records the chosen input path and, in G-spread mode, the selected immutable spread snapshot ID. A spread snapshot binds a TRACE run, a Treasury curve vintage, cleaning policy, Parquet hash, and manifest. Use a named dataset and snapshot for any saved research result; do not rely on the UI's “latest” default for a report that must be reproducible.

## Walk-forward construction

At signal date $T$, only observations dated no later than $T$ are eligible. The evaluator limits signal staleness and forward-change gaps. A stale pre-$T$ print does not contribute a forward move spanning information that predates $T$.

For yield or G-spread, the side-adjusted forward outcome reverses the change sign:

$$
o_{i,T,h} = -\sum_{t=T+1}^{T+h} \Delta x_{i,t}.
$$

For price, it retains the log-return sign:

$$
o_{i,T,h} = \log(P_{i,T+h}/P_{i,T}).
$$

Nelson–Siegel event outcomes are cross-sectionally demeaned on each eligible trade date; partition-percentile outcomes retain raw changes. This distinction is deliberate: the former tests relative value, while the latter may retain a market regime move.

Each date’s signal cross-section can be ranked into deciles. The portfolio diagnostic longs the top residual quantile and shorts the bottom quantile. Event windows overlap, so their averages are descriptive rather than independent return observations.

## Total-return and financing diagnostics

The maximum-horizon report can calculate total return from clean prices, accrued interest, and a synthetic coupon schedule. It:

- uses a semiannual schedule rolled back from maturity when actual coupon dates are absent;
- requires valid price boundaries subject to the configured staleness limit;
- excludes fees, taxes, defaults, settlement lag, and coupon reinvestment;
- reports coverage rather than silently reweighting missing positions.

The rebalanced long-only financing diagnostic accrues 1-month Treasury plus bond G-spread with ACT/365. It is a financing proxy, not a borrow, haircut, repo-specialness, or transaction-cost model.

## Interpretation checklist

Before comparing or exporting a result, record:

1. bond-list file/hash and its membership date;
2. data-source path/hash, cleaning-policy version, and TRACE run;
3. G-spread snapshot and Treasury curve vintage, where applicable;
4. signal space, staleness/gap limits, horizon, quantile, and minimum cross-section;
5. price and total-return coverage; and
6. whether outcomes are raw or cross-sectionally demeaned.

## Known limitations

- The default cleaned-Parquet and G-spread choices select the latest available artifacts. This is convenient for exploration, but is not an immutable research specification.
- Raw-DuckDB total-return routines still read the legacy latest-value `observations` table; use a frozen Parquet/run view for vintage-safe research.
- Current membership comes from the selected bond-list CSV, not historical index membership.
- Yield outcomes remain a rates-plus-credit proxy. G-spread is the benchmark-relative variant, subject to interpolation and curve-data limitations.
- The ticker curve does not yet control for rating, seniority, optionality, liquidity, coupon, or security-level issuer structure.
- Price-space curve fits and portfolio outcomes remain exploratory because clean price is not a duration- or cash-flow-normalized state variable.

See [the methodology note](signal_methodology.tex) for model definitions and [the development review](development_review.md) for the prioritized remediation plan.
