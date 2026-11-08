"""Run the generic preprocessor against the latest FINRA TRACE history run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

import duckdb

from .data_preprocessing import reason_sql
from .trace_cleaning import TRACE_FIELD_POLICIES, TRACE_POLICY_VERSION, trace_value_sql
from .trace_history import DATABASE_PATH, TRACE_DIR


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _field_diagnostics(column: str) -> str:
    policy = TRACE_FIELD_POLICIES[column]
    reason = reason_sql(column, policy)
    reasons = (
        "valid", "missing", "non_numeric", "non_finite", "sentinel",
        "zero_is_missing", "below_minimum", "outside_absolute_limit",
    )
    return ",\n".join(
        f"count(*) FILTER (WHERE ({reason}) = '{label}') AS {column}_{label}"
        for label in reasons
    )


def _policy_report() -> dict[str, Any]:
    fields = {}
    for name, policy in TRACE_FIELD_POLICIES.items():
        fields[name] = {
            "clean_field": policy.clean_field,
            "unit": policy.unit,
            "zero_is_missing": policy.zero_is_missing,
            "sentinel_values": list(policy.sentinels),
            "minimum_valid_exclusive": policy.minimum_valid_exclusive,
            "maximum_absolute_exclusive": policy.maximum_absolute_exclusive,
        }
    return fields


def run_preprocessor(
    run_id: str | None = None,
    database_path: Path = DATABASE_PATH,
    output_root: Path = TRACE_DIR / "preprocessed",
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        if run_id is None:
            run = connection.execute(
                """SELECT run_id, status, start_date, end_date, total_bonds,
                          snapshot_path, snapshot_as_of_date, population_hash
                   FROM history_runs ORDER BY created_at_utc DESC LIMIT 1"""
            ).fetchone()
        else:
            run = connection.execute(
                """SELECT run_id, status, start_date, end_date, total_bonds,
                          snapshot_path, snapshot_as_of_date, population_hash
                   FROM history_runs WHERE run_id=?""",
                [run_id],
            ).fetchone()
        if not run:
            raise ValueError("No matching TRACE history run was found.")
        (
            run_id, run_status, start_date, end_date, total_bonds,
            snapshot_path, snapshot_as_of_date, population_hash,
        ) = run
        print(f"TRACE preprocessor {TRACE_POLICY_VERSION}", flush=True)
        print(f"Source run: {run_id} ({run_status})", flush=True)
        print(f"Input window: {start_date} to {end_date} | bonds in run: {total_bonds:,}", flush=True)
        print("Policies:", flush=True)
        for name, policy in TRACE_FIELD_POLICIES.items():
            print(
                f"  {name}: zero_is_missing={policy.zero_is_missing}; "
                f"sentinels={list(policy.sentinels)}; "
                f"minimum_valid_exclusive={policy.minimum_valid_exclusive}; "
                f"maximum_absolute_exclusive={policy.maximum_absolute_exclusive}",
                flush=True,
            )

        run_sql = _sql_literal(str(run_id))
        price_reason = reason_sql("price", TRACE_FIELD_POLICIES["price"])
        yield_reason = reason_sql("last_sale_yield", TRACE_FIELD_POLICIES["last_sale_yield"])
        print("Scanning raw field values...", flush=True)
        diagnostics = connection.execute(
            f"""SELECT count(*) AS input_rows, count(DISTINCT o.isin) AS securities_with_observations,
                       count(*) FILTER (WHERE ({price_reason}) <> 'valid'
                                           OR ({yield_reason}) <> 'valid') AS rows_with_exclusions,
                       {_field_diagnostics('price')},
                       {_field_diagnostics('last_sale_yield')}
                FROM observations o
                JOIN run_bonds rb ON rb.isin=o.isin AND rb.run_id={run_sql}
                WHERE o.trade_date BETWEEN ? AND ?""",
            [start_date, end_date],
        ).fetchone()
        names = [item[0] for item in connection.description]
        counts = dict(zip(names, diagnostics))
        input_rows = int(counts.pop("input_rows", 0))
        securities_with_observations = int(counts.pop("securities_with_observations", 0))
        excluded_rows = int(counts.pop("rows_with_exclusions", 0))
        print(f"Rows scanned: {input_rows:,}", flush=True)
        print(f"Rows with one or more field exclusions: {excluded_rows:,}", flush=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        preprocess_id = f"{stamp}_{uuid4().hex[:8]}"
        output_dir = output_root / f"run_id={run_id}" / f"preprocess_id={preprocess_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = output_dir / "observations.parquet"
        report_path = output_dir / "diagnostics.json"
        parquet_sql = _sql_literal(parquet_path.resolve().as_posix())
        snapshot_sql = _sql_literal(Path(str(snapshot_path)).resolve().as_posix())
        clean_price = trace_value_sql("price", "price")
        clean_yield = trace_value_sql("last_sale_yield", "last_sale_yield")
        price_status = reason_sql("price", TRACE_FIELD_POLICIES["price"])
        yield_status = reason_sql("last_sale_yield", TRACE_FIELD_POLICIES["last_sale_yield"])
        print(f"Writing normalized clean output to {parquet_path}", flush=True)
        connection.execute(
            f"""COPY (
                    SELECT o.isin, rb.issuer_name,
                           s.sector, s.maturity_year, s.equity_ticker, s.bond_ticker,
                           o.trade_date, o.cusip, o.symbol,
                           o.finra_security_id, o.product_type,
                           o.price AS price_raw, {clean_price} AS clean_price,
                           ({price_status}) AS price_clean_status,
                           o.last_sale_yield AS last_sale_yield_raw,
                           {clean_yield} AS clean_last_sale_yield,
                           ({yield_status}) AS yield_clean_status,
                           o.retrieved_at_utc, o.last_run_id AS observation_last_run_id,
                           rb.run_id AS population_run_id
                    FROM observations o
                    JOIN run_bonds rb ON rb.isin=o.isin AND rb.run_id={run_sql}
                    LEFT JOIN read_csv_auto('{snapshot_sql}', header=true, all_varchar=true) s
                           ON s.isin=o.isin
                    WHERE o.trade_date BETWEEN ? AND ?
                    ORDER BY o.isin, o.trade_date
                ) TO {parquet_sql} (FORMAT PARQUET, COMPRESSION ZSTD)""",
            [start_date, end_date],
        )
        completed_at = datetime.now(timezone.utc)

        fields = {}
        for name in TRACE_FIELD_POLICIES:
            fields[name] = {
                "clean_field": TRACE_FIELD_POLICIES[name].clean_field,
                "counts": {
                    reason: int(counts.get(f"{name}_{reason}", 0) or 0)
                    for reason in (
                        "valid", "missing", "non_numeric", "non_finite", "sentinel",
                        "zero_is_missing", "below_minimum", "outside_absolute_limit",
                    )
                },
            }
        report = {
            "preprocess_id": preprocess_id,
            "started_at_utc": started_at.isoformat(timespec="seconds"),
            "completed_at_utc": completed_at.isoformat(timespec="seconds"),
            "source": "FINRA TRACE via OpenBB normalized observations",
            "source_run_id": str(run_id),
            "source_run_status": run_status,
            "window": {"start": str(start_date), "end": str(end_date)},
            "population_count": int(total_bonds),
            "securities_with_observations": securities_with_observations,
            "population_snapshot_path": snapshot_path,
            "population_snapshot_as_of_date": snapshot_as_of_date,
            "population_hash": population_hash,
            "input_rows": input_rows,
            "clean_parquet_rows": input_rows,
            "rows_with_one_or_more_field_exclusions": excluded_rows,
            "policy_version": TRACE_POLICY_VERSION,
            "policies": _policy_report(),
            "fields": fields,
            "raw_database_values_modified": False,
            "clean_parquet": str(parquet_path.resolve()),
            "clean_parquet_bytes": parquet_path.stat().st_size,
            "diagnostics_report": str(report_path.resolve()),
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Field results:", flush=True)
        for name, result in fields.items():
            field_counts = result["counts"]
            print(
                f"  {name}: valid={field_counts['valid']:,}, "
                f"missing={field_counts['missing']:,}, zero={field_counts['zero_is_missing']:,}, "
                f"sentinel={field_counts['sentinel']:,}, "
                f"outside_limit={field_counts['outside_absolute_limit']:,}, "
                f"non_numeric={field_counts['non_numeric']:,}, "
                f"non_finite={field_counts['non_finite']:,}",
                flush=True,
            )
        print(f"Clean Parquet: {parquet_path}", flush=True)
        print(f"Diagnostics JSON: {report_path}", flush=True)
        print("Raw DuckDB observations were not modified.", flush=True)
        return report
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="History run id; defaults to the latest run.")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--output-root", type=Path, default=TRACE_DIR / "preprocessed")
    args = parser.parse_args()
    try:
        run_preprocessor(args.run_id, args.database, args.output_root)
    except Exception as error:
        print(f"Preprocessing failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
