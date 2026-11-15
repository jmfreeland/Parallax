"""Persist generated exploratory portfolios with their methodology metadata."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .exploratory_portfolio import ExploratoryPortfolio
from .trace_history import DATABASE_PATH


ARCHIVE_DIR = Path(__file__).resolve().parents[1] / "data" / "portfolio_archive"


def save_portfolio(
    portfolio: ExploratoryPortfolio,
    *,
    bond_list_path: Path,
    minimum_return_observations: int,
    require_valid_yield: bool,
) -> Path:
    """Write a self-contained JSON archive record and return its path."""
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    record: dict[str, Any] = {
        "archive_id": uuid4().hex,
        "created_at": created_at,
        "as_of_date": portfolio.as_of_date.isoformat(),
        "methodology": {
            "weighting_method": portfolio.weighting_method,
            "estimation_window_days": portfolio.estimation_window_days,
            "minimum_return_observations": minimum_return_observations,
            "require_valid_yield": require_valid_yield,
            "bond_list_path": str(bond_list_path.expanduser().resolve()),
            "trace_database_path": str(DATABASE_PATH),
            "universe_count": portfolio.universe_count,
            "matured_count": portfolio.matured_count,
            "missing_yield_count": portfolio.missing_yield_count,
            "excluded_no_yield": portfolio.excluded_no_yield,
            "position_count": portfolio.position_count,
            "excluded_by_method": portfolio.excluded_by_method,
        },
        "positions": [
            {key: value.isoformat() if isinstance(value, date) else value for key, value in row.items()}
            for row in portfolio.rows
        ],
    }
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{created_at[:10]}_{portfolio.as_of_date}_{record['archive_id'][:8]}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_archive() -> list[dict[str, Any]]:
    """Return valid archive records, newest creation time first."""
    if not ARCHIVE_DIR.exists():
        return []
    records = []
    for path in ARCHIVE_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["_path"] = str(path)
            records.append(record)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)
