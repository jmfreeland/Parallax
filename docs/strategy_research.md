# Strategy research guide

## Purpose

A strategy is an editable directed acyclic graph of signal components, cross-sectional transforms, and combiners. It is an exploratory research recipe, not an execution specification or a validated portfolio.

The current component catalogue supports:

- issuer-ticker Nelson-Siegel residuals;
- historical partition percentile and partition z-score signals; and
- yield, G-spread, or price signal spaces.

Signal inputs are calculated once and cached. Editing a transform or combiner recombines cached scores without rerunning component evaluation.

## Graph semantics

Each signal node produces a bond-to-score map for a signal date. Transform nodes apply a cross-sectional operation while preserving missing scores:

| Transform | Definition |
|---|---|
| Winsorize | Caps scores at configured cross-sectional quantiles. |
| Z-score | Centres and scales the available cross-section using sample standard deviation. |
| Percentile | Maps mid-ranks to `[0,1]`. |
| Min-max | Maps the available range to `[0,1]`. |

A combiner uses only scores available for a bond on that date. Mean and weighted mean therefore renormalize over present inputs. This avoids treating missing as zero, but changes the effective signal mix across names and dates; coverage and per-component availability must be exported with a result.

## Combination requirements

Never combine raw yield, G-spread, and price scores without an explicit normalized comparable score. Recommended practice is to orient every component such that a higher value means cheaper, transform within the same date's eligible cross-section, and use a declared combination method and weights.

Before interpreting a combined score, record the strategy JSON/hash, component input hashes, TRACE/clean dataset, G-spread snapshot and curve vintage, bond-list hash, as-of date, transform parameters, missing-score policy, and cache signature.

## Current implementation limitation

Nested graphs are not yet safe. In `combine_signal_scores`, a combiner constructs its input-weight lookup before evaluating transformer or nested-combiner branches. Such branches do not yet have a propagated `component_weights` entry, despite the following comment saying they should. A valid graph containing a transform or nested combiner can therefore raise `KeyError`.

Until corrected, use only direct signal-to-combiner connections. The fix is to evaluate all source maps first, then derive each input's propagated weight (or, preferably, make node weights explicit rather than inheriting component weights).

## Research limitations

- Cache signatures use parameter paths rather than file content hashes, so changed CSV/Parquet contents at the same path can reuse stale scores.
- The strategy cache is a score cache, not a full immutable research-run manifest.
- Component scores may have different outcome definitions and exposures; combining them is not diversification proof.
- Cross-sectional transforms are sensitive to universe and missingness changes.
- No transaction costs, liquidity, issuer constraints, turnover, capacity, or portfolio optimizer are implemented.

See [signal evaluation](signal_evaluation.md) and [development review](development_review.md).
