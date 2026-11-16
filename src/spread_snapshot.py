"""Build an immutable maturity-matched Treasury G-spread snapshot."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

import duckdb

from .trace_history import DATABASE_PATH, TRACE_DIR, connect_database
from .trace_cleaner import clean_preprocessed_trace
from .trace_preprocessor import run_preprocessor


POLICY_VERSION = "g-spread-linear-par-yield-v2"
OUTPUT_DIR = TRACE_DIR / "spread_snapshots"
MIN_CURVE_NODES_PER_DATE = 10


def _latest_run_ids(database_path: Path) -> tuple[str, str]:
    connection = connect_database(database_path, initialize=True)
    try:
        trace = connection.execute(
            """SELECT run_id, status FROM history_runs
               WHERE status LIKE 'complete%'
               ORDER BY coalesce(completed_at_utc, updated_at_utc) DESC LIMIT 1"""
        ).fetchone()
        if not trace:
            trace = connection.execute(
                """SELECT run_id, status FROM history_runs
                   WHERE status = 'partial'
                     AND EXISTS (
                         SELECT 1 FROM run_bonds
                         WHERE run_bonds.run_id = history_runs.run_id
                           AND run_bonds.status IN ('downloaded', 'review', 'needs_review')
                     )
                   ORDER BY coalesce(completed_at_utc, updated_at_utc) DESC LIMIT 1"""
            ).fetchone()
        curve = connection.execute(
            """SELECT r.curve_run_id
               FROM treasury_curve_runs r
               LEFT JOIN (
                   SELECT curve_run_id, count(DISTINCT observation_date) AS date_count
                   FROM treasury_curve_observations
                   GROUP BY curve_run_id
               ) o USING (curve_run_id)
               ORDER BY coalesce(o.date_count, 0) DESC,
                        r.last_observation DESC, r.retrieved_at_utc DESC
               LIMIT 1"""
        ).fetchone()
        if not trace:
            raise ValueError(
                "No completed or usable partial TRACE run is available. "
                "Download TRACE history first."
            )
        if not curve:
            raise ValueError("No Treasury curve vintage is stored. Download the curve first.")
        return trace[0], curve[0]
    finally:
        connection.close()


def build_spread_snapshot(
    trace_run_id: str | None = None,
    curve_run_id: str | None = None,
    database_path: Path = DATABASE_PATH,
    output_root: Path = OUTPUT_DIR,
    yield_spike_threshold_bps: float = 50.0,
) -> dict[str, object]:
    """Persist spread rows and a manifest bound to exact TRACE and curve vintages."""
    if yield_spike_threshold_bps <= 0:
        raise ValueError("Yield reversal threshold must be positive.")
    use_all_curve_vintages = curve_run_id is None
    if trace_run_id is None or curve_run_id is None:
        latest_trace, latest_curve = _latest_run_ids(database_path)
        trace_run_id = trace_run_id or latest_trace
        curve_run_id = curve_run_id or latest_curve

    print("Preparing cleaned TRACE history for the selected run...", flush=True)
    preprocess_report = run_preprocessor(trace_run_id, database_path)
    preprocessed_path = Path(preprocess_report["clean_parquet"])
    print(
        f"Applying reversal checks with a {yield_spike_threshold_bps:g} bp minimum yield move...",
        flush=True,
    )
    series_clean_report = clean_preprocessed_trace(
        preprocessed_path, yield_spike_threshold_bps=yield_spike_threshold_bps
    )
    clean_path = Path(series_clean_report["output_cleaned_parquet"])
    clean_hash = hashlib.sha256(clean_path.read_bytes()).hexdigest()

    connection = connect_database(database_path, initialize=True)
    try:
        trace_meta = connection.execute(
            """SELECT coalesce(completed_at_utc, updated_at_utc), snapshot_sha256,
                      start_date, end_date, status
               FROM history_runs WHERE run_id=?""",
            [trace_run_id],
        ).fetchone()
        curve_meta = connection.execute(
            """SELECT r.retrieved_at_utc, r.source_file_sha256,
                      min(o.observation_date), max(o.observation_date)
               FROM treasury_curve_runs r
               LEFT JOIN treasury_curve_observations o USING (curve_run_id)
               WHERE r.curve_run_id=?
               GROUP BY r.curve_run_id, r.retrieved_at_utc, r.source_file_sha256""",
            [curve_run_id],
        ).fetchone()
        if not trace_meta or not curve_meta:
            raise ValueError("The selected TRACE run or Treasury curve vintage does not exist.")
        if trace_meta[4] == "partial":
            failed, unfinished = connection.execute(
                """SELECT count(*) FILTER (WHERE status = 'failed'),
                          count(*) FILTER (WHERE status IN ('pending', 'downloading'))
                   FROM run_bonds WHERE run_id=?""",
                [trace_run_id],
            ).fetchone()
            print(
                f"Using partial TRACE run {trace_run_id}: {failed} failed and "
                f"{unfinished} unfinished bonds; available history will be included.",
                flush=True,
            )
        clean_path_sql = str(clean_path.resolve()).replace("'", "''")
        clean_stats = connection.execute(
            f"""SELECT min(trade_date), max(trade_date), count(*)
                FROM read_parquet('{clean_path_sql}')"""
        ).fetchone()
        if not clean_stats or clean_stats[0] is None:
            raise ValueError("The cleaned TRACE dataset contains no observations.")
        first_clean_date, last_clean_date, row_count = clean_stats
        print(f"Loaded {row_count:,} cleaned TRACE observations.", flush=True)
        if use_all_curve_vintages:
            print(
                "Using all stored Treasury vintages; for each observation date, "
                f"choosing the latest vintage with at least {MIN_CURVE_NODES_PER_DATE} "
                "non-null tenor nodes.",
                flush=True,
            )
        else:
            print(
                f"Using selected Treasury vintage {curve_run_id} covering "
                f"{curve_meta[2]} through {curve_meta[3]}.",
                flush=True,
            )
        snapshot_id = str(uuid.uuid4())
        generated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        output_dir = output_root / f"snapshot_id={snapshot_id}"
        output_dir.mkdir(parents=True, exist_ok=False)
        parquet_path = output_dir / "bond_spreads.parquet"
        manifest_path = output_dir / "manifest.json"

        connection.execute(
            """CREATE TABLE IF NOT EXISTS spread_snapshot_runs (
                   snapshot_id VARCHAR PRIMARY KEY,
                   trace_run_id VARCHAR NOT NULL,
                   trace_as_of_utc VARCHAR NOT NULL,
                   trace_snapshot_sha256 VARCHAR,
                   curve_run_id VARCHAR NOT NULL,
                   curve_retrieved_at_utc VARCHAR NOT NULL,
                   curve_source_file_sha256 VARCHAR,
                   clean_parquet_path VARCHAR NOT NULL,
                   clean_parquet_sha256 VARCHAR NOT NULL,
                   policy_version VARCHAR NOT NULL,
                   generated_at_utc VARCHAR NOT NULL,
                   row_count INTEGER NOT NULL,
                   matched_spread_count INTEGER NOT NULL,
                   parquet_path VARCHAR NOT NULL,
                   manifest_path VARCHAR NOT NULL
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS bond_spread_observations (
                   snapshot_id VARCHAR NOT NULL,
                   isin VARCHAR NOT NULL,
                   trade_date DATE NOT NULL,
                   maturity_date DATE,
                   maturity_years DOUBLE,
                   trace_yield_pct DOUBLE,
                   treasury_par_yield_pct DOUBLE,
                   g_spread_bps DOUBLE,
                   trace_observation_run_id VARCHAR,
                   trace_retrieval_id VARCHAR,
                   trace_record_hash VARCHAR,
                   curve_run_id VARCHAR NOT NULL,
                   record_hash VARCHAR NOT NULL,
                   yield_reversal_flagged BOOLEAN NOT NULL DEFAULT FALSE,
                   PRIMARY KEY (snapshot_id, isin, trade_date)
               )"""
        )
        connection.execute(
            "ALTER TABLE bond_spread_observations "
            "ADD COLUMN IF NOT EXISTS yield_reversal_flagged BOOLEAN DEFAULT FALSE"
        )
        snapshot_id_sql = snapshot_id.replace("'", "''")
        curve_run_id_sql = str(curve_run_id).replace("'", "''")
        curve_vintage_filter = (
            "" if use_all_curve_vintages
            else f"AND o.curve_run_id = '{curve_run_id_sql}'"
        )
        connection.execute("BEGIN TRANSACTION")
        years = list(range(first_clean_date.year, last_clean_date.year + 1))
        print(f"Calculating and inserting spreads by year ({len(years)} years)...", flush=True)
        for year_index, year in enumerate(years, start=1):
            year_start = f"{year}-01-01"
            year_end = f"{year + 1}-01-01"
            mapping_query = f"""WITH curve_vintage_candidates AS (
                    SELECT o.observation_date, o.curve_run_id,
                           max(r.retrieved_at_utc) AS retrieved_at_utc,
                           count(DISTINCT o.tenor_years) AS node_count
                    FROM treasury_curve_observations o
                    JOIN treasury_curve_runs r USING (curve_run_id)
                    WHERE o.par_yield_pct IS NOT NULL
                      AND o.observation_date >= DATE '{year_start}'
                      AND o.observation_date < DATE '{year_end}'
                      {curve_vintage_filter}
                    GROUP BY o.observation_date, o.curve_run_id
                ), curve_vintage_by_date AS (
                    SELECT observation_date, curve_run_id
                    FROM (
                        SELECT *, row_number() OVER (
                            PARTITION BY observation_date ORDER BY retrieved_at_utc DESC
                        ) AS vintage_rank
                        FROM curve_vintage_candidates
                        WHERE node_count >= {MIN_CURVE_NODES_PER_DATE}
                    ) WHERE vintage_rank=1
                ), trace AS (
                    SELECT isin, trade_date,
                           try_cast(maturity_date AS DATE) AS maturity_date,
                           (date_diff('day', trade_date, try_cast(maturity_date AS DATE)) / 365.25)::DOUBLE AS maturity_years,
                           clean_last_sale_yield AS trace_yield_pct,
                           observation_last_run_id AS trace_observation_run_id,
                           retrieval_id AS trace_retrieval_id,
                           record_hash AS trace_record_hash,
                           yield_series_clean_action = 'reversal_spike_flagged' AS yield_reversal_flagged
                    FROM read_parquet('{clean_path_sql}')
                    WHERE trade_date >= DATE '{year_start}' AND trade_date < DATE '{year_end}'
                ), nodes AS (
                    SELECT trace.*,
                           vintage.curve_run_id AS curve_run_id,
                           lower_node.tenor_years AS lower_tenor,
                           lower_node.par_yield_pct AS lower_yield,
                           upper_node.tenor_years AS upper_tenor,
                           upper_node.par_yield_pct AS upper_yield
                    FROM trace
                    LEFT JOIN curve_vintage_by_date vintage
                      ON vintage.observation_date = trace.trade_date
                    LEFT JOIN LATERAL (
                        SELECT tenor_years, par_yield_pct
                        FROM treasury_curve_observations curve_node
                        WHERE curve_node.curve_run_id = vintage.curve_run_id
                          AND curve_node.observation_date = trace.trade_date
                          AND curve_node.par_yield_pct IS NOT NULL
                          AND curve_node.tenor_years <= trace.maturity_years
                        ORDER BY curve_node.tenor_years DESC LIMIT 1
                    ) lower_node ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT tenor_years, par_yield_pct
                        FROM treasury_curve_observations curve_node
                        WHERE curve_node.curve_run_id = vintage.curve_run_id
                          AND curve_node.observation_date = trace.trade_date
                          AND curve_node.par_yield_pct IS NOT NULL
                          AND curve_node.tenor_years >= trace.maturity_years
                        ORDER BY curve_node.tenor_years LIMIT 1
                    ) upper_node ON TRUE
                ), interpolated AS (
                    SELECT *,
                           CASE
                             WHEN lower_tenor IS NULL OR upper_tenor IS NULL THEN NULL
                             WHEN lower_tenor = upper_tenor THEN lower_yield
                             ELSE lower_yield +
                                  (maturity_years - lower_tenor) /
                                  (upper_tenor - lower_tenor) * (upper_yield - lower_yield)
                           END AS treasury_par_yield_pct
                    FROM nodes
                ), calculated AS (
                    SELECT *,
                           CASE WHEN yield_reversal_flagged OR trace_yield_pct IS NULL
                                          OR treasury_par_yield_pct IS NULL THEN NULL
                                ELSE (trace_yield_pct - treasury_par_yield_pct) * 100 END AS g_spread_bps
                    FROM interpolated
                )
                SELECT '{snapshot_id_sql}' AS snapshot_id,
                       isin, trade_date, maturity_date, maturity_years,
                       trace_yield_pct, treasury_par_yield_pct,
                       g_spread_bps,
                       trace_observation_run_id, trace_retrieval_id, trace_record_hash,
                       coalesce(curve_run_id, '{curve_run_id_sql}') AS curve_run_id,
                       sha256(concat_ws('|', '{snapshot_id_sql}', isin,
                           cast(trade_date AS VARCHAR), coalesce(cast(maturity_date AS VARCHAR), ''),
                           coalesce(cast(trace_yield_pct AS VARCHAR), ''),
                           coalesce(cast(treasury_par_yield_pct AS VARCHAR), ''),
                           coalesce(cast(g_spread_bps AS VARCHAR), ''),
                           coalesce(trace_retrieval_id, ''), cast(yield_reversal_flagged AS VARCHAR),
                           coalesce(curve_run_id, '{curve_run_id_sql}'), '{POLICY_VERSION}')) AS record_hash,
                       coalesce(yield_reversal_flagged, FALSE) AS yield_reversal_flagged
                FROM calculated"""
            connection.execute(
                """INSERT INTO bond_spread_observations (
                       snapshot_id, isin, trade_date, maturity_date, maturity_years,
                       trace_yield_pct, treasury_par_yield_pct, g_spread_bps,
                       trace_observation_run_id, trace_retrieval_id, trace_record_hash,
                       curve_run_id, record_hash, yield_reversal_flagged
                   ) """ + mapping_query
            )
            year_stats = connection.execute(
                """SELECT count(*), count(*) FILTER (WHERE g_spread_bps IS NOT NULL),
                          count(*) FILTER (WHERE yield_reversal_flagged)
                   FROM bond_spread_observations
                   WHERE snapshot_id=? AND trade_date >= ? AND trade_date < ?""",
                [snapshot_id, date(year, 1, 1), date(year + 1, 1, 1)],
            ).fetchone()
            print(
                f"Completed year {year} ({year_index}/{len(years)}): "
                f"{year_stats[0]:,} rows, {year_stats[1]:,} matched spreads, "
                f"{year_stats[2]:,} reversal flags excluded.",
                flush=True,
            )
        counts = connection.execute(
            """SELECT count(*), count(*) FILTER (WHERE g_spread_bps IS NOT NULL),
                      count(*) FILTER (WHERE yield_reversal_flagged)
               FROM bond_spread_observations WHERE snapshot_id=?""",
            [snapshot_id],
        ).fetchone()
        row_count, matched, flagged_count = (int(value or 0) for value in counts)
        curve_vintage_rows = connection.execute(
            """SELECT r.curve_run_id, r.retrieved_at_utc, r.source_file_sha256,
                      min(o.observation_date), max(o.observation_date),
                      count(DISTINCT o.observation_date)
               FROM treasury_curve_runs r
               JOIN treasury_curve_observations o USING (curve_run_id)
               WHERE r.curve_run_id IN (
                   SELECT DISTINCT curve_run_id FROM bond_spread_observations
                   WHERE snapshot_id=? AND treasury_par_yield_pct IS NOT NULL
               )
               GROUP BY r.curve_run_id, r.retrieved_at_utc, r.source_file_sha256
               ORDER BY min(o.observation_date)""",
            [snapshot_id],
        ).fetchall()
        curve_vintages = [
            {
                "curve_run_id": row[0],
                "retrieved_at_utc": row[1],
                "source_file_sha256": row[2],
                "first_observation": str(row[3]),
                "last_observation": str(row[4]),
                "observation_date_count": int(row[5]),
            }
            for row in curve_vintage_rows
        ]
        print(
            f"Applied {len(curve_vintages)} Treasury curve vintages across "
            f"{curve_vintages[0]['first_observation'] if curve_vintages else 'no dates'} "
            f"to {curve_vintages[-1]['last_observation'] if curve_vintages else 'no dates'}.",
            flush=True,
        )
        parquet_sql = str(parquet_path.resolve()).replace("'", "''")
        manifest = {
            "snapshot_id": snapshot_id,
            "generated_at_utc": generated_at,
            "trace_run_id": trace_run_id,
            "trace_run_status": trace_meta[4],
            "trace_as_of_utc": str(trace_meta[0]),
            "trace_snapshot_sha256": trace_meta[1],
            "trace_window": {"start": str(trace_meta[2]), "end": str(trace_meta[3])},
            "clean_preprocess_id": preprocess_report["preprocess_id"],
            "base_cleaning_policy_version": preprocess_report["policy_version"],
            "series_clean_id": series_clean_report["clean_id"],
            "cleaning_policy_version": series_clean_report["policy_version"],
            "yield_spike_threshold_bps": yield_spike_threshold_bps,
            "yield_reversal_spikes_flagged": series_clean_report["reversal_spikes_flagged"]["yield"],
            "yield_reversal_spikes_excluded_from_g_spread": flagged_count,
            "clean_parquet_path": str(clean_path.resolve()),
            "clean_parquet_sha256": clean_hash,
            "curve_run_id": curve_run_id,
            "representative_curve_run_id": curve_run_id,
            "curve_vintages": curve_vintages,
            "curve_vintage_selection": (
                "latest retrieved curve vintage per date with at least 10 non-null par-yield tenors"
                if use_all_curve_vintages else "explicitly selected curve vintage"
            ),
            "curve_minimum_valid_tenor_nodes": MIN_CURVE_NODES_PER_DATE,
            "curve_retrieved_at_utc": curve_meta[0],
            "curve_source_file_sha256": curve_meta[1],
            "curve_observation_window": {
                "start": min((row["first_observation"] for row in curve_vintages), default=None),
                "end": max((row["last_observation"] for row in curve_vintages), default=None),
            },
            "interpolation_policy": POLICY_VERSION,
            "spread_definition": "(clean TRACE last-sale yield - interpolated Treasury par yield) * 100",
            "spread_unit": "basis points",
            "row_count": row_count,
            "matched_spread_count": matched,
            "unmatched_or_invalid_count": row_count - matched,
            "parquet_path": str(parquet_path.resolve()),
        }

        connection.execute(
            """INSERT INTO spread_snapshot_runs VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               )""",
            [
                snapshot_id, trace_run_id, str(trace_meta[0]), trace_meta[1],
                curve_run_id, curve_meta[0], curve_meta[1], str(clean_path.resolve()),
                clean_hash, POLICY_VERSION, generated_at, row_count, matched,
                str(parquet_path.resolve()), str(manifest_path.resolve()),
            ],
        )
        print("Exporting spread rows to Parquet...", flush=True)
        connection.execute(
            f"""COPY (
                SELECT * FROM bond_spread_observations
                WHERE snapshot_id='{snapshot_id}' ORDER BY isin, trade_date
            ) TO '{parquet_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
        print("Parquet export complete; writing manifest...", flush=True)
        manifest["parquet_sha256"] = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        connection.commit()
        return manifest
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-run-id")
    parser.add_argument("--curve-run-id")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--yield-spike-threshold-bps", type=float, default=50.0)
    args = parser.parse_args()
    try:
        result = build_spread_snapshot(
            args.trace_run_id, args.curve_run_id, args.database,
            yield_spike_threshold_bps=args.yield_spike_threshold_bps,
        )
        print(f"Built spread snapshot {result['snapshot_id']}")
        print(
            f"Rows: {result['row_count']:,}; matched spreads: "
            f"{result['matched_spread_count']:,}"
        )
        print(
            "Yield reversal threshold: "
            f"{result['yield_spike_threshold_bps']:g} bp; flagged and excluded: "
            f"{result['yield_reversal_spikes_excluded_from_g_spread']:,}"
        )
        print(f"Manifest: {result['parquet_path'].replace('bond_spreads.parquet', 'manifest.json')}")
    except Exception as error:
        print(f"Spread snapshot failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
