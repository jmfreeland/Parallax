"""Download daily U.S. Treasury par curves and map bond yields to them.

Treasury publishes constant-maturity par yields at fixed nodes. This module
preserves those observations and linearly interpolates between adjacent nodes
for a bond's remaining maturity. Interpolated values are derived estimates,
not Treasury-published CMT rates or zero-coupon spot rates.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Callable
import uuid
import xml.etree.ElementTree as ET

import requests

from .trace_history import DATABASE_PATH, connect_database


ROOT = Path(__file__).resolve().parents[1]
TREASURY_DIR = ROOT / "data" / "treasury"
SOURCE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
# Treasury's field names are expressed as tenor strings in its XML data feed.
NODE_FIELDS = {
    "BC_1MONTH": 1 / 12,
    "BC_1_5MONTH": 1.5 / 12,
    "BC_2MONTH": 2 / 12,
    "BC_3MONTH": 3 / 12,
    "BC_4MONTH": 4 / 12,
    "BC_6MONTH": 6 / 12,
    "BC_1YEAR": 1.0,
    "BC_2YEAR": 2.0,
    "BC_3YEAR": 3.0,
    "BC_5YEAR": 5.0,
    "BC_7YEAR": 7.0,
    "BC_10YEAR": 10.0,
    "BC_20YEAR": 20.0,
    "BC_30YEAR": 30.0,
}

ProgressCallback = Callable[[int, int, str], None]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _parse_feed(payload: bytes) -> list[dict[str, object]]:
    root = ET.fromstring(payload)
    records: list[dict[str, object]] = []
    for entry in root.iter():
        if _local_name(entry.tag) != "ENTRY":
            continue
        properties = {
            _local_name(element.tag): (element.text or "").strip()
            for element in entry.iter()
            if len(element) == 0
        }
        raw_date = properties.get("NEW_DATE") or properties.get("DATE")
        if not raw_date:
            continue
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw_date)
        if not match:
            continue
        row: dict[str, object] = {"date": date.fromisoformat(match.group())}
        for field, years in NODE_FIELDS.items():
            raw = properties.get(field, "")
            try:
                value = float(raw) if raw and raw.lower() not in {"null", "n/a"} else None
            except ValueError:
                value = None
            row[f"yield_{years:g}y_pct"] = value
        records.append(row)
    return records


def fetch_treasury_curve(
    start_date: date,
    end_date: date,
    *,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, object]]:
    """Fetch Treasury curve history in annual request batches for an inclusive range."""
    if start_date > end_date:
        raise ValueError("Start date must not be after end date.")
    rows: dict[date, dict[str, object]] = {}
    session = requests.Session()
    years = list(range(start_date.year, end_date.year + 1))
    for batch_index, year in enumerate(years, start=1):
        if on_progress is not None:
            on_progress(
                batch_index - 1,
                len(years),
                f"Requesting Treasury curve data for {year} (annual batch {batch_index}/{len(years)})",
            )
        response = session.get(
            SOURCE_URL,
            params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": year},
            timeout=45,
        )
        response.raise_for_status()
        batch_rows = _parse_feed(response.content)
        for row in batch_rows:
            observed = row["date"]
            if start_date <= observed <= end_date:
                rows[observed] = row
        if on_progress is not None:
            in_range = sum(start_date <= row["date"] <= end_date for row in batch_rows)
            on_progress(
                batch_index,
                len(years),
                f"Received {year} batch: {in_range:,} curve dates ({len(rows):,} unique dates total)",
            )
    return [rows[day] for day in sorted(rows)]


def treasury_yield_at_maturity(
    curve_row: dict[str, object], maturity_years: float
) -> float | None:
    """Linearly interpolate the par yield at a positive remaining maturity."""
    if maturity_years <= 0:
        return None
    points = sorted(
        (tenor, curve_row.get(f"yield_{tenor:g}y_pct"))
        for tenor in NODE_FIELDS.values()
        if curve_row.get(f"yield_{tenor:g}y_pct") is not None
    )
    if not points or maturity_years < points[0][0] or maturity_years > points[-1][0]:
        return None
    for tenor, rate in points:
        if maturity_years == tenor:
            return float(rate)
    for (left_tenor, left_rate), (right_tenor, right_rate) in zip(points, points[1:]):
        if left_tenor < maturity_years < right_tenor:
            weight = (maturity_years - left_tenor) / (right_tenor - left_tenor)
            return float(left_rate) + weight * (float(right_rate) - float(left_rate))
    return None


def map_trace_yields(
    trace_rows: list[dict[str, object]], curve_rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Join by exact observation date and compute maturity-matched yield spreads."""
    by_date = {
        date.fromisoformat(row["date"]) if isinstance(row["date"], str) else row["date"]: row
        for row in curve_rows
    }
    mapped: list[dict[str, object]] = []
    for bond in trace_rows:
        trade_date = bond["trade_date"]
        if isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)
        curve = by_date.get(trade_date)
        maturity = bond.get("maturity_date")
        if isinstance(maturity, str):
            maturity = date.fromisoformat(maturity)
        maturity_years = (
            (maturity - trade_date).days / 365.25
            if curve is not None and isinstance(maturity, date) and isinstance(trade_date, date)
            else None
        )
        treasury = (
            treasury_yield_at_maturity(curve, maturity_years)
            if curve is not None and maturity_years is not None
            else None
        )
        bond_yield = bond.get("yield_pct")
        mapped.append({
            **bond,
            "maturity_years": maturity_years,
            "treasury_par_yield_pct": treasury,
            "g_spread_bps": (
                (float(bond_yield) - treasury) * 100
                if bond_yield is not None and treasury is not None
                else None
            ),
            "curve_source": "U.S. Treasury daily nominal par yield curve",
            "curve_interpolation": "linear between published CMT nodes",
        })
    return mapped


