"""Desktop analyzer for single-bond total and financing-excess returns."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bond_return_analysis import analyze_bond_return_series, financing_rate_columns
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


class BondReturnWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, parameters: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.parameters = parameters

    def run(self) -> None:
        try:
            self.succeeded.emit(analyze_bond_return_series(**self.parameters))
        except Exception as error:
            self.failed.emit(str(error))


class ReturnSeriesChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(340)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        if len(self.rows) < 2:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Run an analysis to plot return indices.")
            painter.end()
            return
        series = (
            ("cumulative_bond_index", "Bond total return", "#38bdf8"),
            ("cumulative_financing_index", "Financing", "#fbbf24"),
            ("cumulative_excess_index", "Log excess return", "#34d399"),
        )
        left, right = 82.0, float(self.width() - 24)
        top, bottom = 40.0, float(self.height() - 56)
        values = [float(row[key]) for row in self.rows for key, _, _ in series]
        low, high = min(values), max(values)
        pad = max((high - low) * 0.10, 0.25)
        low, high = low - pad, high + pad
        if high <= low:
            high = low + 1.0
        x_map = lambda index: left + index / (len(self.rows) - 1) * (right - left)
        y_map = lambda value: bottom - (value - low) / (high - low) * (bottom - top)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 22), Qt.AlignmentFlag.AlignLeft,
                         "Growth of 100 | bond, financing and excess indices")
        for step in range(5):
            value = low + step * (high - low) / 4
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 70, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{value:.2f}")
        for key, _, color in series:
            path = QPainterPath()
            for index, row in enumerate(self.rows):
                point = QPointF(x_map(index), y_map(float(row[key])))
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            painter.setPen(QPen(QColor(color), 2.2))
            painter.drawPath(path)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, bottom + 6, right - left, 18), Qt.AlignmentFlag.AlignCenter,
                         f"{self.rows[0]['date']} to {self.rows[-1]['date']}")
        legend_x = left
        for _, label, color in series:
            painter.setPen(QPen(QColor(color), 2.2))
            painter.drawLine(QPointF(legend_x, self.height() - 14), QPointF(legend_x + 18, self.height() - 14))
            painter.setPen(QColor("#e2e8f0"))
            painter.drawText(QRectF(legend_x + 24, self.height() - 22, 160, 16), Qt.AlignmentFlag.AlignLeft, label)
            legend_x += 195
        painter.end()


class BondReturnAnalysisWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bond Total Return and Financing Excess")
        self.resize(1500, 940)
        self.result: dict[str, Any] | None = None
        self.worker: BondReturnWorker | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        bond_source = QHBoxLayout()
        bond_source.addWidget(QLabel("Bond list CSV"))
        self.bond_list = QLineEdit(str(DEFAULT_SNAPSHOT))
        self.bond_list.textChanged.connect(self._refresh_bonds)
        bond_source.addWidget(self.bond_list, 1)
        browse_bonds = QPushButton("Browse...")
        browse_bonds.clicked.connect(self._browse_bond_list)
        bond_source.addWidget(browse_bonds)
        layout.addLayout(bond_source)

        rate_source = QHBoxLayout()
        rate_source.addWidget(QLabel("Short-rate history CSV"))
        self.rate_file = QLineEdit()
        self.rate_file.textChanged.connect(self._refresh_rate_columns)
        rate_source.addWidget(self.rate_file, 1)
        browse_rates = QPushButton("Browse...")
        browse_rates.clicked.connect(self._browse_rate_file)
        rate_source.addWidget(browse_rates)
        rate_source.addWidget(QLabel("Rate column (%)"))
        self.rate_column = QComboBox()
        self.rate_column.setMinimumWidth(130)
        rate_source.addWidget(self.rate_column)
        layout.addLayout(rate_source)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("ISIN"))
        self.isin = QComboBox()
        self.isin.setEditable(True)
        self.isin.setMinimumWidth(310)
        controls.addWidget(self.isin)
        controls.addWidget(QLabel("Start"))
        self.start_date = QDateEdit(calendarPopup=True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate().addYears(-1))
        controls.addWidget(self.start_date)
        controls.addWidget(QLabel("End"))
        self.end_date = QDateEdit(calendarPopup=True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate.currentDate().addDays(-1))
        controls.addWidget(self.end_date)
        controls.addWidget(QLabel("Financing spread (bp)"))
        self.spread = QDoubleSpinBox()
        self.spread.setRange(-5000.0, 10000.0)
        self.spread.setDecimals(1)
        self.spread.setSingleStep(5.0)
        controls.addWidget(self.spread)
        controls.addWidget(QLabel("Max initial price age (days)"))
        self.price_age = QSpinBox()
        self.price_age.setRange(0, 365)
        self.price_age.setValue(30)
        controls.addWidget(self.price_age)
        controls.addWidget(QLabel("Max rate age (days)"))
        self.rate_age = QSpinBox()
        self.rate_age.setRange(0, 90)
        self.rate_age.setValue(7)
        controls.addWidget(self.rate_age)
        self.run_button = QPushButton("Analyze return series")
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        layout.addWidget(QLabel(
            "Financing rate = latest short rate known at the start of each interval + spread. "
            "Excess log return = log(1 + bond total return) − log(1 + financing accrual), ACT/365."
        ))
        self.summary = QLabel("Select a bond, dates, a short-rate CSV and rate column, then run the analysis.")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.status = QLabel("Ready")
        layout.addWidget(self.status)
        self.chart = ReturnSeriesChart()
        layout.addWidget(self.chart, 2)
        self.table = QTableWidget(0, 15)
        self.table.setHorizontalHeaderLabels([
            "Date", "Price mark date", "Price age (days)", "Clean price", "Period days",
            "Bond return (%)", "Rate observation date", "Rate age (days)", "Base short rate (%)",
            "Financing rate (%)", "Financing log return", "Bond log return", "Excess log return",
            "Cumulative excess index", "Mark status",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 3)
        actions = QHBoxLayout()
        self.export_button = QPushButton("Export time series CSV")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_csv)
        actions.addWidget(self.export_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.setCentralWidget(central)
        self._refresh_bonds()

    def _browse_bond_list(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select bond list", self.bond_list.text(), "CSV files (*.csv)")
        if selected:
            self.bond_list.setText(selected)

    def _browse_rate_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select short-rate history", "", "CSV files (*.csv)")
        if selected:
            self.rate_file.setText(selected)

    def _refresh_bonds(self, *_args: Any) -> None:
        selected = self.isin.currentText() if hasattr(self, "isin") else ""
        entries: list[tuple[str, str]] = []
        try:
            with Path(self.bond_list.text()).expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    isin = (row.get("isin") or "").strip().upper()
                    if isin:
                        label = f"{isin} | {(row.get('issuer_name') or '').strip()}"
                        entries.append((isin, label))
        except OSError:
            entries = []
        if not hasattr(self, "isin"):
            return
        self.isin.blockSignals(True)
        self.isin.clear()
        for isin, label in entries:
            self.isin.addItem(label, isin)
        index = self.isin.findData(selected)
        if index >= 0:
            self.isin.setCurrentIndex(index)
        self.isin.blockSignals(False)

    def _refresh_rate_columns(self, *_args: Any) -> None:
        selected = self.rate_column.currentText() if hasattr(self, "rate_column") else ""
        try:
            _, columns = financing_rate_columns(Path(self.rate_file.text()).expanduser())
        except (OSError, ValueError):
            columns = ()
        if not hasattr(self, "rate_column"):
            return
        self.rate_column.blockSignals(True)
        self.rate_column.clear()
        self.rate_column.addItems(columns)
        if selected in columns:
            self.rate_column.setCurrentText(selected)
        self.rate_column.blockSignals(False)

    def _run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        start = date.fromisoformat(self.start_date.date().toString("yyyy-MM-dd"))
        end = date.fromisoformat(self.end_date.date().toString("yyyy-MM-dd"))
        if end <= start:
            QMessageBox.warning(self, "Invalid date range", "End date must be after start date.")
            return
        selected_isin = self.isin.currentData()
        if not selected_isin:
            selected_isin = self.isin.currentText().split("|", 1)[0].strip().upper()
        if not selected_isin or not self.rate_column.currentText():
            QMessageBox.warning(self, "Missing input", "Select an ISIN and a financing rate column.")
            return
        self.run_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.table.setRowCount(0)
        self.chart.set_rows([])
        self.status.setText("Calculating business-day total and financing-excess returns...")
        parameters = {
            "isin": selected_isin,
            "start_date": start,
            "end_date": end,
            "financing_rate_csv_path": Path(self.rate_file.text()).expanduser(),
            "financing_rate_column": self.rate_column.currentText(),
            "financing_spread_bps": self.spread.value(),
            "maximum_price_age_days": self.price_age.value(),
            "maximum_financing_rate_age_days": self.rate_age.value(),
            "bond_list_path": Path(self.bond_list.text()).expanduser(),
            "database_path": DATABASE_PATH,
        }
        self.worker = BondReturnWorker(parameters, self)
        self.worker.succeeded.connect(self._show_results)
        self.worker.failed.connect(self._show_error)
        self.worker.finished.connect(lambda: self.run_button.setEnabled(True))
        self.worker.start()

    @staticmethod
    def _display(value: Any, precision: int = 4) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.{precision}f}"
        return str(value)

    def _show_results(self, result: dict[str, Any]) -> None:
        self.result = result
        summary = result["summary"]
        def format_optional(value: Any, suffix: str = "") -> str:
            return f"{float(value):.3f}{suffix}" if value is not None else "n/a"

        self.summary.setText(
            f"{summary['isin']} | {summary['issuer_name']} | {summary['requested_start_date']} to "
            f"{summary['effective_end_date']} | Bond return {format_optional(summary['cumulative_bond_return_pct'], '%')} | "
            f"Financing return {format_optional(summary['cumulative_financing_return_pct'], '%')} | "
            f"Cumulative excess return {format_optional(summary['cumulative_excess_return_pct'], '%')} | "
            f"Annualized excess {format_optional(summary['annualized_log_excess_return_pct'], '%')} | "
            f"Excess volatility {format_optional(summary['annualized_excess_volatility_pct'], '%')} | "
            f"Excess Sharpe {format_optional(summary['excess_sharpe_ratio'])} | "
            f"Spread {summary['financing_spread_bps']:.1f} bp over {summary['financing_rate_column']}"
        )
        self.chart.set_rows(result["time_series"])
        fields = (
            "date", "price_observation_date", "price_age_days", "clean_price_per_100", "period_days",
            "bond_total_return_pct", "financing_rate_observation_date", "financing_rate_age_days",
            "base_short_rate_pct", "financing_rate_pct", "financing_log_return", "bond_log_return",
            "log_excess_return", "cumulative_excess_index", "mark_status",
        )
        rows = result["time_series"]
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, field in enumerate(fields):
                item = QTableWidgetItem(self._display(row.get(field), 6 if "return" in field else 4))
                if field == "date":
                    item.setToolTip(row.get("mark_status", ""))
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        self.status.setText(
            f"{len(rows) - 1:,} business-day intervals | prices carried forward between TRACE prints; "
            f"stale marks are flagged in the table. Coupon dates are estimated from maturity; chained total-return "
            f"indices compound interval returns."
        )
        self.export_button.setEnabled(bool(rows))

    def _show_error(self, message: str) -> None:
        self.status.setText(f"Analysis failed: {message}")
        QMessageBox.warning(self, "Could not analyze bond returns", message)

    def _export_csv(self) -> None:
        if self.result is None:
            return
        default_name = f"{self.result['summary']['isin']}_return_series.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export bond return time series", default_name, "CSV files (*.csv)")
        if not path:
            return
        try:
            rows = self.result["time_series"]
            fields = list(dict.fromkeys(key for row in rows for key in row))
            with Path(path).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            Path(path).with_suffix(".json").write_text(
                json.dumps(self.result["summary"], indent=2), encoding="utf-8"
            )
        except OSError as error:
            QMessageBox.warning(self, "Could not export return series", str(error))
            return
        self.status.setText(f"Exported return series CSV and summary JSON to {path}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = BondReturnAnalysisWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
