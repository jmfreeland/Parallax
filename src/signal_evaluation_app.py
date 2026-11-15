"""Desktop point-in-time evaluation for the Nelson-Siegel residual signal."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .signal_evaluation import evaluate_nelson_siegel_signal
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


class EvaluationWorker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, parameters: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.parameters = parameters

    def run(self) -> None:
        try:
            result = evaluate_nelson_siegel_signal(
                **self.parameters,
                on_progress=self.progress.emit,
            )
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


class SignalEvaluationWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Signal Evaluation - Nelson-Siegel Residual")
        self.resize(1500, 900)
        self.worker: EvaluationWorker | None = None
        self.result: dict[str, Any] | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        source = QHBoxLayout()
        source.addWidget(QLabel("Bond list"))
        self.bond_list = QLineEdit(str(DEFAULT_SNAPSHOT))
        source.addWidget(self.bond_list, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        source.addWidget(browse)
        layout.addLayout(source)

        row_one = QHBoxLayout()
        row_one.addWidget(QLabel("Test start"))
        self.start_date = QDateEdit(calendarPopup=True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate().addDays(-90))
        row_one.addWidget(self.start_date)
        row_one.addWidget(QLabel("Test end"))
        self.end_date = QDateEdit(calendarPopup=True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate.currentDate().addDays(-1))
        row_one.addWidget(self.end_date)
        row_one.addWidget(QLabel("Horizon (calendar days)"))
        self.horizon_days = QSpinBox()
        self.horizon_days.setRange(2, 365)
        self.horizon_days.setValue(21)
        row_one.addWidget(self.horizon_days)
        row_one.addWidget(QLabel("Minimum bonds per ticker"))
        self.minimum_bonds = QSpinBox()
        self.minimum_bonds.setRange(5, 100)
        self.minimum_bonds.setValue(5)
        row_one.addWidget(self.minimum_bonds)
        layout.addLayout(row_one)

        row_two = QHBoxLayout()
        row_two.addWidget(QLabel("Max signal age (days)"))
        self.signal_age = QSpinBox()
        self.signal_age.setRange(0, 365)
        self.signal_age.setValue(30)
        row_two.addWidget(self.signal_age)
        row_two.addWidget(QLabel("Max T+1 observation delay (days)"))
        self.forward_start_delay = QSpinBox()
        self.forward_start_delay.setRange(0, 90)
        self.forward_start_delay.setValue(7)
        row_two.addWidget(self.forward_start_delay)
        row_two.addWidget(QLabel("Max horizon-end observation age (days)"))
        self.end_age = QSpinBox()
        self.end_age.setRange(0, 90)
        self.end_age.setValue(14)
        row_two.addWidget(self.end_age)
        row_two.addWidget(QLabel("Max TRACE price age (days)"))
        self.price_age = QSpinBox()
        self.price_age.setRange(0, 90)
        self.price_age.setValue(7)
        row_two.addWidget(self.price_age)
        row_two.addWidget(QLabel("Long/short tail quantile"))
        self.quantile = QDoubleSpinBox()
        self.quantile.setRange(0.05, 0.45)
        self.quantile.setSingleStep(0.05)
        self.quantile.setValue(0.20)
        self.quantile.setDecimals(2)
        row_two.addWidget(self.quantile)
        self.run_button = QPushButton("Run evaluation")
        self.run_button.clicked.connect(self._run_evaluation)
        row_two.addWidget(self.run_button)
        self.export_button = QPushButton("Export bond observations...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        row_two.addWidget(self.export_button)
        self.export_report_button = QPushButton("Export date report...")
        self.export_report_button.setEnabled(False)
        self.export_report_button.clicked.connect(self._export_report)
        row_two.addWidget(self.export_report_button)
        row_two.addStretch(1)
        layout.addLayout(row_two)

        self.status = QLabel(f"Signal: Nelson-Siegel ticker residual | TRACE database: {DATABASE_PATH}")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        dates_page = QWidget()
        dates_layout = QVBoxLayout(dates_page)
        self.summary = QLabel(
            "Select a test range and run. Positive residual means high yield versus the issuer's fitted curve. "
            "Forward yield change is a spread proxy (no Treasury benchmark); long-short return is a duration-adjusted, "
            "yield-only price proxy that excludes carry and costs. TRACE observations are not stored in immutable vintages, "
            "so later revisions to past observations may affect historical results."
        )
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dates_layout.addWidget(self.summary)
        self.date_table = QTableWidget(0, 11)
        self.date_table.setHorizontalHeaderLabels([
            "Signal date", "Horizon end", "Bond pairs", "Ticker curves", "Pearson IC",
            "Spearman IC", "Long positions", "Short positions", "L/S total return (%)",
            "Return coverage (%)", "Yield-only proxy (bps)",
        ])
        self.date_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.date_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.date_table.horizontalHeader().setStretchLastSection(True)
        self.date_table.currentCellChanged.connect(self._show_date_details)
        dates_layout.addWidget(self.date_table, 1)
        self.tabs.addTab(dates_page, "Date performance")

        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        self.detail_status = QLabel("Select a signal date to inspect the included bond observations and strategy legs.")
        details_layout.addWidget(self.detail_status)
        self.detail_table = QTableWidget(0, 16)
        self.detail_table.setHorizontalHeaderLabels([
            "Signal date", "Ticker", "ISIN", "Signal residual (pp)", "Signal yield date",
            "T+1 yield date", "T+end yield date", "Yield change (bps)",
            "Duration (years)", "Yield-only proxy (bps)", "Strategy leg", "Bond total return (%)",
            "Return status", "Forward days", "Sector", "Issuer",
        ])
        self.detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        details_layout.addWidget(self.detail_table)
        self.tabs.addTab(details_page, "Bond observations")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Select bond list", self.bond_list.text(), "CSV files (*.csv)"
        )
        if selected:
            self.bond_list.setText(selected)

    def _run_evaluation(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        start = date.fromisoformat(self.start_date.date().toString("yyyy-MM-dd"))
        end = date.fromisoformat(self.end_date.date().toString("yyyy-MM-dd"))
        if end < start:
            QMessageBox.warning(self, "Invalid date range", "Test end must be on or after test start.")
            return
        parameters = {
            "start_date": start,
            "end_date": end,
            "horizon_days": self.horizon_days.value(),
            "bond_list_path": Path(self.bond_list.text()).expanduser(),
            "database_path": DATABASE_PATH,
            "minimum_bonds": self.minimum_bonds.value(),
            "maximum_signal_age_days": self.signal_age.value(),
            "maximum_forward_start_delay_days": self.forward_start_delay.value(),
            "maximum_horizon_end_age_days": self.end_age.value(),
            "maximum_price_age_days": self.price_age.value(),
            "long_short_quantile": self.quantile.value(),
        }
        self.run_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.export_report_button.setEnabled(False)
        self.result = None
        self.date_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.status.setText("Building point-in-time curves and evaluating forward observations...")
        self.progress.setRange(0, 0)
        self.worker = EvaluationWorker(parameters, self)
        self.worker.progress.connect(self._progress)
        self.worker.succeeded.connect(self._show_results)
        self.worker.failed.connect(self._show_error)
        self.worker.finished.connect(lambda: self.run_button.setEnabled(True))
        self.worker.start()

    def _progress(self, completed: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(completed)
        self.status.setText(f"Evaluating signal dates: {completed:,}/{total:,}")

    def _show_results(self, result: dict[str, Any]) -> None:
        self.result = result
        date_rows = result["date_results"]
        detail_rows = result["bond_results"]
        self._fill_table(
            self.date_table,
            date_rows,
            (
                "as_of_date", "horizon_end_date", "signal_bonds_with_forward_data", "ticker_curves_fitted",
                "pearson_signal_forward_yield_change", "spearman_signal_forward_yield_change",
                "long_positions", "short_positions", "long_short_total_return_pct",
                "long_short_total_return_coverage_pct", "long_short_duration_adjusted_return_proxy_bps",
            ),
        )
        if date_rows:
            self.date_table.setCurrentCell(0, 0)
            self._show_date_details(0, 0, -1, -1)
        else:
            self.detail_table.setRowCount(0)
        summary = result["summary"]

        def display(value: Any, suffix: str = "") -> str:
            return "n/a" if value is None else f"{float(value):.3f}{suffix}"

        self.summary.setText(
            f"{summary['signal_name']} | {summary['test_start_date']} to {summary['test_end_date']} | "
            f"T+1 to T+{summary['horizon_days']} calendar days. "
            f"Mean daily Pearson/Spearman correlation with forward yield-change proxy: "
            f"{display(summary['mean_daily_pearson_correlation'])} / "
            f"{display(summary['mean_daily_spearman_correlation'])}. "
            f"Mean daily gross long-short duration-adjusted return proxy: "
            f"{display(summary['mean_daily_long_short_return_proxy_bps'], ' bps')}; "
            f"mean actual total-return long-short: {display(summary['mean_daily_long_short_total_return_pct'], '%')} "
            f"on {summary['dates_with_total_return']} fully priced dates (mean leg coverage "
            f"{display(summary['mean_long_short_total_return_coverage_pct'], '%')}; positive on "
            f"{display(summary['total_return_positive_date_fraction'] * 100 if summary['total_return_positive_date_fraction'] is not None else None, '%')}). "
            "Yield changes are only a spread proxy because no Treasury benchmark is available. "
            "The yield-only proxy excludes carry, convexity, and costs; total returns use the current synthetic coupon schedule and exclude costs and defaults. "
            "Forward windows overlap. Past TRACE revisions and the current bond-list snapshot may affect this retrospective test."
        )
        self.status.setText(
            f"Completed {summary['signal_dates']:,} signal dates; "
            f"{summary['dates_with_correlation']:,} dates had enough pairs for correlation."
        )
        self.export_button.setEnabled(bool(detail_rows))
        self.export_report_button.setEnabled(bool(date_rows))

    def _show_date_details(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        if self.result is None or row < 0 or row >= len(self.result["date_results"]):
            self.detail_table.setRowCount(0)
            return
        as_of = self.result["date_results"][row]["as_of_date"]
        details = [item for item in self.result["bond_results"] if item["as_of_date"] == as_of]
        self.detail_status.setText(f"{as_of}: {len(details):,} bond-date observations; selected strategy legs are labelled Long/Short.")
        self._fill_table(
            self.detail_table,
            details,
            (
                "as_of_date", "ticker", "isin", "signal_value_pct", "yield_date",
                "forward_start_yield_date", "horizon_end_yield_date", "forward_yield_change_bps",
                "modified_duration_years", "yield_duration_return_proxy_bps", "strategy_leg",
                "bond_total_return_pct", "bond_total_return_status", "forward_window_days", "sector", "issuer_name",
            ),
        )

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, key in enumerate(fields):
                value = row.get(key)
                if value is None:
                    display = ""
                elif isinstance(value, float):
                    display = f"{value:.4f}"
                else:
                    display = str(value)
                table.setItem(row_index, column, QTableWidgetItem(display))
        table.resizeColumnsToContents()

    def _show_error(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText(f"Evaluation failed: {message}")
        QMessageBox.warning(self, "Could not evaluate signal", message)

    def _export(self) -> None:
        if self.result is None or not self.result["bond_results"]:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export signal evaluation observations",
            "nelson_siegel_signal_observations.csv",
            "CSV files (*.csv)",
        )
        if not path:
            return
        rows = self.result["bond_results"]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        try:
            with Path(path).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
        except OSError as error:
            QMessageBox.warning(self, "Could not export observations", str(error))
            return
        self.status.setText(f"Exported {len(rows):,} bond-date observations to {path}")

    def _export_report(self) -> None:
        if self.result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export signal evaluation date report",
            "nelson_siegel_signal_report.json",
            "JSON files (*.json)",
        )
        if not path:
            return
        report = {"summary": self.result["summary"], "date_results": self.result["date_results"]}
        try:
            Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Could not export report", str(error))
            return
        self.status.setText(f"Exported date-level signal report to {path}")


def main() -> None:
    app = QApplication(sys.argv)
    window = SignalEvaluationWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