def persist_curve_rows(
    rows: list[dict[str, object]],
    requested_start: date,
    requested_end: date,
    database_path: Path = DATABASE_PATH,
    source_file_sha256: str | None = None,
) -> str:
    """Append one immutable Treasury retrieval vintage to DuckDB."""
    if not rows:
        raise ValueError("Cannot persist an empty Treasury curve response.")
    curve_run_id = str(uuid.uuid4())
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    connection = connect_database(database_path, initialize=True)
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS treasury_curve_runs (
                   curve_run_id VARCHAR PRIMARY KEY,
                   source_name VARCHAR NOT NULL,
                   source_url VARCHAR NOT NULL,
                   requested_start DATE NOT NULL,
                   requested_end DATE NOT NULL,
                   first_observation DATE NOT NULL,
                   last_observation DATE NOT NULL,
                   observation_dates INTEGER NOT NULL,
                   retrieved_at_utc VARCHAR NOT NULL,
                   source_file_sha256 VARCHAR
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS treasury_curve_observations (
                   curve_run_id VARCHAR NOT NULL,
                   observation_date DATE NOT NULL,
                   tenor_years DOUBLE NOT NULL,
                   tenor_label VARCHAR NOT NULL,
                   par_yield_pct DOUBLE,
                   record_hash VARCHAR NOT NULL,
                   PRIMARY KEY (curve_run_id, observation_date, tenor_years)
               )"""
        )
        dates = [row["date"] for row in rows]
        connection.execute(
            """INSERT INTO treasury_curve_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                curve_run_id,
                "U.S. Treasury daily nominal par yield curve",
                SOURCE_URL,
                requested_start,
                requested_end,
                min(dates),
                max(dates),
                len(set(dates)),
                retrieved_at,
                source_file_sha256,
            ],
        )
        observations = []
        for row in rows:
            for field, tenor in NODE_FIELDS.items():
                label = field.removeprefix("BC_").replace("MONTH", "M").replace("YEAR", "Y").replace("_", ".")
                value = row.get(f"yield_{tenor:g}y_pct")
                digest_text = f"{row['date']}|{tenor:.8f}|{value}"
                record_hash = hashlib.sha256(digest_text.encode("utf-8")).hexdigest()
                observations.append((curve_run_id, row["date"], tenor, label, value, record_hash))
        connection.executemany(
            "INSERT INTO treasury_curve_observations VALUES (?, ?, ?, ?, ?, ?)",
            observations,
        )
        connection.commit()
        return curve_run_id
    finally:
        connection.close()


def load_curve_csv(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            row: dict[str, object] = {"date": date.fromisoformat(source["date"])}
            for tenor in NODE_FIELDS.values():
                raw = source.get(f"yield_{tenor:g}y_pct", "")
                row[f"yield_{tenor:g}y_pct"] = float(raw) if raw else None
            rows.append(row)
    return rows


def _history_window(database_path: Path) -> tuple[date, date]:
    if not database_path.exists():
        raise FileNotFoundError(f"TRACE history database not found: {database_path}")
    connection = connect_database(database_path, initialize=True)
    try:
        trace = connection.execute(
            "SELECT min(trade_date) FROM trace_observation_versions"
        ).fetchone()
        if not trace or not trace[0]:
            trace = connection.execute(
                "SELECT min(start_date) FROM history_runs WHERE status LIKE 'complete%'"
            ).fetchone()
        if not trace or not trace[0]:
            raise ValueError("No completed TRACE run is available to supply a date window.")
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "treasury_curve_observations" in tables:
            latest_curve = connection.execute(
                "SELECT max(observation_date) FROM treasury_curve_observations"
            ).fetchone()[0]
        else:
            latest_curve = None
        start = latest_curve - timedelta(days=10) if latest_curve else trace[0]
        return start, date.today()
    finally:
        connection.close()


def _write_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("Treasury returned no curve observations for the requested window.")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: value.isoformat() if isinstance(value, date) else value for key, value in row.items()})


