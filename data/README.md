# LQD snapshot workflow

`src/build_lqd_snapshot.py` normalizes the current iShares LQD holdings to a
compact bond-level schema at `data/snapshots/lqd_bonds.csv`. Run it from the
project root with:

```powershell
uv run python -m src.build_lqd_snapshot
```

The output contains only `Fixed Income` records with an ISIN. Coupon, yield,
and duration retain their source units, with percentages stored as percentage
points (for example, `5.25` means 5.25%). Maturity is provided as an
ISO-formatted date plus separate numeric month and year columns. Market value
and portfolio weight are omitted from the snapshot. Source holdings date is
included on each row.

Issuer equity tickers are not inferred directly from bond ISINs. Unique exact
GLEIF legal-entity to SEC matches are applied automatically; manual mappings
can be added to `issuer_ticker_map.csv` after review. Parent-company candidates,
ambiguous matches, and unresolved issuers stay blank in the snapshot. The Home
Depot mapping uses ticker `HD` as shown in its [SEC filing](https://www.sec.gov/Archives/edgar/data/354950/000162828026038247/hd-20260503.htm).

To generate issuer-entity and SEC ticker candidates for review, set
`SEC_USER_AGENT` to a project identifier containing a real contact email, then
run:

```powershell
$env:SEC_USER_AGENT = "Parallax Observatory research your.name@example.com"
uv run python -m src.resolve_lqd_tickers
```

The resolver queries GLEIF by one representative ISIN per issuer name, caches
those responses under `data/reference/`, and compares GLEIF legal names with
the SEC company ticker file. A unique exact legal-entity match is written to
`issuer_ticker_map.csv` as an automated exact match and applied to the snapshot.
If the issuer name has no exact ticker match, the resolver checks the GLEIF
direct parent and reports any parent ticker as a separate review candidate.
Ambiguous share classes and parent matches are not applied automatically.
This also lets a run resume from its GLEIF caches.

## Initial mapping pass

For the November 5, 2026 LQD snapshot, GLEIF matched 534 of 606 issuer names
to an LEI. The resolver found unique exact SEC ticker matches for 232 issuer
names, which are applied to 1,299 bonds; the manually reviewed Home Depot
mapping covers another 25 bonds. Direct-parent matches remain review
candidates and are not applied automatically.

The 606-row [ticker candidate report](issuer_ticker_candidates.csv) records
exact matches, parent candidates, ambiguous listings, and unresolved names.
The [issuer LEI report](issuer_lei_candidates.csv) records the first-stage
ISIN-to-entity matches. In bond terms, 1,324 of 3,219 rows now have an equity
ticker; parent and ambiguous candidates require review before assignment.

## Mapping desktop app

Install the project requirements, then start the Parallax launcher:

```powershell
uv pip install -r requirements.txt
uv run python -m src.launcher
```

The launcher provides buttons for reviewing mappings, inspecting current
holdings, refreshing issuer candidates, and building the bond snapshot. Its
lower panel reports the last successful holdings download, snapshot and map
version counts, and installed dependency versions. Use **Check latest
versions** to compare dependencies with PyPI. You can still start the mapping
editor directly with `uv run python -m src.mapping_app`.

The app edits `issuer_ticker_map.csv` only when **Save mappings** is pressed.
Each save also writes an immutable, timestamped copy under
`data/mapping_versions/YYYY-MM-DD/`. It keeps the bond ticker and issuer
equity ticker in separate fields. Automated parent candidates are shown as
suggestions and do not become the effective equity ticker until you enter a
manual override. Saving the map does not alter the published bond snapshot.
The mapping table includes a sortable ISIN count from the current snapshot and
opens with the highest-count issuers first. Build a fresh snapshot to refresh
those counts. **Final ticker** shows the automatic candidate after any manual
override. **Snapshot ticker** and its status show whether the current generated
CSV has that value; save the map and rebuild the snapshot to apply pending
changes. A coverage summary above the table counts mapped issuer identities
that already match the current snapshot. Use the snapshot ticker filter to show
only rows with a missing or present ticker in the current CSV.

To publish a bond file using the current downloaded LQD holdings and the
current map, run `uv run python -m src.build_lqd_snapshot`. The map history
preserves review decisions; the generated snapshot contains the holdings and
mapping results for that build.

The generated CSV is an internal static snapshot for the Parallax project.
Its source URL and `source_as_of_date` are preserved for traceability. Refresh
it deliberately when a newer snapshot is wanted; ordinary test runs
should consume the committed snapshot without making a network request.
