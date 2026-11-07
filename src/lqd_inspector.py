"""Browse the latest iShares LQD holdings in a searchable desktop table."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from PySide6.QtCore import QSortFilterProxyModel, QThread, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .lqd_holdings import get_lqd_holdings


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "data" / "snapshots" / "lqd_bonds.csv"


class HoldingsLoader(QThread):
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, source: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = source

    def run(self) -> None:
        try:
            if self.source == "snapshot":
                if not SNAPSHOT_PATH.exists():
                    raise FileNotFoundError(f"Build a snapshot first; file not found: {SNAPSHOT_PATH}")
                holdings = pd.read_csv(SNAPSHOT_PATH, dtype="string").fillna("")
                as_of_date = str(holdings["source_as_of_date"].iloc[0]) if not holdings.empty else "Date unavailable"
            else:
                holdings = get_lqd_holdings()
                as_of_date = str(holdings.attrs.get("source_as_of_date") or "Date unavailable")
            self.loaded.emit(holdings, as_of_date)
        except Exception as error:
            self.failed.emit(str(error))


class HoldingsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LQD Holdings")
        self.resize(1200, 760)
        self.loader: HoldingsLoader | None = None

        central = QWidget()
        layout = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItem("Latest iShares holdings", "latest")
        self.source.addItem("Built bond snapshot", "snapshot")
        self.source.currentIndexChanged.connect(lambda _index: self.refresh())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search holdings...")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.source)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.summary = QLabel("Loading latest LQD holdings...")
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.table.setModel(self.proxy)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        layout.addWidget(self.table, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.refresh()

    def refresh(self) -> None:
        if self.loader and self.loader.isRunning():
            return
        self.refresh_button.setEnabled(False)
        source = self.source.currentData()
        message = "Loading built bond snapshot..." if source == "snapshot" else "Downloading current iShares holdings..."
        self.statusBar().showMessage(message)
        self.loader = HoldingsLoader(str(source), self)
        self.loader.loaded.connect(self._show_holdings)
        self.loader.failed.connect(self._show_error)
        self.loader.finished.connect(lambda: self.refresh_button.setEnabled(True))
        self.loader.start()

    def _show_holdings(self, holdings: pd.DataFrame, as_of_date: str) -> None:
        self.model.clear()
        self.model.setHorizontalHeaderLabels([str(column) for column in holdings.columns])
        for row in holdings.itertuples(index=False, name=None):
            items = [QStandardItem("" if pd.isna(value) else str(value)) for value in row]
            for item in items:
                item.setEditable(False)
            self.model.appendRow(items)

        asset_class = next(
            (column for column in holdings.columns if str(column).strip().casefold() == "asset class"),
            None,
        )
        if asset_class is not None:
            bond_count = int(
                holdings[asset_class].fillna("").astype(str).str.strip().str.casefold().eq("fixed income").sum()
            )
        else:
            bond_count = len(holdings)
        source_label = "Snapshot holdings date" if self.source.currentData() == "snapshot" else "Holdings date"
        summary = f"{source_label}: {as_of_date} | {bond_count:,} bonds | {len(holdings):,} rows"
        if self.source.currentData() == "snapshot":
            normalized_columns = {str(column).strip().casefold(): column for column in holdings.columns}
            bond_ticker_column = normalized_columns.get("bond_ticker")
            equity_ticker_column = normalized_columns.get("equity_ticker")
            if bond_ticker_column and equity_ticker_column:
                has_bond_ticker = holdings[bond_ticker_column].fillna("").astype(str).str.strip().ne("")
                has_equity_ticker = holdings[equity_ticker_column].fillna("").astype(str).str.strip().ne("")
                summary += (
                    f" | Bond ticker: {int(has_bond_ticker.sum()):,}"
                    f" | Equity ticker: {int(has_equity_ticker.sum()):,}"
                    f" | Both: {int((has_bond_ticker & has_equity_ticker).sum()):,}"
                )
        self.summary.setText(summary)
        self.table.resizeColumnsToContents()
        self.statusBar().showMessage(f"Loaded {len(holdings):,} holdings")

    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage("Could not load holdings")
        QMessageBox.warning(self, "Holdings unavailable", message)


def main() -> None:
    app = QApplication(sys.argv)
    window = HoldingsWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