def update_treasury_curve(
    start_date: date | None = None,
    end_date: date | None = None,
    database_path: Path = DATABASE_PATH,
    output_path: Path = TREASURY_DIR / "nominal_par_curve.csv",
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Fetch, persist, and describe a new Treasury curve vintage."""
    if (start_date is None) != (end_date is None):
        raise ValueError("Supply both start_date and end_date, or neither.")
    start, end = (start_date, end_date) if start_date and end_date else _history_window(database_path)
    years = end.year - start.year + 1
    total_steps = years + 3
    if on_progress is not None:
        on_progress(0, total_steps, f"Preparing annual Treasury batches for {start} to {end}")
    def report_fetch_progress(completed: int, _total: int, message: str) -> None:
        if on_progress is not None:
            on_progress(completed, total_steps, message)

    rows = fetch_treasury_curve(start, end, on_progress=report_fetch_progress)
    if not rows:
        raise ValueError("Treasury returned no curve observations for the requested window.")
    _write_csv(rows, output_path)
    if on_progress is not None:
        on_progress(years + 1, total_steps, f"Wrote {len(rows):,} curve dates to CSV")
    input_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    curve_run_id = persist_curve_rows(rows, start, end, database_path, input_hash)
    if on_progress is not None:
        on_progress(years + 2, total_steps, f"Stored {len(rows):,} observations in DuckDB")
    metadata_connection = connect_database(database_path, initialize=False)
    try:
        retrieved_at = metadata_connection.execute(
            "SELECT retrieved_at_utc FROM treasury_curve_runs WHERE curve_run_id=?",
            [curve_run_id],
        ).fetchone()[0]
    finally:
        metadata_connection.close()
    manifest: dict[str, object] = {
        "source": "U.S. Treasury daily nominal par yield curve XML feed",
        "source_url": SOURCE_URL,
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "observation_count": len(rows),
        "first_observation": rows[0]["date"].isoformat(),
        "last_observation": rows[-1]["date"].isoformat(),
        "retrieved_at_utc": retrieved_at,
        "yield_unit": "percent",
        "curve_type": "nominal par yield (Treasury CMT nodes)",
        "curve_run_id": curve_run_id,
        "source_file_sha256": input_hash,
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if on_progress is not None:
        on_progress(years + 3, total_steps, "Wrote vintage manifest; update complete")
    return rows, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--output", type=Path, default=TREASURY_DIR / "nominal_par_curve.csv")
    parser.add_argument("--import-csv", type=Path, help="Import an already downloaded curve CSV into DuckDB.")
    args = parser.parse_args()
    try:
        if bool(args.start) != bool(args.end):
            raise ValueError("Supply both --start and --end, or neither.")
        if args.import_csv:
            rows = load_curve_csv(args.import_csv)
            if not rows:
                raise ValueError("The imported Treasury CSV contains no observations.")
            start, end = args.start or min(row["date"] for row in rows), args.end or max(row["date"] for row in rows)
            input_hash = hashlib.sha256(args.import_csv.read_bytes()).hexdigest()
            output_path = args.import_csv
        else:
            rows, manifest = update_treasury_curve(
                args.start,
                args.end,
                args.database,
                args.output,
                on_progress=lambda completed, total, message: print(
                    f"[{completed}/{total}] {message}", flush=True
                ),
            )
            print(
                f"Stored {manifest['observation_count']:,} Treasury curve dates "
                f"({manifest['first_observation']} to {manifest['last_observation']}) "
                f"as run {manifest['curve_run_id']}"
            )
            print(f"CSV: {args.output}")
            print(f"Manifest: {args.output.with_suffix('.manifest.json')}")
            print(f"Retrieved at {manifest['retrieved_at_utc']}")
            return
        curve_run_id = persist_curve_rows(rows, start, end, args.database, input_hash)
        metadata_connection = connect_database(args.database, initialize=False)
        try:
            retrieved_at = metadata_connection.execute(
                "SELECT retrieved_at_utc FROM treasury_curve_runs WHERE curve_run_id=?",
                [curve_run_id],
            ).fetchone()[0]
        finally:
            metadata_connection.close()
        manifest = {
            "source": "U.S. Treasury daily nominal par yield curve XML feed",
            "source_url": SOURCE_URL,
            "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
            "observation_count": len(rows),
            "first_observation": rows[0]["date"].isoformat(),
            "last_observation": rows[-1]["date"].isoformat(),
            "retrieved_at_utc": retrieved_at,
            "yield_unit": "percent",
            "curve_type": "nominal par yield (Treasury CMT nodes)",
            "curve_run_id": curve_run_id,
            "source_file_sha256": input_hash,
        }
        output_path.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        print(f"Stored {len(rows):,} Treasury curve dates ({start} to {end}) as run {curve_run_id}")
        print(f"CSV: {output_path}")
        print(f"Manifest: {output_path.with_suffix('.manifest.json')}")
        print(f"Retrieved at {retrieved_at}")
    except Exception as error:
        print(f"Treasury curve download failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
