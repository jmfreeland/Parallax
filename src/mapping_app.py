"""Local PySide6 desktop app for reviewing issuer-to-equity-ticker mappings."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.build_lqd_snapshot import normalize_issuer_name


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_PATH = ROOT / "data" / "issuer_ticker_candidates.csv"
MAPPINGS_PATH = ROOT / "data" / "issuer_ticker_map.csv"

FIELDS = [
    "issuer_name",
    "issuer_lei",
    "representative_isin",
    "bond_ticker",
    "automated_equity_ticker",
    "equity_ticker_override",
    "equity_ticker",
    "equity_exchange",
    "equity_country",
    "mapping_source",
    "confidence",
    "mapping_relationship",
    "candidate_status",
    "mapping_status",
    "review_status",
    "notes",
]

HEADERS = [
    "Issuer / company",
    "Issuer LEI",
    "Representative bond ISIN",
    "Bond ticker",
    "Automated candidate",
    "Manual override",
    "Effective equity ticker",
    "Equity exchange",
    "Equity country",
    "Source",
    "Confidence",
    "Candidate relation",
    "Candidate status",
    "Mapping status",
    "Review status",
    "Notes",
]

EDITABLE_FIELDS = {
    "bond_ticker",
    "equity_ticker_override",
    "equity_exchange",
    "equity_country",
    "mapping_source",
    "confidence",
    "notes",
}


def _candidate_confidence(status: str) -> str:
    if status == "exact_issuer_entity_match_auto":
        return "high"
    if status == "direct_parent_candidate_review_required":
        return "medium"
    if status.startswith("ambiguous"):
        return "low"
    return "unrated"


def load_mapping_records(candidates_path: Path = CANDIDATES_PATH, mappings_path: Path = MAPPINGS_PATH) -> list[dict[str, str]]:
    """Combine automated candidate evidence with saved manual decisions."""
    if not candidates_path.exists():
        raise FileNotFoundError(f"Ticker candidate file not found: {candidates_path}")

    candidates = pd.read_csv(candidates_path, dtype="string").fillna("")
    saved = pd.read_csv(mappings_path, dtype="string").fillna("") if mappings_path.exists() else pd.DataFrame()
    saved_by_name: dict[str, dict[str, str]] = {}
    saved_by_lei: dict[str, dict[str, str]] = {}
    if not saved.empty:
        for row in saved.to_dict(orient="records"):
            if row.get("issuer_name"):
                saved_by_name[normalize_issuer_name(row["issuer_name"])] = row
            if row.get("issuer_lei"):
                saved_by_lei[row["issuer_lei"].strip()] = row

    records: list[dict[str, str]] = []
    included_names: set[str] = set()
    for candidate in candidates.to_dict(orient="records"):
        name = candidate.get("issuer_name", "").strip()
        lei = candidate.get("issuer_lei", "").strip()
        old = saved_by_lei.get(lei) or saved_by_name.get(normalize_issuer_name(name), {})
        candidate_status = candidate.get("mapping_status", "unresolved")
        auto_ticker = candidate.get("ticker_candidate", "").strip().upper()
        override = old.get("equity_ticker_override", "").strip().upper()

        # Backward compatibility with the first four-column mapping file.
        old_ticker = old.get("equity_ticker", "").strip().upper()
        old_review_status = old.get("review_status", "").strip()
        if not override and old_ticker and old_review_status == "reviewed":
            override = old_ticker
        if old_review_status == "automated_exact_entity_match" and old_ticker:
            auto_ticker = old_ticker

        effective = override or (auto_ticker if candidate_status == "exact_issuer_entity_match_auto" else "")
        mapping_status = "manual_override" if override else candidate_status
        review_status = "reviewed" if override else (
            "automated_exact_entity_match" if effective else "unresolved"
        )
        source = old.get("mapping_source", "").strip() or _candidate_source(candidate)
        record = {
            "issuer_name": name,
            "issuer_lei": lei,
            "representative_isin": candidate.get("representative_isin", ""),
            "bond_ticker": old.get("bond_ticker", "").strip().upper(),
            "automated_equity_ticker": auto_ticker,
            "equity_ticker_override": override,
            "equity_ticker": effective,
            "equity_exchange": old.get("equity_exchange", candidate.get("exchange", "")).strip(),
            "equity_country": old.get("equity_country", "").strip(),
            "mapping_source": source,
            "confidence": old.get("confidence", "").strip() or _candidate_confidence(candidate_status),
            "mapping_relationship": old.get("mapping_relationship", "").strip() or candidate.get("candidate_relationship", "issuer"),
            "candidate_status": candidate_status,
            "mapping_status": mapping_status,
            "review_status": review_status,
            "notes": old.get("notes", "").strip(),
        }
        records.append(record)
        included_names.add(normalize_issuer_name(name))

    # Preserve saved mappings if an issuer has disappeared from a refreshed candidate file.
    if not saved.empty:
        for old in saved.to_dict(orient="records"):
            name = old.get("issuer_name", "").strip()
            key = normalize_issuer_name(name)
            if not name or key in included_names:
                continue
            override = old.get("equity_ticker_override", "").strip().upper()
            old_ticker = old.get("equity_ticker", "").strip().upper()
            review_status = old.get("review_status", "").strip()
            if not override and review_status == "reviewed":
                override = old_ticker
            records.append({
                "issuer_name": name,
                "issuer_lei": old.get("issuer_lei", ""),
                "representative_isin": old.get("representative_isin", ""),
                "bond_ticker": old.get("bond_ticker", ""),
                "automated_equity_ticker": old.get("automated_equity_ticker", ""),
                "equity_ticker_override": override,
                "equity_ticker": override or old_ticker,
                "equity_exchange": old.get("equity_exchange", ""),
                "equity_country": old.get("equity_country", ""),
                "mapping_source": old.get("mapping_source", ""),
                "confidence": old.get("confidence", "unrated"),
                "mapping_relationship": old.get("mapping_relationship", "issuer"),
                "candidate_status": old.get("candidate_status", "unresolved"),
                "mapping_status": "manual_override" if override else old.get("mapping_status", "unresolved"),
                "review_status": "reviewed" if override else review_status or "unresolved",
                "notes": old.get("notes", ""),
            })

    return records


def _candidate_source(candidate: dict[str, str]) -> str:
    status = candidate.get("mapping_status", "")
    if status == "exact_issuer_entity_match_auto":
        return f"GLEIF exact entity name {candidate.get('issuer_lei', '')}; SEC CIK {candidate.get('sec_cik', '')} ({candidate.get('exchange', '')})"
    if candidate.get("candidate_relationship") == "gleif_direct_parent":
        return f"GLEIF direct parent {candidate.get('direct_parent_lei', '')}; SEC name candidate"
    if candidate.get("ticker_alternatives"):
        return "GLEIF and SEC ambiguous candidates"
    return ""


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
        frame.to_csv(temp, index=False, na_rep="")
    os.replace(temp_path, path)


class MappingWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Parallax — Issuer Ticker Mapping")
        self.resize(1600, 820)
        self.records = load_mapping_records()
        self._loading = False

        root = QWidget()
        layout = QVBoxLayout(root)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search issuer, LEI, ISIN, or ticker…")
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses")
        self.status_filter.addItems(sorted({record["mapping_status"] for record in self.records}))
        self.save_button = QPushButton("Save mappings")
        self.dirty_label = QLabel("All changes saved")
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.status_filter)
        toolbar.addWidget(self.dirty_label)
        toolbar.addWidget(self.save_button)
        layout.addLayout(toolbar)

        self.table = QTableWidget(len(self.records), len(FIELDS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        help_text = QLabel(
            "The bond ISIN and optional bond ticker identify the debt security. "
            "Equity ticker is a separate issuer or parent listing. Automated parent matches are suggestions; "
            "enter an override only when the relationship is clear."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.setCentralWidget(root)
        self._populate_table()
        self.table.cellChanged.connect(self._on_cell_changed)
        self.search.textChanged.connect(self._apply_filters)
        self.status_filter.currentTextChanged.connect(self._apply_filters)
        self.save_button.clicked.connect(self._save)
        self.statusBar().showMessage(f"Loaded {len(self.records):,} issuer mapping records from local CSV files")

    def _populate_table(self) -> None:
        self._loading = True
        for row_index, record in enumerate(self.records):
            for column, field in enumerate(FIELDS):
                item = QTableWidgetItem(str(record.get(field, "")))
                if field not in EDITABLE_FIELDS:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if field == "confidence":
                    item.setToolTip("Use high, medium, low, or unrated. This is your confidence in the issuer-to-equity link.")
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(FIELDS.index("issuer_name"), 260)
        self.table.setColumnWidth(FIELDS.index("issuer_lei"), 190)
        self.table.setColumnWidth(FIELDS.index("representative_isin"), 180)
        self.table.setColumnWidth(FIELDS.index("mapping_source"), 320)
        self.table.setColumnWidth(FIELDS.index("notes"), 240)
        self._loading = False
        self._apply_filters()

    def _on_cell_changed(self, row: int, column: int) -> None:
        if self._loading:
            return
        field = FIELDS[column]
        if field == "equity_ticker_override":
            override = self.table.item(row, column).text().strip().upper()
            self._set_cell(row, column, override)

        shared_fields = {
            "equity_ticker_override", "equity_exchange", "equity_country",
            "mapping_source", "confidence", "notes",
        }
        if field in shared_fields:
            lei_column = FIELDS.index("issuer_lei")
            name_column = FIELDS.index("issuer_name")
            row_lei = self.table.item(row, lei_column).text().strip()
            row_name = self.table.item(row, name_column).text()
            identity = ("LEI", row_lei) if row_lei else ("NAME", normalize_issuer_name(row_name))
            value = self.table.item(row, column).text()
            affected = []
            for peer in range(self.table.rowCount()):
                peer_lei = self.table.item(peer, lei_column).text().strip()
                peer_name = self.table.item(peer, name_column).text()
                peer_identity = ("LEI", peer_lei) if peer_lei else ("NAME", normalize_issuer_name(peer_name))
                if peer_identity == identity:
                    self._set_cell(peer, column, value)
                    affected.append(peer)
            if field == "equity_ticker_override":
                effective_column = FIELDS.index("equity_ticker")
                candidate_column = FIELDS.index("automated_equity_ticker")
                candidate_status_column = FIELDS.index("candidate_status")
                status_column = FIELDS.index("mapping_status")
                review_column = FIELDS.index("review_status")
                for peer in affected:
                    override = self.table.item(peer, column).text().strip().upper()
                    effective = override
                    if not effective and self.table.item(peer, candidate_status_column).text() == "exact_issuer_entity_match_auto":
                        effective = self.table.item(peer, candidate_column).text().strip().upper()
                    self._set_cell(peer, effective_column, effective)
                    self._set_cell(peer, status_column, "manual_override" if override else self.table.item(peer, candidate_status_column).text())
                    self._set_cell(peer, review_column, "reviewed" if override else ("automated_exact_entity_match" if effective else "unresolved"))
        self.dirty_label.setText("Unsaved changes")

    def _set_cell(self, row: int, column: int, text: str) -> None:
        self._loading = True
        self.table.item(row, column).setText(text)
        self._loading = False

    def _apply_filters(self) -> None:
        query = self.search.text().strip().casefold()
        status = self.status_filter.currentText()
        for row in range(self.table.rowCount()):
            row_text = " ".join(self.table.item(row, col).text() for col in range(self.table.columnCount())).casefold()
            status_text = self.table.item(row, FIELDS.index("mapping_status")).text()
            self.table.setRowHidden(row, (query not in row_text) or (status != "All statuses" and status != status_text))

    def _save(self) -> None:
        rows: list[dict[str, str]] = []
        for row in range(self.table.rowCount()):
            record = {field: self.table.item(row, col).text().strip() for col, field in enumerate(FIELDS)}
            override = record["equity_ticker_override"].upper()
            record["automated_equity_ticker"] = record["automated_equity_ticker"].upper()
            record["equity_ticker_override"] = override
            if override:
                record["equity_ticker"] = override
                record["mapping_status"] = "manual_override"
                record["review_status"] = "reviewed"
            elif record["candidate_status"] == "exact_issuer_entity_match_auto":
                record["equity_ticker"] = record["automated_equity_ticker"]
                record["mapping_status"] = record["candidate_status"]
                record["review_status"] = "automated_exact_entity_match"
            else:
                record["equity_ticker"] = ""
                record["mapping_status"] = record["candidate_status"]
                record["review_status"] = "unresolved"
            record["equity_exchange"] = record["equity_exchange"].upper()
            record["confidence"] = record["confidence"].casefold()
            if record["confidence"] not in {"high", "medium", "low", "unrated"}:
                QMessageBox.warning(self, "Invalid confidence", f"Use high, medium, low, or unrated.\n\nIssuer: {record['issuer_name']}")
                return
            rows.append(record)

        frame = pd.DataFrame(rows, columns=FIELDS)
        frame["mapping_source"] = frame["mapping_source"].fillna("")
        legacy = frame[["issuer_name", "equity_ticker", "mapping_source", "review_status"]].copy()
        # Keep the legacy columns first for compatibility with the snapshot builder.
        for field in FIELDS:
            if field not in legacy:
                legacy[field] = frame[field]
        legacy = legacy[["issuer_name", "equity_ticker", "mapping_source", "review_status", *[field for field in FIELDS if field not in {"issuer_name", "equity_ticker", "mapping_source", "review_status"}]]]
        _atomic_csv(legacy, MAPPINGS_PATH)
        self._sync_fixture(rows)

        self.records = rows
        self.dirty_label.setText("All changes saved")
        self.statusBar().showMessage(f"Saved {len(rows):,} mapping records to {MAPPINGS_PATH}", 5000)

    @staticmethod
    def _sync_fixture(records: list[dict[str, str]]) -> None:
        """Apply saved bond and equity symbols to the static snapshot."""
        fixture_path = ROOT / "data" / "snapshots" / "lqd_bonds.csv"
        if not fixture_path.exists():
            return
        fixture = pd.read_csv(fixture_path, dtype="string").fillna("")
        by_lei = {row["issuer_lei"]: row for row in records if row["issuer_lei"]}
        by_name = {normalize_issuer_name(row["issuer_name"]): row for row in records}

        def mapping_for(issuer_name: str, issuer_lei: str = "") -> dict[str, str]:
            return by_lei.get(issuer_lei, by_name.get(normalize_issuer_name(issuer_name), {}))

        if "issuer_lei" not in fixture:
            fixture["issuer_lei"] = fixture["issuer_name"].map(
                lambda name: next((row["issuer_lei"] for row in records if row["issuer_name"] == name), "")
            )
        fixture["bond_ticker"] = fixture.apply(lambda row: mapping_for(row["issuer_name"], row["issuer_lei"]).get("bond_ticker", ""), axis=1)
        fixture["equity_ticker"] = fixture.apply(lambda row: mapping_for(row["issuer_name"], row["issuer_lei"]).get("equity_ticker", ""), axis=1)
        fixture["equity_exchange"] = fixture.apply(lambda row: mapping_for(row["issuer_name"], row["issuer_lei"]).get("equity_exchange", ""), axis=1)
        fixture["equity_country"] = fixture.apply(lambda row: mapping_for(row["issuer_name"], row["issuer_lei"]).get("equity_country", ""), axis=1)
        fixture["ticker_mapping_status"] = fixture.apply(
            lambda row: "reviewed" if mapping_for(row["issuer_name"], row["issuer_lei"]).get("review_status") == "reviewed"
            else "exact_entity_match" if mapping_for(row["issuer_name"], row["issuer_lei"]).get("review_status") == "automated_exact_entity_match"
            else "unmapped"
            , axis=1
        )
        _atomic_csv(fixture, fixture_path)


def main() -> None:
    app = QApplication(sys.argv)
    try:
        window = MappingWindow()
    except Exception as error:  # Show data/schema errors in a useful window.
        QMessageBox.critical(None, "Could not load mapping files", str(error))
        raise
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
