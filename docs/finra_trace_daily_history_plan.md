# FINRA TRACE daily bond history plan

## Goal

Try a reproducible daily history for a supplied set of bonds, beginning with a
small sample from the current LQD snapshot. LQD is a convenient pilot input,
not the canonical universe. Keep the instrument-list input replaceable so a
better fund-level source can take over without changing the history provider.
Treat results as observed TRACE end-of-day data, with dates, missing
observations, identifiers, and retrieval lineage visible. Do not fill
non-trading days or describe the series as evaluated prices.

## Candidate OpenBB interface

OpenBB documents `obb.finra.fixedincome.bond_historical` as a daily TRACE
end-of-day price and yield history. It accepts one or more CUSIPs or FINRA
symbols, and the documented default history window is five years. Its fields
include trade date, CUSIP, FINRA symbol/security ID, product type, last sale
price, and last sale yield. The `bond_prices` command is documented with ISIN
and CUSIP filters and can provide reference data and the FINRA identifiers
needed by the history query. The installed FINRA provider must be checked
separately: its ISIN filter may not be implemented by the underlying service.

OpenBB describes `openbb-finra` as a provider connector that needs no OpenBB
credential. OpenBB does not host the data; FINRA controls endpoint access,
coverage, terms, and rate limits. No credential requirement does not establish
that bulk history storage or downstream redistribution is unrestricted. Confirm
the actual endpoint and applicable FINRA terms before building a scheduled
collector.

References:

