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
from .treasury_curves import NODE_FIELDS, map_trace_yields


POLICY_VERSION = "g-spread-linear-par-yield-v1"
OUTPUT_DIR = TRACE_DIR / "spread_snapshots"


def _latest_run_ids(database_path: Path) -> tuple[str, str]:
    connection = connect_database(database_path, initialize=True)
    try:
        trace = connection.execute(
            """SELECT run_id FROM history_runs
               WHERE status LIKE 'complete%'
               ORDER BY coalesce(completed_at_utc, updated_at_utc) DESC LIMIT 1"""
        ).fetchone()
        curve = connection.execute(
            """SELECT curve_run_id FROM treasury_curve_runs
               ORDER BY retrieved_at_utc DESC LIMIT 1"""
        ).fetchone()
        if not trace:
            raise ValueError("No completed TRACE run is available.")
        if not curve:
            raise ValueError("No Treasury curve vintage is stored. Download the curve first.")
        return trace[0], curve[0]
    finally:
        connection.close()


def _curve_rows(connection: duckdb.DuckDBPyConnection, curve_run_id: str) -> list[dict[str, object]]:
    records: dict[date, dict[str, object]] = {}
    rows = connection.execute(
        """SELECT observation_date, tenor_years, par_yield_pct
           FROM treasury_curve_observations WHERE curve_run_id=?
           ORDER BY observation_date, tenor_years""",
        [curve_run_id],
    ).fetchall()
    for observation_date, tenor, rate in rows:
        item = records.setdefault(observation_date, {"date": observation_date})
        item[f"yield_{float(tenor):g}y_pct"] = rate
    return list(records.values())


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
    read_connection = duckdb.connect()
    try:
        records = read_connection.execute(
            """SELECT isin, trade_date, maturity_date, clean_last_sale_yield,
                      yield_series_clean_action, observation_last_run_id, retrieval_id, record_hash
               FROM read_parquet(?) ORDER BY isin, trade_date""",
            [str(clean_path)],
        ).fetchall()
    finally:
        read_connection.close()
    print(f"Loaded {len(records):,} cleaned TRACE observations.", flush=True)

    connection = connect_database(database_path, initialize=True)
    try:
        trace_meta = connection.execute(
            """SELECT coalesce(completed_at_utc, updated_at_utc), snapshot_sha256,
                      start_date, end_date
               FROM history_runs WHERE run_id=?""",
            [trace_run_id],
        ).fetchone()
        curve_meta = connection.execute(
            """SELECT retrieved_at_utc, source_file_sha256, first_observation,
                      last_observation
               FROM treasury_curve_runs WHERE curve_run_id=?""",
            [curve_run_id],
        ).fetchone()
        if not trace_meta or not curve_meta:
            raise ValueError("The selected TRACE run or Treasury curve vintage does not exist.")
        curves = _curve_rows(connection, curve_run_id)
        trace_rows = [
            {
                "isin": row[0],
                "trade_date": row[1],
                "maturity_date": row[2],
                "yield_pct": row[3],
                "yield_reversal_flagged": row[4] == "reversal_spike_flagged",
                "trace_observation_run_id": row[5],
                "trace_retrieval_id": row[6],
                "trace_record_hash": row[7],
            }
            for row in records
        ]
        mapped = map_trace_yields(trace_rows, curves)
        for row in mapped:
            if row.get("yield_reversal_flagged"):
                row["g_spread_bps"] = None
        matched = sum(row.get("g_spread_bps") is not None for row in mapped)
        print(
            f"Mapped {len(mapped):,} observations to Treasury curves; "
            f"{matched:,} have valid G-spreads; "
            f"{sum(bool(row.get('yield_reversal_flagged')) for row in mapped):,} flagged reversals excluded.",
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
        spread_rows = []
        for row in mapped:
            digest_fields = (
                snapshot_id, row.get("isin"), row.get("trade_date"),
                row.get("maturity_date"), row.get("yield_pct"),
                row.get("treasury_par_yield_pct"), row.get("g_spread_bps"),
                row.get("trace_retrieval_id"), row.get("yield_reversal_flagged"),
                curve_run_id, POLICY_VERSION,
            )
            record_hash = hashlib.sha256(
                json.dumps(digest_fields, default=str, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            spread_rows.append((
                snapshot_id, row.get("isin"), row.get("trade_date"),
                row.get("maturity_date"), row.get("maturity_years"),
                row.get("yield_pct"), row.get("treasury_par_yield_pct"),
                row.get("g_spread_bps"), row.get("trace_observation_run_id"),
                row.get("trace_retrieval_id"), row.get("trace_record_hash"),
                curve_run_id, record_hash, bool(row.get("yield_reversal_flagged")),
            ))
        parquet_sql = str(parquet_path.resolve()).replace("'", "''")
        manifest = {
            "snapshot_id": snapshot_id,
            "generated_at_utc": generated_at,
            "trace_run_id": trace_run_id,
            "trace_as_of_utc": str(trace_meta[0]),
            "trace_snapshot_sha256": trace_meta[1],
            "trace_window": {"start": str(trace_meta[2]), "end": str(trace_meta[3])},
            "clean_preprocess_id": preprocess_report["preprocess_id"],
            "base_cleaning_policy_version": preprocess_report["policy_version"],
            "series_clean_id": series_clean_report["clean_id"],
            "cleaning_policy_version": series_clean_report["policy_version"],
            "yield_spike_threshold_bps": yield_spike_threshold_bps,
            "yield_reversal_spikes_flagged": series_clean_report["reversal_spikes_flagged"]["yield"],
            "yield_reversal_spikes_excluded_from_g_spread": sum(
                bool(row.get("yield_reversal_flagged")) for row in mapped
            ),
            "clean_parquet_path": str(clean_path.resolve()),
            "clean_parquet_sha256": clean_hash,
            "curve_run_id": curve_run_id,
            "curve_retrieved_at_utc": curve_meta[0],
            "curve_source_file_sha256": curve_meta[1],
            "curve_observation_window": {
                "start": str(curve_meta[2]), "end": str(curve_meta[3])
            },
            "interpolation_policy": POLICY_VERSION,
            "spread_definition": "(clean TRACE last-sale yield - interpolated Treasury par yield) * 100",
            "spread_unit": "basis points",
            "row_count": len(mapped),
            "matched_spread_count": matched,
            "unmatched_or_invalid_count": len(mapped) - matched,
            "parquet_path": str(parquet_path.resolve()),
        }

        connection.execute("BEGIN TRANSACTION")
        print("Writing immutable snapshot, Parquet output, and manifest...", flush=True)
        connection.executemany(
            """INSERT INTO bond_spread_observations (
                   snapshot_id, isin, trade_date, maturity_date, maturity_years,
                   trace_yield_pct, treasury_par_yield_pct, g_spread_bps,
                   trace_observation_run_id, trace_retrieval_id, trace_record_hash,
                   curve_run_id, record_hash, yield_reversal_flagged
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            spread_rows,
        )
        connection.execute(
            """INSERT INTO spread_snapshot_runs VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               )""",
            [
                snapshot_id, trace_run_id, str(trace_meta[0]), trace_meta[1],
                curve_run_id, curve_meta[0], curve_meta[1], str(clean_path.resolve()),
                clean_hash, POLICY_VERSION, generated_at, len(mapped), matched,
                str(parquet_path.resolve()), str(manifest_path.resolve()),
            ],
        )
        connection.execute(
            f"""COPY (
                SELECT * FROM bond_spread_observations
                WHERE snapshot_id='{snapshot_id}' ORDER BY isin, trade_date
            ) TO '{parquet_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
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
