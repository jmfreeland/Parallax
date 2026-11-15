"""Desktop tool for creating date-specific exploratory bond portfolios."""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDateEdit,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .exploratory_portfolio import (
    ExploratoryPortfolio,
    generate_exploratory_portfolio,
    load_bond_observations,
)
from .observation_chart import ObservationChart
from .portfolio_archive import save_portfolio
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


class PortfolioBuilderWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Exploratory Portfolio Builder")
        self.resize(1540, 820)
        self.portfolio: ExploratoryPortfolio | None = None
        self.output_rows: list[dict[str, Any]] = []

        central = QWidget()
        layout = QVBoxLayout(central)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Bond list CSV"))
        self.bond_list = QLineEdit(str(DEFAULT_SNAPSHOT))
        source_row.addWidget(self.bond_list, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_bond_list)
        source_row.addWidget(browse)
        layout.addLayout(source_row)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Portfolio date"))
        self.as_of_date = QDateEdit(calendarPopup=True)
        self.as_of_date.setDisplayFormat("yyyy-MM-dd")
        self.as_of_date.setDate(QDate.currentDate().addDays(-1))
        controls.addWidget(self.as_of_date)
        controls.addWidget(QLabel("Weighting"))
        self.weighting = QComboBox()
        self.weighting.addItems(["Equal weight", "Inverse price-return variance"])
        self.weighting.currentTextChanged.connect(self._update_method_controls)
        controls.addWidget(self.weighting)
        controls.addWidget(QLabel("Estimation window (days)"))
        self.window_days = QSpinBox()
        self.window_days.setRange(30, 3650)
        self.window_days.setValue(365)
        controls.addWidget(self.window_days)
        controls.addWidget(QLabel("Minimum returns"))
        self.minimum_returns = QSpinBox()
        self.minimum_returns.setRange(2, 1000)
        self.minimum_returns.setValue(20)
        controls.addWidget(self.minimum_returns)
        self.require_yield = QCheckBox("Require valid yield by date")
        self.require_yield.setToolTip(
            "Filter out bonds with valid price history but no valid TRACE yield on or before the portfolio date."
        )
        controls.addWidget(self.require_yield)
        self.generate_button = QPushButton("Generate portfolio")
        self.generate_button.clicked.connect(self._generate)
        controls.addWidget(self.generate_button)
        self.export_button = QPushButton("Export CSV...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        self.archive_button = QPushButton("Save to archive")
        self.archive_button.setEnabled(False)
        self.archive_button.clicked.connect(self._save_to_archive)
        controls.addWidget(self.archive_button)
        layout.addLayout(controls)

        self.summary = QLabel(
            f"TRACE database: {DATABASE_PATH} | Choose a date and weighting method to create a portfolio."
        )
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(
            [
                "ISIN",
                "Issuer",
                "Sector",
                "Maturity",
                "History began",
                "Latest valid observation",
                "Latest price",
                "Price date",
                "Latest yield (%)",
                "Yield date",
                "Price-return obs.",
                "Price-return variance",
                "Weight",
            ]
        )
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self._selection_changed)
        self.chart = ObservationChart()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.chart)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #0b1220;
                color: #e2e8f0;
            }
            QLineEdit, QDateEdit, QComboBox, QSpinBox, QTableWidget {
                background-color: #111827;
                color: #e2e8f0;
                border: 1px solid #334155;
                selection-background-color: #1d4ed8;
                selection-color: #ffffff;
            }
            QComboBox QAbstractItemView {
                background-color: #111827;
                color: #e2e8f0;
                selection-background-color: #1d4ed8;
            }
            QHeaderView::section {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                padding: 5px;
            }
            QTableWidget {
                gridline-color: #334155;
                alternate-background-color: #0f172a;
            }
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #475569;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
            QPushButton:disabled {
                color: #64748b;
                background-color: #111827;
            }
            QSplitter::handle {
                background-color: #334155;
            }
            """
        )
        self._update_method_controls()

    def _browse_bond_list(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select bond list",
            self.bond_list.text(),
            "CSV files (*.csv)",
        )
        if selected:
            self.bond_list.setText(selected)

    def _update_method_controls(self) -> None:
        inverse_variance = self.weighting.currentText() == "Inverse price-return variance"
        self.window_days.setEnabled(inverse_variance)
        self.minimum_returns.setEnabled(inverse_variance)

    def _generate(self) -> None:
        try:
            target_date = date.fromisoformat(self.as_of_date.date().toString("yyyy-MM-dd"))
            portfolio = generate_exploratory_portfolio(
                as_of_date=target_date,
                bond_list_path=Path(self.bond_list.text()).expanduser(),
                database_path=DATABASE_PATH,
                weighting_method=self.weighting.currentText(),
                estimation_window_days=self.window_days.value(),
                minimum_return_observations=self.minimum_returns.value(),
                require_valid_yield=self.require_yield.isChecked(),
            )
        except Exception as error:
            QMessageBox.warning(self, "Could not generate portfolio", str(error))
            return

        self.portfolio = portfolio
        self.output_rows = list(portfolio.rows)
        self._populate_table()
        self.export_button.setEnabled(True)
        self.archive_button.setEnabled(True)
        details = (
            f"As of {portfolio.as_of_date}: {portfolio.universe_count:,} bonds with valid history "
            f"and not yet matured; {portfolio.position_count:,} positions included; "
            f"{portfolio.matured_count:,} matured bonds excluded; "
            f"{portfolio.missing_yield_count:,} eligible bonds had no valid yield"
            f"{f' ({portfolio.excluded_no_yield:,} filtered out)' if self.require_yield.isChecked() else ''}; "
            f"{portfolio.excluded_by_method:,} excluded by the weighting method. "
            f"Weights sum to 100%."
        )
        if portfolio.weighting_method == "Inverse price-return variance":
            details += (
                f" Variance uses consecutive observed log clean-price changes over the last "
                f"{portfolio.estimation_window_days} calendar days. This is a price-only "
                "exploratory estimate; observations may be irregular."
            )
        self.summary.setText(f"TRACE database: {DATABASE_PATH}\n{details}")

    def _save_to_archive(self) -> None:
        if self.portfolio is None:
            return
        try:
            path = save_portfolio(
                self.portfolio,
                bond_list_path=Path(self.bond_list.text()),
                minimum_return_observations=self.minimum_returns.value(),
                require_valid_yield=self.require_yield.isChecked(),
            )
        except OSError as error:
            QMessageBox.warning(self, "Could not archive portfolio", str(error))
            return
        QMessageBox.information(self, "Portfolio archived", f"Saved portfolio archive:\n{path}")

    def _populate_table(self) -> None:
        if self.portfolio is None:
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.output_rows))
        columns = (
            "isin",
            "issuer_name",
            "sector",
            "maturity_date",
            "history_started_date",
            "latest_valid_observation_date",
            "latest_price_per_100",
            "latest_price_date",
            "latest_yield_pct",
            "latest_yield_date",
            "price_return_observations",
            "price_return_variance",
            "weight",
        )
        for row_index, row in enumerate(self.output_rows):
            for column_index, key in enumerate(columns):
                value = row.get(key)
                if isinstance(value, date):
                    display = value.isoformat()
                elif value is None:
                    display = ""
                elif isinstance(value, float):
                    display = f"{value:.8f}" if key.endswith("variance") else f"{value:.6f}"
                else:
                    display = str(value)
                item = QTableWidgetItem(display)
                if key == "weight":
                    item.setText(f"{float(value):.4%}")
                self.table.setItem(row_index, column_index, item)
        self.table.setSortingEnabled(True)
        if self.output_rows:
            self.table.setCurrentCell(0, 0)
            self._selection_changed(0, 0, -1, -1)

    def _selection_changed(
        self,
        current_row: int,
        _current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        if current_row < 0 or self.portfolio is None:
            self.chart.set_message("Generate a portfolio, then select a bond to inspect its observations.")
            return
        isin_item = self.table.item(current_row, 0)
        if isin_item is None:
            self.chart.set_message("Select a bond to view its TRACE observations.")
            return
        isin = isin_item.text()
        try:
            observations = load_bond_observations(
                database_path=DATABASE_PATH,
                isin=isin,
                as_of_date=self.portfolio.as_of_date,
                window_days=90,
            )
        except Exception as error:
            self.chart.set_message(f"Could not load observations: {error}")
            return
        self.chart.set_data(isin, self.portfolio.as_of_date, observations)

    def _export(self) -> None:
        if not self.output_rows or self.portfolio is None:
            return
        suggested = Path(self.bond_list.text()).with_name(
            f"portfolio_{self.portfolio.as_of_date}_{self.portfolio.weighting_method.lower().replace(' ', '_')}.csv"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Export exploratory portfolio",
            str(suggested),
            "CSV files (*.csv)",
        )
        if not selected:
            return
        fieldnames = list(self.output_rows[0])
        try:
            with Path(selected).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in self.output_rows:
                    writer.writerow(
                        {
                            key: value.isoformat() if isinstance(value, date) else value
                            for key, value in row.items()
                        }
                    )
        except OSError as error:
            QMessageBox.warning(self, "Could not export portfolio", str(error))
            return
        self.summary.setText(f"{self.summary.text()}\nExported: {selected}")


def main() -> None:
    app = QApplication(sys.argv)
    window = PortfolioBuilderWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
