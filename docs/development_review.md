# Development review — 9 November 2026

Current code supports a LQD-seeded TRACE data pilot and visual inspection. It does not implement fundamental value, latent fair value, forecast returns, portfolio construction, or a validated trading signal.

## Correctness and research risk

| Priority | Finding | Evidence and consequence | Correction |
|---|---|---|---|
| P0 | Historical-run reproducibility depended on overwrite semantics. | The legacy `observations` table still holds the latest value, but new TRACE writes also append to `trace_observation_versions`. Exports and preprocessing select versions at a run cutoff. | Implemented for new retrievals. Historical data predating migration has only its latest surviving value and cannot be reconstructed into earlier vintages. |
| P1 | Group “momentum” and “volatility” are level statistics, not return signals. | `load_group_data` computes `mean - lag(mean, 63/252)` and `stddev_samp(mean)` over price/yield levels. Price differences mix duration; yield changes have the opposite economic sign to price return. | Rename the display fields to level change/dispersion. Construct clean returns, duration/convexity-adjusted yield changes, and standardized residuals for signals. |
| P1 | Rolling windows change economic horizon with UI interval. | The same 63/252 `ROWS` are applied to daily, monthly, quarterly, and yearly views. | Calculate research signals at daily frequency, aggregate afterward, and display the effective horizon. |
| P1 | Snapshot attributes were not frozen at row level. | Older runs may point to a rebuilt source file. | New runs archive the exact CSV bytes under a SHA-256 path and preprocessing reads that archived copy. Legacy runs cannot recover a lost prior file. |
| P2 | Zero yield is globally treated as missing. | This is an untested product convention in the current cleaning policy. | Make rules product-specific and test policy boundaries. |
| P2 | Coverage can count a bond from first raw, not first valid, observation. | `first_seen` is calculated before `value IS NOT NULL`. | Define coverage against frozen membership and valid observations, then label it. |

## Architecture

1. Introduce typed schemas for instruments, observations, run manifests, and reports.
2. Split `trace_history.py` into provider client, run repository, quality assessment, and orchestration.
3. Keep Qt in `ui/`; provide CLI pipeline entry points for headless runs and tests.
4. Write a manifest for every snapshot, run export, and clean Parquet: schema, source IDs, hashes, range, policy, upstream datasets.
5. Replace the empty `TODO.MD` with a small prioritized roadmap or GitHub issues.

## Performance

| Area | Improvement |
|---|---|
| FINRA collection | Adaptive batches, bounded retries/backoff, rate-limit budget, and separately cached identifier resolution. |
| DuckDB writes | Stage each provider batch in Arrow/Polars and merge once per batch instead of per-bond commits. |
| Parquet/inspector | Use a clean analytical view and cache group-period aggregates by dataset manifest, measure, dimension, and interval. |
| Snapshot/mapping | Replace row-wise `DataFrame.apply` identity-key construction with vectorized columns once schema is stable. |

## Minimum validation suite

- A vendor correction cannot alter a prior run export.
- Multiple ISIN/date vintages are retained.
- Python and SQL cleaning classifications agree on boundary values.
- Aggregation gives equal bond weight and documented eligibility.
- Yield-direction and duration-adjusted return signs are correct.
- A frozen snapshot gives the same classifications after a newer snapshot is built.

Next research milestones: validate maturity-matched spreads and their missing-date behavior, then add instrument-master and universe-membership vintages. A full historical restatement of pre-migration TRACE vintages is not possible from the overwritten legacy table.
