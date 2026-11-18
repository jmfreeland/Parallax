# Development review — 16 November 2026

Parallax now has versioned TRACE data, G-spread snapshots, exploratory portfolios, and walk-forward signal research. Those additions improve lineage, but default input selection still prevents a result being reproducible unless every input is pinned.

## Correctness and research risk

| Priority | Finding | Evidence and consequence | Recommended correction |
|---|---|---|---|
| P0 | Strategy graph combination can fail on valid transformed/nested branches. | `combine_signal_scores` looks up `component_weights[input_id]` before evaluating source transformer or nested-combiner nodes, although weights are only propagated during evaluation. This can raise `KeyError`; direct leaves are the only presently reliable combiner inputs. | Evaluate source maps before resolving weights and make weights explicit at each node; test direct, transformed, nested, missing-score, and negative-weight graphs. |
| P0 | Strategy cache signatures do not hash file contents. | The cache hashes parameter paths and dates, not the contents of CSV/Parquet inputs. Replacing a file in place can reuse stale component scores. | Hash all immutable input bytes/manifests in the cache key and save a ResearchRun manifest alongside the cache. |
| P0 | Signal evaluation defaults to mutable “latest” artifacts. | Cleaned Parquet is selected by modification time; G-spread and financing inputs select their latest snapshots. Inputs may therefore represent different TRACE or curve vintages and re-runs can differ. | Require a ResearchRun manifest: dataset hash, TRACE run, clean ID, G-spread snapshot, curve vintage, bond-list hash, policies, parameters, and output hashes. Keep “latest” explicitly exploration-only. |
| P0 | Some return/raw-history paths still read \`observations\`, the latest-value compatibility table. | New TRACE values append to \`trace_observation_versions\`, but raw-DuckDB evaluation and total-return routines query \`observations\`. Later vendor corrections can change retrospective results. | Route all analytical reads through a pinned as-of/run version view; mark pre-migration records as latest-value-only. |
| P1 | Snapshot workflow has no immutable baseline-to-final lineage. | The guided sequence builds a baseline CSV, generates issuer candidates, then fetches holdings again and overwrites the same final CSV. A holding can enter/leave between calls and no source-file hash, mapping-file hash, or candidate baseline ID is persisted with the final snapshot. | Archive raw holdings under a content hash and write a manifest naming raw-holdings hash/date, candidate artifact hash, mapping hash, and final snapshot hash. Warn when final-universe membership differs from the reviewed baseline. |
| P1 | Viewer “momentum” and “volatility” are still level statistics. | 63/252-row calculations use group price/yield levels, and their calendar horizon changes with UI interval. | Rename to level change/dispersion; calculate daily return and risk measures in research, then aggregate. |
| P1 | Signal outcomes intentionally use different estimands. | Nelson–Siegel outcomes are cross-sectionally demeaned; percentile outcomes are raw. Yield/spread changes reverse sign; price uses log returns. | Preserve the distinctions but include them in titles/exports; prohibit pooled comparison across estimands without a common target. |
| P1 | Total return has synthetic coupon dates. | Schedules are rolled back from maturity because actual coupon dates are absent. | Add authoritative schedules or label results approximate; validate against known-bond fixtures. |
| P1 | Current bond-list attributes stand in for historical membership/classification. | Ticker, partition, duration, coupon, and maturity come from a selected current CSV. | Add instrument-master and as-of universe/attribute vintages; label historical tests accordingly. |
| P2 | G-spread is a maturity-interpolated par-yield proxy. | It linearly interpolates Treasury par nodes, not a discount/zero curve. | Validate node/between-node/missing-endpoint cases and sensitivity to curve representation. |
| P2 | Zero yield is globally missing. | This is a policy assumption, not a product-specific convention. | Make cleaning product-aware and unit-test boundary values. |
| P2 | Coverage is reported but not gated. | Price, financing, and total-return coverage can differ materially. | Require numerator/denominator in all headlines and a configurable research coverage gate. |

## Architecture priorities

1. Create one immutable \`ResearchRun\` resolver used by every evaluator, portfolio report, and export.
2. Separate repositories (TRACE versions, curves, spreads, manifests) from pure calculations and Qt UI clients.
3. Consolidate repeated latest-snapshot lookup into explicit \`exploratory_latest\` and \`pinned\` modes.
4. Promote typed models for attributes, observations, quality flags, signals, returns, and portfolio legs.
5. Continue the package extraction described in [architecture.md](architecture.md), with unit and integration tests.

## Performance opportunities

| Area | Improvement |
|---|---|
| G-spread build | Year-by-year DuckDB SQL is a good improvement. Benchmark lateral Treasury-node lookup; a pre-expanded daily curve or interval join may scale better. |
| Signal evaluation | Cache pinned datasets, date cross-sections, curve fits, and forward outcomes by manifest + parameter hash; reuse overlapping windows. |
| Total return | Batch boundary-price lookup and cash-flow calculation by instrument/date grid instead of repeated per-window database work. |
| UI | Virtualize/paginate large tables and stream exports. |
| Collection | Use adaptive batches, bounded backoff, rate-limit budget, and cached identifier resolution. |

## Minimum validation suite

- A vendor correction cannot alter an export or performance report pinned to an older run.
- Mixed TRACE/clean/spread/curve lineage is rejected unless an override is recorded.
- G-spread interpolation matches hand-checked node, between-node, and missing-endpoint cases.
- Yield/spread sign, price log return, duration approximation, and cash flows agree with fixtures.
- Exports identify raw versus demeaned outcomes.
- Tail membership, equal weights, missing-price policy, financing coverage, and maturity handling are deterministic.
- Changing UI interval never changes a research signal’s economic horizon.
- Pre-migration observations are clearly marked as not vintage-reproducible.

## Recommended sequence

Ship pinned ResearchRun manifests and versioned analytical reads first. Next add authoritative historical attributes and validate Treasury interpolation and total-return fixtures. Only then use walk-forward results for model selection, transaction-cost modelling, or portfolio construction.
