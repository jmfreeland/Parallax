# Security master and universe membership

## Current state

`data/snapshots/lqd_bonds.csv` is a dated output of the current LQD holdings
and issuer-mapping workflow. It is useful as a reproducible population input
for a history run, but it is not a canonical security master: it does not own
all bond identities, effective-dated identifier history, security lifecycle,
or user-managed exclusions.

## Separate the concepts

### Security master

One stable Parallax `security_id` per instrument, with ISIN as the preferred
external bond identifier when available. Store security attributes and issuer
relationship separately from any one data source. Identifier aliases such as
CUSIP and FINRA symbol should be records with source and effective dates, so a
changed or corrected identifier does not rewrite the past.

Candidate fields include instrument type, coupon, maturity date, issue date
when known, currency, seniority, rating references, issuer ID, and lifecycle
status. Every sourced attribute should carry source, source-as-of date, and
retrieval time where available. Unknown values remain unknown; do not infer
issue date from first observed TRACE trade.

### Universe membership

Membership is a dated many-to-many relation between a named universe and a
security. A fund holdings snapshot, an analyst-curated universe, or a model
population can each be a source of membership without defining the security
master.

Membership records should support effective start/end dates, source snapshot,
included/excluded state, exclusion reason, and user note. Preserve membership
history rather than overwriting it. Prefer a universe-scoped exclusion; use a
global security exclusion only when the instrument itself should never be
considered (for example, a duplicate identity or unsupported instrument
type).

### Market observations and preprocessing

Raw TRACE observations remain keyed to the stable security identity and
provider identifiers. The provider-neutral preprocessor consumes normalized
fields and a named source policy, emits clean derived fields and diagnostics,
and never alters raw observations. Other providers can register their own
sentinel and range policy without changing the generic engine.

## Candidate workflow

1. Import a dated source list as a candidate population; never replace the
   security master with the latest snapshot.
2. Match candidates by stable identifiers; queue conflicts and missing IDs
   for manual resolution.
3. Add or update source-backed security attributes and identifier aliases.
4. Create an effective-dated membership snapshot for the named universe.
5. Apply explicit membership exclusions with reason, scope, and note.
6. Run the chosen source adapter and preprocessor against the selected
   membership set; keep the input snapshot and preprocessing report linked to
   the run.

## Migration direction

Keep the existing LQD snapshot and history-run inputs working during the first
step. Introduce the master and membership tables in DuckDB, seed candidate
securities and a dated `LQD` membership from the latest snapshot, then change
the snapshot workflow to update membership and metadata through reviewable
reconciliation. Avoid a one-shot migration that drops the current snapshot
workflow or rewrites stored TRACE observations.
