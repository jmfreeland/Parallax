"""Resumable FINRA TRACE history download and local DuckDB storage."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import shutil
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import polars as pl

from .trace_cleaning import (
    TRACE_FIELD_POLICIES,
    clean_trace_value,
)
from .data_preprocessing import classify_value


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "data" / "snapshots" / "lqd_bonds.csv"
TRACE_DIR = ROOT / "data" / "trace"
DATABASE_PATH = TRACE_DIR / "history.duckdb"
DEFAULT_BATCH_SIZE = 20
LOG_PATH = TRACE_DIR / "downloader.log"
logger = logging.getLogger("parallax.trace_history")
_openbb: Any | None = None

JobCallback = Callable[[dict[str, Any]], None]
ProgressCallback = Callable[[int, int], None]
BatchCallback = Callable[[int], None]


def configure_logging() -> Path:
    """Write durable, bounded diagnostics for the downloader."""
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    resolved = LOG_PATH.resolve()
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == resolved
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            resolved, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return resolved


def initialize_provider() -> None:
    """Import OpenBB on the main thread before any worker starts."""
    global _openbb
    if _openbb is not None:
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Initialize the OpenBB FINRA provider on the main thread first.")
    logger.info("Initializing OpenBB FINRA provider on the main thread")
    from openbb import obb

    _openbb = obb
    logger.info("OpenBB FINRA provider initialized")


def _provider_client() -> Any:
    if _openbb is None:
        raise RuntimeError("OpenBB provider is not initialized; start the run from the GUI.")
    return _openbb


def connect_database(path: Path = DATABASE_PATH, initialize: bool = True) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    if initialize:
        _initialize_database(connection)
    return connection


def _initialize_database(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS history_runs (
            run_id VARCHAR PRIMARY KEY,
            created_at_utc VARCHAR NOT NULL,
            updated_at_utc VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            snapshot_path VARCHAR NOT NULL,
            snapshot_sha256 VARCHAR,
            snapshot_as_of_date VARCHAR NOT NULL,
            population_hash VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            total_bonds INTEGER NOT NULL,
            parquet_path VARCHAR,
            completed_at_utc VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_bonds (
            run_id VARCHAR NOT NULL,
            isin VARCHAR NOT NULL,
            issuer_name VARCHAR NOT NULL,
            cusip VARCHAR NOT NULL,
            symbol VARCHAR,
            finra_security_id VARCHAR,
            status VARCHAR NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            observation_count INTEGER NOT NULL DEFAULT 0,
            first_date DATE,
            last_date DATE,
            quality_flags VARCHAR NOT NULL DEFAULT '[]',
            last_error VARCHAR,
            updated_at_utc VARCHAR NOT NULL,
            PRIMARY KEY (run_id, isin)
        )
        """
    )
    _ensure_column(connection, "history_runs", "snapshot_sha256", "VARCHAR")
    _ensure_column(connection, "history_runs", "completed_at_utc", "VARCHAR")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            isin VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            cusip VARCHAR NOT NULL,
            symbol VARCHAR,
            finra_security_id VARCHAR,
            product_type VARCHAR,
            price DOUBLE,
            last_sale_yield DOUBLE,
            retrieved_at_utc VARCHAR NOT NULL,
            last_run_id VARCHAR NOT NULL,
            PRIMARY KEY (isin, trade_date)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_augmentation_runs (
            augmentation_id VARCHAR PRIMARY KEY,
            source_run_id VARCHAR NOT NULL,
            started_at_utc VARCHAR NOT NULL,
            completed_at_utc VARCHAR,
            status VARCHAR NOT NULL,
            requested_bonds INTEGER NOT NULL,
            processed_bonds INTEGER NOT NULL DEFAULT 0,
            response_rows INTEGER NOT NULL DEFAULT 0,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            fields_filled INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_augmentation_fields (
            augmentation_id VARCHAR NOT NULL,
            source_run_id VARCHAR NOT NULL,
            isin VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            field_name VARCHAR NOT NULL,
            previous_value DOUBLE,
            previous_reason VARCHAR NOT NULL,
            augmented_value DOUBLE NOT NULL,
            retrieved_at_utc VARCHAR NOT NULL,
            provider_cusip VARCHAR NOT NULL,
            PRIMARY KEY (augmentation_id, isin, trade_date, field_name)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trace_observation_versions (
            retrieval_id VARCHAR NOT NULL,
            isin VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            cusip VARCHAR NOT NULL,
            symbol VARCHAR,
            finra_security_id VARCHAR,
            product_type VARCHAR,
            price DOUBLE,
            last_sale_yield DOUBLE,
            retrieved_at_utc VARCHAR NOT NULL,
            retrieval_run_id VARCHAR NOT NULL,
            record_hash VARCHAR NOT NULL,
            PRIMARY KEY (retrieval_id, isin, trade_date)
        )
        """
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               migration_id VARCHAR PRIMARY KEY,
               applied_at_utc VARCHAR NOT NULL
           )"""
    )
    migrated = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id='legacy_observations_v1'"
    ).fetchone()
    if not migrated:
        # Preserve the current legacy state as one initial vintage before new
        # collection runs start writing append-only observations.
        connection.execute(
            """
            INSERT INTO trace_observation_versions
            SELECT 'legacy-' || o.last_run_id || '-' || o.isin || '-' || CAST(o.trade_date AS VARCHAR),
                   o.isin, o.trade_date, o.cusip, o.symbol, o.finra_security_id,
                   o.product_type, o.price, o.last_sale_yield, o.retrieved_at_utc,
                   o.last_run_id, sha256(concat_ws('|', o.isin, CAST(o.trade_date AS VARCHAR),
                        o.cusip, coalesce(o.symbol, ''), coalesce(o.finra_security_id, ''),
                        coalesce(o.product_type, ''), coalesce(CAST(o.price AS VARCHAR), ''),
                        coalesce(CAST(o.last_sale_yield AS VARCHAR), '')))
            FROM observations o
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('legacy_observations_v1', ?)", [_now()]
        )


def _ensure_column(
    connection: duckdb.DuckDBPyConnection, table: str, column: str, data_type: str
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _archive_snapshot(snapshot_path: Path) -> tuple[Path, str]:
    payload = snapshot_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    archive = TRACE_DIR / "snapshots" / f"{digest}.csv"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        archive.write_bytes(payload)
    return archive.resolve(), digest


def _cusip_from_isin(isin: str) -> str:
    normalized = isin.strip().upper()
    if len(normalized) == 12 and normalized.startswith("US"):
        return normalized[2:11]
    return ""


def _read_population(snapshot_path: Path) -> tuple[list[dict[str, str]], str, str]:
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Bond snapshot not found: {snapshot_path}")
    with snapshot_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "isin" not in reader.fieldnames:
            raise ValueError("Bond snapshot needs an 'isin' column.")
        rows = [
            {
                "isin": (row.get("isin") or "").strip().upper(),
                "issuer_name": (row.get("issuer_name") or "").strip(),
            }
            for row in reader
            if (row.get("isin") or "").strip()
        ]
        snapshot_date = ""
        if "source_as_of_date" in reader.fieldnames:
            handle.seek(0)
            snapshot_date = next(csv.DictReader(handle), {}).get("source_as_of_date", "")
    if not rows:
        raise ValueError("Bond snapshot contains no ISIN rows.")
    digest_source = "\n".join(f"{row['isin']}|{row['issuer_name']}" for row in sorted(rows, key=lambda item: item["isin"]))
    return rows, snapshot_date, hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


def prepare_run(
    snapshot_path: Path,
    start_date: date,
    end_date: date,
    resume: bool,
    database_path: Path = DATABASE_PATH,
) -> tuple[str, bool]:
    """Find a compatible incomplete run or create a new run and per-bond jobs."""
    if start_date > end_date:
        raise ValueError("Start date must not be after end date.")
    bonds, snapshot_date, population_hash = _read_population(snapshot_path)
    archived_snapshot, snapshot_sha256 = _archive_snapshot(snapshot_path)
    logger.info(
        "Prepare run: snapshot=%s bonds=%d range=%s..%s resume=%s population=%s",
        snapshot_path, len(bonds), start_date, end_date, resume, population_hash[:12],
    )
    connection = connect_database(database_path)
    try:
        if resume:
            existing = connection.execute(
                """
                SELECT run_id FROM history_runs
                WHERE population_hash = ? AND snapshot_sha256 = ?
                    AND start_date = ? AND end_date = ?
                    AND status IN ('pending', 'running', 'partial', 'failed')
                ORDER BY created_at_utc DESC LIMIT 1
                """,
                [population_hash, snapshot_sha256, start_date, end_date],
            ).fetchone()
            if existing:
                run_id = existing[0]
                connection.execute(
                    """
                    UPDATE run_bonds SET status = 'pending', updated_at_utc = ?
                    WHERE run_id = ? AND status = 'downloading'
                    """,
                    [_now(), run_id],
                )
                connection.execute(
                    "UPDATE history_runs SET status = 'pending', updated_at_utc = ?, completed_at_utc = NULL WHERE run_id = ?",
                    [_now(), run_id],
                )
                return run_id, True

        run_id = str(uuid.uuid4())
        created = _now()
        connection.execute(
            """
            INSERT INTO history_runs (
                run_id, created_at_utc, updated_at_utc, status, snapshot_path,
                snapshot_sha256, snapshot_as_of_date, population_hash,
                start_date, end_date, total_bonds, parquet_path, completed_at_utc
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                run_id,
                created,
                created,
                str(archived_snapshot),
                snapshot_sha256,
                snapshot_date,
                population_hash,
                start_date,
                end_date,
                len(bonds),
            ],
        )
        jobs = []
        for bond in bonds:
            cusip = _cusip_from_isin(bond["isin"])
            status = "pending" if cusip else "needs_review"
            error = None if cusip else "Could not derive a U.S. CUSIP from this ISIN. Edit the CUSIP before retrying."
            jobs.append(
                (
                    run_id,
                    bond["isin"],
                    bond["issuer_name"],
                    cusip,
                    None,
                    None,
                    status,
                    0,
                    0,
                    None,
                    None,
                    "[]",
                    error,
                    created,
                )
            )
        connection.executemany(
            "INSERT INTO run_bonds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            jobs,
        )
        return run_id, False
    finally:
        connection.close()