- [OpenBB FINRA bond history](https://docs.openbb.co/odp/python/reference/finra/fixedincome/bond_historical)
- [OpenBB FINRA bond reference and last sale](https://docs.openbb.co/odp/python/reference/finra/fixedincome/bond_prices)
- [OpenBB provider and credential model](https://docs.openbb.co/odp/python/extensions/providers)
- [FINRA TRACE product overview and agreements](https://www.finra.org/sites/default/files/TRACE_Overview.pdf)
- [FINRA Fixed Income API terms and catalog](https://developer.finra.org/node/1171)

## Phase 1: prove coverage and terms with a small spike

Use a disposable environment with the `openbb-finra` provider. Query a small,
reviewable sample of current LQD bonds that includes liquid and less-liquid
issues, a recent issue, a seasoned issue, and one matured or recently removed
bond if an identifier is available. For each one:

1. Try resolving its ISIN with `bond_prices`; if the FINRA provider rejects
   ISIN, derive and validate the CUSIP from the U.S. ISIN or use an existing
   licensed CUSIP crosswalk. Record the returned CUSIP, FINRA symbol, security
   ID, product type, and any warnings.
2. Query `bond_historical` with explicit start and end dates, then with a list
   of identifiers. Save a redacted result sample and the retrieval metadata.
3. Check actual start-date coverage, empty responses, maximum identifier batch
   size, latency, throttling behavior, and whether missing-trade days are absent
   or represented with a carried close. Check whether corrections alter prior
   observations on a repeat request.
4. Check whether price and yield are present consistently, their units, and how
   returned rows align with the provider's reported trade date.
5. Review the source endpoint, FINRA API terms, and any TRACE agreement or fee
   that applies to the use, retention, and display of the returned data.

**Go/no-go:** proceed with the OpenBB collector only if the small sample gives
reliable bond identifiers and useful history, and the intended internal
storage/display use is permitted. Record any coverage or rate-limit constraints
before choosing a backfill size.

### Initial probe (2026-11-07)

- Installed `openbb-finra==2.0.0` in an isolated uv environment; the project
  requirements were not changed.
- `bond_prices(isin="US037833AL42")` was rejected by the provider with
  `FINRA TRACE does not publish isin. Remove it.` The documented CUSIP lookup
  `bond_prices(cusip="037833AL4")` succeeded, resolving Apple bond CUSIP
  `037833AL4` to FINRA symbol `AAPL4001810`, FINRA security ID `4001810`, and a
  last trade date of `2026-11-06`.
- The first one-bond, one-month `bond_historical` request timed out at the
  provider's 120-second timeout. An identical retry returned 26 rows in about
  28 seconds; subsequent requests returned in 6-8 seconds. The service works
  in this environment, but response time was inconsistent.
- A four-CUSIP request covering `2026-10-01` through `2026-11-06` returned 101
  rows in about 7 seconds, so list input works. Per-CUSIP row counts were 25
  for Aflac, 24 for AEP Texas, 26 for AT&T, and 26 for Apple. The dates were
  present as the DataFrame index in `to_df()` and as `date` in each result.
- The newest row for all four bonds was dated `2026-11-05`, while the latest
  sale lookup for Apple reported `2026-11-06`; an exact Nov 6 history request
  returned `EmptyDataError`. Treat the one-day gap as observed feed lag until
  more dates establish its normal publication schedule.
- The history response includes rows with `price=0.0` and no yield: 8 of 24
  AEP Texas rows, 1 AT&T row, and 1 Apple row in this sample. Do not interpret
  these as zero market prices. Preserve and flag them until FINRA confirms
  their meaning.
- A five-year Apple request (`2021-11-07` through `2026-11-06`) succeeded with
  1,230 rows, from `2021-11-08` through `2026-11-05`. This confirms five years
  are retrievable for this bond. OpenBB documents five years as the default;
  a hard maximum beyond that was not tested and is outside the current pilot.
- **Pilot decision:** the interface is usable for a small, resumable sample.
  Hold off on a broad backfill until the one-day lag, zero-price sentinel
  behavior, intermittent timeout, and applicable FINRA terms have been
  checked. Add bounded retries and quality flags to any collector.

## Phase 2: define identity and observation rules

- Use ISIN as Parallax's stable bond key. Keep CUSIP, FINRA symbol, and FINRA
  security ID as source identifiers in a dated crosswalk; do not assume a
  symbol remains unchanged over a bond's life.
- Resolve from the project's bond snapshot by ISIN. Only accept a unique
  reference-data match; quarantine ambiguous, missing, or conflicting
  identifiers for review rather than guessing from issuer name.
- Preserve the provider's returned trade date, price, and yield as reported.
  Keep price and yield units explicit. Never convert an absent observation into
  a flat price or forward-fill it.
- Treat repeated requests as revisions of the same source observations. Keep
  retrieval time and refresh a bounded recent window after the initial load so
  provider corrections can be captured idempotently.
- Keep raw provider output only if the agreement permits it. Otherwise retain
  normalized fields and request metadata sufficient to reproduce the query.

## Phase 3: backfill and persist

1. Seed this trial from the current `data/snapshots/lqd_bonds.csv`, treating it
   only as the input list for the pilot.
2. Resolve identifiers, then backfill the provider's available history in
   measured batches. Begin with a small batch-size limit established in Phase
   1; increase only after checking response completeness and throttling.
3. Store a normalized observation keyed by `(isin, trade_date, source)` with
   CUSIP, FINRA symbol/security ID, product type, price, yield, retrieval time,
   and source/version metadata. Keep run state and resumable per-bond jobs in
   DuckDB. Export completed runs as Parquet for durable analytical files and
   use Polars for per-bond quality checks. CSV is an optional interchange
   export, not the run store. Keep the provider adapter independent of storage.
4. Make writes resumable and idempotent. Persist completed batches and failures
   so an interrupted backfill resumes without losing successful work.
5. Keep downloaded observations out of source control unless FINRA's terms
   explicitly permit that storage and sharing. Commit code, schemas, and
   synthetic fixtures instead.

## Phase 4: daily maintenance

- Read a supplied fund list and preserve its source date and provenance. Start
  with the existing LQD workflow, but keep that input adapter replaceable so a
  better managed fund source can take over.
- Resolve newly supplied bonds and fetch their available history. Refresh a
  recent lookback window for known bonds to pick up corrections; do not
  redownload the entire history every day.
- Record run start/end time, holdings source date, universe size, resolved and
  unresolved identifiers, rows inserted/updated, empty histories, errors, and
  provider warnings.
- Start with an explicit manual launcher action. Automate scheduling only
  after coverage, terms, retries, and monitoring have been demonstrated.

## Validation and known limits

- Compare requested identifiers with returned CUSIPs/FINRA symbols and flag
  mismatches, duplicate `(isin, trade_date, source)` rows, and unexpected
  product types.
- Report coverage by bond and date: first/last observation, observation count,
  and missing trading days. Missing days are diagnostics, not values to
  synthesize.
- Distinguish last-sale history from a full TRACE transaction tape. The
  documented OpenBB history returns the last sale's price and yield; it does
  not document a complete daily trade count/volume series.
- A history seeded from the current LQD list only covers the supplied bonds;
  it does not claim to reconstruct historical fund membership. A better
  future static holdings source can replace this input without changing the
  bond-history store, provided identifiers are crosswalked consistently.
- The five-year window is what OpenBB currently documents as the default for
  this command, not a guarantee that every bond has five years of observations.
  Verify per-identifier coverage in Phase 1.

## Suggested implementation sequence

1. Complete the Phase 1 spike and write down actual sample coverage, response
   shape, identifier behavior, terms, and rate limits.
2. Maintain DuckDB run state and observations; export completed runs to
   partitioned Parquet and use Polars quality checks.
3. Add an identifier crosswalk and improve the resumable historical backfill
   as observed rate limits and coverage become clear.
4. Add a daily incremental command with correction lookback and run diagnostics.
5. Add history coverage to the inspector, keeping TRACE observations distinct
   from the current holdings snapshot and from any model-implied price.

ClickHouse is a possible later shared analytical store if multiple users or
services need concurrent access. QuestDB becomes relevant if live event or
tick processing is introduced. ArcticDB can be evaluated for versioned
research datasets if dataset branching and revision workflows become a real
need. None is needed for this single-process Python desktop workflow today.