def incremental_date_window(
    database_path: Path = DATABASE_PATH,
    end_date: date | None = None,
    overlap_days: int = 10,
    initial_years: int = 5,
) -> tuple[date, date]:
    """Return a current update window with a recent correction overlap."""
    end_date = end_date or date.today()
    if overlap_days < 0 or initial_years <= 0:
        raise ValueError("overlap_days must be nonnegative and initial_years positive.")
    connection = connect_database(database_path)
    try:
        latest = connection.execute(
            "SELECT max(trade_date) FROM trace_observation_versions"
        ).fetchone()[0]
        start_date = (
            latest - timedelta(days=overlap_days)
            if latest
            else end_date - timedelta(days=365 * initial_years)
        )
        return start_date, end_date
    finally:
        connection.close()


def prepare_incremental_run(
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    database_path: Path = DATABASE_PATH,
    end_date: date | None = None,
    overlap_days: int = 10,
) -> tuple[str, date, date]:
    """Create a new TRACE update run from the latest date plus overlap to today."""
    start_date, end_date = incremental_date_window(
        database_path=database_path, end_date=end_date, overlap_days=overlap_days
    )
    run_id, _ = prepare_run(
        snapshot_path, start_date, end_date, resume=False, database_path=database_path
    )
    return run_id, start_date, end_date


def latest_run(database_path: Path = DATABASE_PATH) -> dict[str, Any] | None:
    if not database_path.exists():
        return None
    connection = connect_database(database_path, initialize=True)
    try:
        row = connection.execute(
            """
            SELECT run_id, status, snapshot_as_of_date, start_date, end_date,
                   total_bonds, parquet_path, created_at_utc, completed_at_utc,
                   snapshot_sha256
            FROM history_runs ORDER BY created_at_utc DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        columns = [item[0] for item in connection.description]
        return dict(zip(columns, row))
    finally:
        connection.close()


def list_jobs(run_id: str, database_path: Path = DATABASE_PATH) -> list[dict[str, Any]]:
    if not database_path.exists():
        return []
    connection = connect_database(database_path, initialize=False)
    try:
        result = connection.execute(
            """
            SELECT isin, issuer_name, cusip, symbol, status, attempts,
                   observation_count, first_date, last_date, quality_flags, last_error
            FROM run_bonds WHERE run_id = ?
            ORDER BY CASE status
                WHEN 'failed' THEN 0 WHEN 'needs_review' THEN 1 WHEN 'review' THEN 2
                WHEN 'pending' THEN 3 WHEN 'downloading' THEN 4 ELSE 5 END,
                observation_count DESC, issuer_name
            """,
            [run_id],
        ).fetchall()
        names = [item[0] for item in connection.description]
        jobs = [dict(zip(names, row)) for row in result]
        for job in jobs:
            job["quality_flags"] = json.loads(job["quality_flags"] or "[]")
        return jobs
    finally:
        connection.close()


def run_summary(run_id: str, database_path: Path = DATABASE_PATH) -> dict[str, int | str]:
    connection = connect_database(database_path, initialize=False)
    try:
        counts = dict(
            connection.execute(
                "SELECT status, count(*) FROM run_bonds WHERE run_id = ? GROUP BY status",
                [run_id],
            ).fetchall()
        )
        run = connection.execute(
            "SELECT status, total_bonds FROM history_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if not run:
            raise ValueError(f"Unknown download run: {run_id}")
        return {
            "status": run[0],
            "total": run[1],
            "downloaded": counts.get("downloaded", 0),
            "review": counts.get("review", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0) + counts.get("downloading", 0),
            "needs_review": counts.get("needs_review", 0),
        }
    finally:
        connection.close()


def update_cusip(run_id: str, isin: str, cusip: str, database_path: Path = DATABASE_PATH) -> None:
    normalized = cusip.strip().upper()
    if normalized and len(normalized) != 9:
        raise ValueError("Enter a 9-character CUSIP.")
    connection = connect_database(database_path, initialize=False)
    try:
        connection.execute(
            """
            UPDATE run_bonds SET cusip = ?, status = ?, last_error = NULL,
                quality_flags = '[]', updated_at_utc = ?
            WHERE run_id = ? AND isin = ?
            """,
            [normalized, "pending" if normalized else "needs_review", _now(), run_id, isin],
        )
    finally:
        connection.close()


def _fetch_history(identifiers: list[str], start_date: date, end_date: date) -> list[dict[str, Any]]:
    obb = _provider_client()
    response = obb.finra.fixedincome.bond_historical(
        cusip=identifiers,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        provider="finra",
    )
    return [record.model_dump() for record in response.results]


def augment_missing_fields(
    run_id: str,
    database_path: Path = DATABASE_PATH,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: ProgressCallback = lambda _done, _total: None,
    on_batch: BatchCallback = lambda _size: None,
) -> dict[str, Any]:
    """Refetch incomplete bond histories and audit valid price/yield backfills.

    Only bonds with at least one invalid/missing price or yield field (or no
    observations) in the source run window are requested. Existing valid raw
    values are never replaced. An invalid prior value can be replaced only by a
    newly fetched value that passes the registered TRACE cleaning policy; its
    prior value and disposition are retained in the augmentation audit table.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")

    augmentation_id = uuid.uuid4().hex
    connection = connect_database(database_path, initialize=True)
    try:
        run = connection.execute(
            "SELECT start_date, end_date FROM history_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if not run:
            raise ValueError(f"Unknown download run: {run_id}")
        start_date, end_date = run
        price_reason = reason_sql("price", TRACE_FIELD_POLICIES["price"])
        yield_reason = reason_sql("last_sale_yield", TRACE_FIELD_POLICIES["last_sale_yield"])
        jobs = connection.execute(
            f"""SELECT rb.isin, rb.issuer_name, rb.cusip, rb.symbol,
                       rb.finra_security_id
                FROM run_bonds rb
                WHERE rb.run_id = ? AND trim(rb.cusip) <> ''
                  AND (
                    NOT EXISTS (
                        SELECT 1 FROM observations ox
                        WHERE ox.isin = rb.isin
                          AND ox.trade_date BETWEEN ? AND ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM observations o
                        WHERE o.isin = rb.isin
                          AND o.trade_date BETWEEN ? AND ?
                          AND (({price_reason}) <> 'valid' OR ({yield_reason}) <> 'valid')
                    )
                  )
                ORDER BY rb.issuer_name, rb.isin""",
            [run_id, start_date, end_date, start_date, end_date],
        ).fetchall()
        job_names = [column[0] for column in connection.description]
        jobs = [dict(zip(job_names, row)) for row in jobs]
        started = _now()
        connection.execute(
            """INSERT INTO observation_augmentation_runs
               (augmentation_id, source_run_id, started_at_utc, status, requested_bonds)
               VALUES (?, ?, ?, 'running', ?)""",
            [augmentation_id, run_id, started, len(jobs)],
        )
        connection.commit()

        processed = response_rows = inserted_rows = fields_filled = error_count = 0
        last_error: str | None = None
        status = "complete"
        for batch_start in range(0, len(jobs), batch_size):
            batch = jobs[batch_start : batch_start + batch_size]
            on_batch(len(batch))
            try:
                response = _fetch_history(
                    [str(job["cusip"]).strip().upper() for job in batch],
                    start_date,
                    end_date,
                )
            except Exception as error:
                logger.exception(
                    "Augmentation %s provider request failed for batch starting at %d",
                    augmentation_id,
                    batch_start,
                )
                error_count += len(batch)
                last_error = str(error)
                status = "partial"
                break
            response_rows += len(response)
            by_cusip: dict[str, list[dict[str, Any]]] = {}
            for record in response:
                cusip = str(record.get("cusip") or "").strip().upper()
                if cusip:
                    by_cusip.setdefault(cusip, []).append(record)

            for job in batch:
                cusip = str(job["cusip"]).strip().upper()
                records = by_cusip.get(cusip, [])
                unique_records = _deduplicate_records(records)
                now = _now()
                bond_inserted = 0
                bond_fields_filled = 0
                try:
                    connection.execute("BEGIN TRANSACTION")
                    for record in unique_records:
                        trade_date = _as_date(record.get("date"))
                        if trade_date is None or not start_date <= trade_date <= end_date:
                            continue
                        incoming_price, _ = classify_value(
                            record.get("price"), TRACE_FIELD_POLICIES["price"]
                        )
                        incoming_yield, _ = classify_value(
                            record.get("last_sale_yield"),
                            TRACE_FIELD_POLICIES["last_sale_yield"],
                        )
                        prior = connection.execute(
                            """SELECT price, last_sale_yield FROM observations
                               WHERE isin = ? AND trade_date = ?""",
                            [job["isin"], trade_date],
                        ).fetchone()
                        provider_cusip = str(record.get("cusip") or cusip).strip().upper()
                        values_to_audit = []
                        if prior is None:
                            raw_price = _optional_float(record.get("price"))
                            raw_yield = _optional_float(record.get("last_sale_yield"))
                            connection.execute(
                                """INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                [
                                    job["isin"],
                                    trade_date,
                                    provider_cusip,
                                    record.get("symbol") or job.get("symbol"),
                                    str(record.get("finra_security_id") or job.get("finra_security_id") or ""),
                                    str(record.get("product_type") or ""),
                                    raw_price,
                                    raw_yield,
                                    now,
                                    run_id,
                                ],
                            )
                            bond_inserted += 1
                            for field_name, value in (
                                ("price", incoming_price),
                                ("last_sale_yield", incoming_yield),
                            ):
                                if value is not None:
                                    values_to_audit.append(
                                        (field_name, None, "no_row", value)
                                    )
                        else:
                            updates: dict[str, float] = {}
                            for field_name, old_value, new_value, policy in (
                                ("price", prior[0], incoming_price, TRACE_FIELD_POLICIES["price"]),
                                (
                                    "last_sale_yield",
                                    prior[1],
                                    incoming_yield,
                                    TRACE_FIELD_POLICIES["last_sale_yield"],
                                ),
                            ):
                                old_clean, old_reason = classify_value(old_value, policy)
                                if old_clean is None and new_value is not None:
                                    updates[field_name] = new_value
                                    values_to_audit.append(
                                        (field_name, old_value, old_reason, new_value)
                                    )
                            if updates:
                                connection.execute(
                                    """UPDATE observations SET
                                       price = coalesce(?, price),
                                       last_sale_yield = coalesce(?, last_sale_yield)
                                       WHERE isin = ? AND trade_date = ?""",
                                    [
                                        updates.get("price"),
                                        updates.get("last_sale_yield"),
                                        job["isin"],
                                        trade_date,
                                    ],
                                )
                        for field_name, old_value, old_reason, new_value in values_to_audit:
                            connection.execute(
                                """INSERT INTO observation_augmentation_fields VALUES
                                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                [
                                    augmentation_id,
                                    run_id,
                                    job["isin"],
                                    trade_date,
                                    field_name,
                                    old_value,
                                    old_reason,
                                    new_value,
                                    now,
                                    provider_cusip,
                                ],
                            )
                        bond_fields_filled += len(values_to_audit)
                    coverage = connection.execute(
                        """SELECT count(*), min(trade_date), max(trade_date)
                           FROM observations WHERE isin = ? AND trade_date BETWEEN ? AND ?""",
                        [job["isin"], start_date, end_date],
                    ).fetchone()
                    connection.execute(
                        """UPDATE run_bonds SET observation_count = ?, first_date = ?,
                           last_date = ?, updated_at_utc = ?
                           WHERE run_id = ? AND isin = ?""",
                        [
                            int(coverage[0]),
                            coverage[1],
                            coverage[2],
                            now,
                            run_id,
                            job["isin"],
                        ],
                    )
                    connection.execute(
                        """UPDATE observation_augmentation_runs SET
                           processed_bonds = ?, response_rows = ?, inserted_rows = ?,
                           fields_filled = ?, error_count = ?
                           WHERE augmentation_id = ?""",
                        [
                            processed + 1,
                            response_rows,
                            inserted_rows + bond_inserted,
                            fields_filled + bond_fields_filled,
                            error_count,
                            augmentation_id,
                        ],
                    )
                    connection.commit()
                except Exception as error:
                    connection.rollback()
                    logger.exception(
                        "Augmentation %s failed while merging bond %s",
                        augmentation_id,
                        job["isin"],
                    )
                    error_count += 1
                    last_error = str(error)
                    bond_inserted = 0
                    bond_fields_filled = 0
                processed += 1
                inserted_rows += bond_inserted
                fields_filled += bond_fields_filled
                on_progress(processed, len(jobs))

        if error_count:
            status = "partial"
        completed = _now()
        connection.execute(
            """UPDATE observation_augmentation_runs SET completed_at_utc = ?,
               status = ?, processed_bonds = ?, response_rows = ?, inserted_rows = ?,
               fields_filled = ?, error_count = ?, last_error = ?
               WHERE augmentation_id = ?""",
            [
                completed,
                status,
                processed,
                response_rows,
                inserted_rows,
                fields_filled,
                error_count,
                last_error,
                augmentation_id,
            ],
        )
        connection.commit()
        if inserted_rows or fields_filled:
            try:
                _export_parquet(connection, run_id, database_path)
            except Exception as error:
                logger.exception("Augmentation %s could not refresh the Parquet export", augmentation_id)
                status = "complete_export_failed"
                last_error = str(error)
                error_count += 1
                connection.execute(
                    """UPDATE observation_augmentation_runs SET status = ?, error_count = ?,
                       last_error = ? WHERE augmentation_id = ?""",
                    [status, error_count, last_error, augmentation_id],
                )
                connection.commit()
        logger.info(
            "Augmentation %s finished: status=%s bonds=%d/%d rows=%d fields=%d errors=%d",
            augmentation_id,
            status,
            processed,
            len(jobs),
            inserted_rows,
            fields_filled,
            error_count,
        )
        return {
            "augmentation_id": augmentation_id,
            "status": status,
            "requested_bonds": len(jobs),
            "processed_bonds": processed,
            "response_rows": response_rows,
            "inserted_rows": inserted_rows,
            "fields_filled": fields_filled,
            "error_count": error_count,
            "last_error": last_error,
        }
    finally:
        connection.close()


def _quality_flags(records: list[dict[str, Any]], start_date: date, end_date: date) -> list[str]:
    if not records:
        return ["no_history"]
    dates = [_as_date(record.get("date")) for record in records]
    raw_prices = [_optional_float(record.get("price")) for record in records]
    raw_yields = [_optional_float(record.get("last_sale_yield")) for record in records]
    price_reasons = [classify_value(value, TRACE_FIELD_POLICIES["price"])[1] for value in raw_prices]
    yield_reasons = [
        classify_value(value, TRACE_FIELD_POLICIES["last_sale_yield"])[1] for value in raw_yields
    ]
    prices = [clean_trace_value("price", value) for value in raw_prices]
    yields = [clean_trace_value("last_sale_yield", value) for value in raw_yields]
    # TRACE can return sentinel and whole-number values in columns that also
    # contain floats. Give Polars explicit types rather than infer from an early
    # sample of rows.
    frame = pl.DataFrame(
        {
            "date": pl.Series("date", dates, dtype=pl.Date, strict=False),
            "price": pl.Series("price", prices, dtype=pl.Float64, strict=False),
            "last_sale_yield": pl.Series(
                "last_sale_yield", yields, dtype=pl.Float64, strict=False
            ),
        }
    )
    flags: list[str] = []
    invalid_prices = frame.filter(pl.col("price").is_null()).height
    if invalid_prices:
        flags.append(f"invalid_price:{invalid_prices}")
    sentinel_prices = price_reasons.count("sentinel")
    if sentinel_prices:
        flags.append(f"sentinel_price:{sentinel_prices}")
    price_limits = price_reasons.count("outside_absolute_limit")
    if price_limits:
        flags.append(f"price_outside_limit:{price_limits}")
    missing_yields = yield_reasons.count("missing") + yield_reasons.count("zero_is_missing")
    if missing_yields:
        flags.append(f"missing_yield:{missing_yields}")
    sentinel_yields = yield_reasons.count("sentinel")
    if sentinel_yields:
        flags.append(f"sentinel_yield:{sentinel_yields}")
    yield_limits = yield_reasons.count("outside_absolute_limit")
    if yield_limits:
        flags.append(f"yield_outside_limit:{yield_limits}")
    missing_dates = frame.filter(pl.col("date").is_null()).height
    if missing_dates:
        flags.append(f"missing_date:{missing_dates}")
    outside = frame.filter(
        pl.col("date").is_not_null()
        & ((pl.col("date") < start_date) | (pl.col("date") > end_date))
    ).height
    if outside:
        flags.append(f"date_out_of_range:{outside}")
    non_null_dates = frame.filter(pl.col("date").is_not_null()).get_column("date")
    duplicates = int(non_null_dates.is_duplicated().sum())
    if duplicates:
        flags.append(f"duplicate_dates:{duplicates}")
    return flags


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for record in records:
        trade_date = record.get("date")
        key = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        by_date[key] = record
    return [by_date[key] for key in sorted(by_date)]


def _store_job_result(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    job: dict[str, Any],
    records: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    error: str | None = None,
) -> dict[str, Any]:
    now = _now()
    if not records:
        status = "failed"
        error = error or "FINRA returned no history for this CUSIP and date range."
        flags: list[str] = []
        unique_records: list[dict[str, Any]] = []
    else:
        flags = _quality_flags(records, start_date, end_date)
        unique_records = _deduplicate_records(records)
        status = "review" if flags else "downloaded"
        if "no_history" in flags:
            status = "failed"

    mismatched = [
        record for record in unique_records
        if str(record.get("cusip") or "").strip().upper() != job["cusip"].upper()
    ]
    if mismatched:
        flags.append(f"cusip_mismatch:{len(mismatched)}")
        status = "review"

    observation_rows = []
    retrieval_id = str(uuid.uuid4())
    for record in unique_records:
        trade_date = record.get("date")
        if hasattr(trade_date, "isoformat"):
            trade_date = trade_date.isoformat()
        price = _optional_float(record.get("price"))
        sale_yield = _optional_float(record.get("last_sale_yield"))
        normalized = (
            job["isin"],
            trade_date,
            str(record.get("cusip") or job["cusip"]).strip().upper(),
            record.get("symbol") or job.get("symbol"),
            str(record.get("finra_security_id") or job.get("finra_security_id") or ""),
            str(record.get("product_type") or ""),
            price,
            sale_yield,
        )
        record_hash = hashlib.sha256(
            json.dumps(normalized, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        observation_rows.append((retrieval_id, *normalized, now, run_id, record_hash))
    if observation_rows:
        connection.executemany(
            """INSERT INTO trace_observation_versions VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            observation_rows,
        )
        # Retain the old latest-value table for existing readers during the
        # migration. Reproducible pipelines read the append-only table.
        connection.executemany(
            """INSERT OR REPLACE INTO observations VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*row[1:9], row[9], run_id) for row in observation_rows],
        )

    dates = [
        record["date"].isoformat() if hasattr(record.get("date"), "isoformat") else str(record.get("date"))
        for record in unique_records
        if record.get("date")
    ]
    symbol = next((str(row.get("symbol")) for row in records if row.get("symbol")), job.get("symbol"))
    finra_id = next(
        (str(row.get("finra_security_id")) for row in records if row.get("finra_security_id")),
        job.get("finra_security_id"),
    )
    connection.execute(
        """
        UPDATE run_bonds SET symbol = ?, finra_security_id = ?, status = ?,
            observation_count = ?, first_date = ?, last_date = ?, quality_flags = ?,
            last_error = ?, updated_at_utc = ?
        WHERE run_id = ? AND isin = ?
        """,
        [
            symbol,
            finra_id,
            status,
            len(unique_records),
            min(dates) if dates else None,
            max(dates) if dates else None,
            json.dumps(flags),
            error,
            now,
            run_id,
            job["isin"],
        ],
    )
    return {
        **job,
        "symbol": symbol,
        "finra_security_id": finra_id,
        "status": status,
        "attempts": job.get("attempts", 0),
        "observation_count": len(unique_records),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "quality_flags": flags,
        "last_error": error,
        "updated_at_utc": now,
    }


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _get_job(connection: duckdb.DuckDBPyConnection, run_id: str, isin: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT isin, issuer_name, cusip, symbol, finra_security_id, status, attempts,
               observation_count, first_date, last_date, quality_flags, last_error
        FROM run_bonds WHERE run_id = ? AND isin = ?
        """,
        [run_id, isin],
    ).fetchone()
    if not row:
        raise ValueError(f"No bond job for ISIN {isin} in run {run_id}.")
    columns = [item[0] for item in connection.description]
    job = dict(zip(columns, row))
    job["quality_flags"] = json.loads(job["quality_flags"] or "[]")
    return job


def _set_downloading(connection: duckdb.DuckDBPyConnection, run_id: str, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for job in jobs:
        attempts = int(job.get("attempts", 0)) + 1
        now = _now()
        connection.execute(
            """
            UPDATE run_bonds SET status = 'downloading', attempts = ?, last_error = NULL,
                updated_at_utc = ? WHERE run_id = ? AND isin = ?
            """,
            [attempts, now, run_id, job["isin"]],
        )
        updated.append({**job, "status": "downloading", "attempts": attempts, "last_error": None})
    return updated


def _fetch_reference_symbol(cusip: str) -> str | None:
    obb = _provider_client()
    response = obb.finra.fixedincome.bond_prices(cusip=cusip, provider="finra")
    for record in response.results:
        symbol = getattr(record, "symbol", None)
        if symbol:
            return str(symbol)
    return None


def download_run(
    run_id: str,
    database_path: Path = DATABASE_PATH,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stop_requested: Callable[[], bool] = lambda: False,
    on_job: JobCallback = lambda _job: None,
    on_progress: ProgressCallback = lambda _done, _total: None,
    on_batch: BatchCallback = lambda _size: None,
) -> None:
    connection = connect_database(database_path, initialize=False)
    try:
        run = connection.execute(
            "SELECT start_date, end_date, total_bonds FROM history_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if not run:
            raise ValueError(f"Unknown run: {run_id}")
        start_date, end_date, total = run
        logger.info(
            "Run %s started: total_bonds=%d range=%s..%s batch_size=%d thread=%s",
            run_id, total, start_date, end_date, batch_size, threading.current_thread().name,
        )
        connection.execute(
            "UPDATE history_runs SET status = 'running', updated_at_utc = ? WHERE run_id = ?",
            [_now(), run_id],
        )
        completed = int(
            connection.execute(
                "SELECT count(*) FROM run_bonds WHERE run_id = ? AND status IN ('downloaded','review','needs_review')",
                [run_id],
            ).fetchone()[0]
        )
        while not stop_requested():
            rows = connection.execute(
                """
                SELECT isin, issuer_name, cusip, symbol, finra_security_id, status,
                       attempts, observation_count, first_date, last_date, quality_flags, last_error
                FROM run_bonds WHERE run_id = ? AND status IN ('pending','failed') AND cusip <> ''
                ORDER BY issuer_name, isin LIMIT ?
                """,
                [run_id, max(1, batch_size)],
            ).fetchall()
            names = [item[0] for item in connection.description]
            jobs = [dict(zip(names, row)) for row in rows]
            if not jobs:
                break
            for job in jobs:
                job["quality_flags"] = json.loads(job["quality_flags"] or "[]")
            downloading = _set_downloading(connection, run_id, jobs)
            connection.commit()
            for job in downloading:
                on_job(job)

            try:
                logger.info(
                    "Run %s requesting batch: bonds=%d first_isin=%s first_cusip=%s",
                    run_id, len(jobs), jobs[0]["isin"], jobs[0]["cusip"],
                )
                on_batch(len(jobs))
                records = _fetch_history(
                    [job["cusip"] for job in jobs], start_date, end_date
                )
                by_cusip: dict[str, list[dict[str, Any]]] = {}
                for record in records:
                    key = str(record.get("cusip") or "").strip().upper()
                    by_cusip.setdefault(key, []).append(record)
                batch_error = None
                logger.info(
                    "Run %s response received: rows=%d unique_cusips=%d",
                    run_id, len(records), len(by_cusip),
                )
            except Exception as error:
                by_cusip = {}
                batch_error = str(error)
                logger.exception("Run %s batch request failed", run_id)

            for job in jobs:
                try:
                    connection.execute("BEGIN TRANSACTION")
                    result = _store_job_result(
                        connection,
                        run_id,
                        job,
                        by_cusip.get(job["cusip"].upper(), []),
                        start_date,
                        end_date,
                        batch_error,
                    )
                    if batch_error:
                        connection.execute(
                            "UPDATE run_bonds SET status = 'failed' WHERE run_id = ? AND isin = ?",
                            [run_id, job["isin"]],
                        )
                        result["status"] = "failed"
                        result["last_error"] = batch_error
                    connection.commit()
                except Exception as error:
                    connection.rollback()
                    logger.exception(
                        "Run %s failed while processing bond %s; continuing",
                        run_id,
                        job["isin"],
                    )
                    connection.execute(
                        """UPDATE run_bonds SET status='failed', last_error=?,
                           updated_at_utc=? WHERE run_id=? AND isin=?""",
                        [str(error), _now(), run_id, job["isin"]],
                    )
                    connection.commit()
                    result = {
                        **job,
                        "status": "failed",
                        "last_error": str(error),
                        "observation_count": 0,
                        "quality_flags": [],
                    }
                completed += 1
                logger.info(
                    "Run %s bond result: isin=%s status=%s rows=%d flags=%s",
                    run_id, job["isin"], result["status"],
                    result["observation_count"], result["quality_flags"],
                )
                on_job(result)
                on_progress(completed, int(total))

            if batch_error:
                # A batch-wide failure is likely systemic (provider startup,
                # connectivity, or endpoint outage). Stop here so one such
                # problem does not mark the entire universe failed.
                logger.error(
                    "Run %s paused after a batch-wide error; remaining bonds stay resumable",
                    run_id,
                )
                break

        pending = connection.execute(
            "SELECT count(*) FROM run_bonds WHERE run_id = ? AND status IN ('pending','downloading','failed')",
            [run_id],
        ).fetchone()[0]
        review = connection.execute(
            "SELECT count(*) FROM run_bonds WHERE run_id = ? AND status IN ('review','needs_review')",
            [run_id],
        ).fetchone()[0]
        status = "partial" if pending or stop_requested() else ("complete_with_review" if review else "complete")
        completed_at = _now()
        connection.execute(
            "UPDATE history_runs SET status = ?, updated_at_utc = ?, completed_at_utc = ? WHERE run_id = ?",
            [status, completed_at, completed_at, run_id],
        )
        connection.commit()
        logger.info("Run %s ended with status=%s", run_id, status)
        if status.startswith("complete"):
            _export_parquet(connection, run_id, database_path)
    except Exception:
        logger.exception("Run %s terminated unexpectedly", run_id)
        raise
    finally:
        connection.close()


def fix_bond(
    run_id: str,
    isin: str,
    database_path: Path = DATABASE_PATH,
    on_job: JobCallback = lambda _job: None,
) -> None:
    """Retry a single bond, falling back to the FINRA symbol if CUSIP lookup fails."""
    connection = connect_database(database_path, initialize=False)
    try:
        run = connection.execute(
            "SELECT start_date, end_date FROM history_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if not run:
            raise ValueError(f"Unknown run: {run_id}")
        start_date, end_date = run
        job = _get_job(connection, run_id, isin)
        if not job["cusip"]:
            raise ValueError("Enter a valid CUSIP in the CUSIP column before attempting a fix.")

        [downloading] = _set_downloading(connection, run_id, [job])
        connection.commit()
        on_job(downloading)

        primary_records: list[dict[str, Any]] = []
        primary_error: str | None = None
        try:
            primary_records = _fetch_history([job["cusip"]], start_date, end_date)
        except Exception as error:
            primary_error = str(error)
            logger.exception("Fix attempt primary CUSIP lookup failed: run=%s isin=%s", run_id, isin)

        primary_for_bond = [
            record for record in primary_records
            if str(record.get("cusip") or "").strip().upper() == job["cusip"].upper()
        ]
        chosen = primary_for_bond
        chosen_symbol = job.get("symbol")
        fallback_error = None
        if not chosen or _quality_flags(chosen, start_date, end_date):
            symbol = job.get("symbol")
            try:
                symbol = symbol or _fetch_reference_symbol(job["cusip"])
            except Exception as error:
                fallback_error = str(error)
                logger.exception("Fix attempt reference lookup failed: run=%s isin=%s", run_id, isin)
            if symbol and symbol.upper() != job["cusip"].upper():
                try:
                    alternate = _fetch_history([symbol], start_date, end_date)
                    alternate_for_bond = [
                        record for record in alternate
                        if str(record.get("cusip") or "").strip().upper() == job["cusip"].upper()
                        or str(record.get("symbol") or "").strip().upper() == symbol.upper()
                    ]
                    if alternate_for_bond and len(_quality_flags(alternate_for_bond, start_date, end_date)) < len(_quality_flags(chosen, start_date, end_date)):
                        chosen = alternate_for_bond
                        chosen_symbol = symbol
                except Exception as error:
                    fallback_error = str(error)
                    logger.exception("Fix attempt symbol lookup failed: run=%s isin=%s", run_id, isin)

        error_text = None
        if not chosen:
            error_text = primary_error or fallback_error or "No history returned after retry."
        connection.execute("BEGIN TRANSACTION")
        try:
            result = _store_job_result(
                connection,
                run_id,
                {**job, "symbol": chosen_symbol},
                chosen,
                start_date,
                end_date,
                error_text,
            )
            if not chosen:
                connection.execute(
                    "UPDATE run_bonds SET status='failed', last_error=? WHERE run_id=? AND isin=?",
                    [error_text, run_id, isin],
                )
                result["status"] = "failed"
                result["last_error"] = error_text
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        on_job(result)
        counts = connection.execute(
            "SELECT count(*) FILTER (WHERE status IN ('pending','downloading','failed')), "
            "count(*) FILTER (WHERE status IN ('review','needs_review')) "
            "FROM run_bonds WHERE run_id = ?", [run_id]
        ).fetchone()
        status = "partial" if counts[0] else ("complete_with_review" if counts[1] else "complete")
        completed_at = _now()
        connection.execute(
            "UPDATE history_runs SET status = ?, updated_at_utc = ?, completed_at_utc = ? WHERE run_id = ?",
            [status, completed_at, completed_at, run_id]
        )
        connection.commit()
        if status.startswith("complete"):
            _export_parquet(connection, run_id, database_path)
    finally:
        connection.close()


def _export_parquet(
    connection: duckdb.DuckDBPyConnection, run_id: str, database_path: Path
) -> Path:
    run = connection.execute(
        """SELECT start_date, end_date, coalesce(completed_at_utc, updated_at_utc)
           FROM history_runs WHERE run_id = ?""", [run_id]
    ).fetchone()
    if not run:
        raise ValueError(f"Unknown history run: {run_id}")
    start_date, end_date, as_of = run
    as_of_key = str(as_of).replace("-", "").replace(":", "").replace("+00:00", "Z")
    export_dir = (
        database_path.parent / "parquet" / f"run_id={run_id}" / f"snapshot_asof={as_of_key}"
    )
    export_dir.mkdir(parents=True, exist_ok=True)
    destination = export_dir.as_posix().replace("'", "''")
    run_literal = run_id.replace("'", "''")
    as_of_literal = str(as_of).replace("'", "''")
    connection.execute(
        f"""
        COPY (
            WITH eligible AS (
                SELECT v.*, row_number() OVER (
                    PARTITION BY v.isin, v.trade_date
                    ORDER BY v.retrieved_at_utc DESC, v.retrieval_id DESC
                ) AS version_rank
                FROM trace_observation_versions v
                WHERE v.retrieved_at_utc <= '{as_of_literal}'
            )
            SELECT v.isin, rb.issuer_name, v.trade_date, v.cusip, v.symbol,
                   v.finra_security_id, v.product_type, v.price,
                   v.last_sale_yield, v.retrieved_at_utc,
                   v.retrieval_run_id AS last_run_id, v.retrieval_id,
                   v.record_hash, year(v.trade_date) AS trade_year
            FROM eligible v
            JOIN run_bonds rb ON rb.isin = v.isin AND rb.run_id = '{run_literal}'
            WHERE v.version_rank = 1
            ORDER BY v.isin, v.trade_date
        ) TO '{destination}'
        (FORMAT PARQUET, PARTITION_BY (trade_year), OVERWRITE_OR_IGNORE TRUE)
        """
    )
    connection.execute(
        "UPDATE history_runs SET parquet_path = ?, updated_at_utc = ? WHERE run_id = ?",
        [str(export_dir), _now(), run_id],
    )
    return export_dir


def export_csv(run_id: str, output_path: Path, database_path: Path = DATABASE_PATH) -> Path:
    connection = connect_database(database_path, initialize=False)
    try:
        run = connection.execute(
            """SELECT start_date, end_date, coalesce(completed_at_utc, updated_at_utc)
               FROM history_runs WHERE run_id=?""", [run_id]
        ).fetchone()
        if not run:
            raise ValueError(f"Unknown history run: {run_id}")
        start_date, end_date, as_of = run
        destination = output_path.resolve().as_posix().replace("'", "''")
        as_of_literal = str(as_of).replace("'", "''")
        connection.execute(
            f"""
            COPY (
                WITH eligible AS (
                    SELECT v.*, row_number() OVER (
                        PARTITION BY v.isin, v.trade_date
                        ORDER BY v.retrieved_at_utc DESC, v.retrieval_id DESC
                    ) AS version_rank
                    FROM trace_observation_versions v
                    WHERE v.retrieved_at_utc <= '{as_of_literal}'
                      AND v.trade_date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
                )
                SELECT v.trade_date AS date, v.isin, rb.issuer_name, v.cusip,
                       v.symbol, v.finra_security_id, v.product_type, v.price,
                       v.last_sale_yield, v.retrieved_at_utc,
                       v.retrieval_run_id, v.retrieval_id, v.record_hash
                FROM eligible v
                JOIN run_bonds rb ON rb.isin = v.isin AND rb.run_id = '{run_id}'
                WHERE v.version_rank=1
                ORDER BY v.isin, v.trade_date
            ) TO '{destination}' (HEADER, DELIMITER ',')
            """
        )
        return output_path
    finally:
        connection.close()


def read_observations_for_run(run_id: str, database_path: Path = DATABASE_PATH) -> int:
    connection = connect_database(database_path, initialize=False)
    try:
        return int(
            connection.execute(
                """
                WITH run_info AS (
                    SELECT start_date, end_date,
                           coalesce(completed_at_utc, updated_at_utc) AS as_of
                    FROM history_runs WHERE run_id=?
                ), eligible AS (
                    SELECT v.*, row_number() OVER (
                        PARTITION BY v.isin, v.trade_date
                        ORDER BY v.retrieved_at_utc DESC, v.retrieval_id DESC
                    ) AS version_rank
                    FROM trace_observation_versions v, run_info r
                    WHERE v.retrieved_at_utc <= r.as_of
                )
                SELECT count(*) FROM eligible o
                JOIN run_bonds rb ON rb.isin = o.isin AND rb.run_id = ?
                WHERE o.version_rank=1
                """,
                [run_id, run_id],
            ).fetchone()[0]
        )
    finally:
        connection.close()
