"""Desktop view for ticker-level Nelson-Siegel relative-value fits."""

from __future__ import annotations

import csv
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateEdit, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .nelson_siegel import fit_ticker_curves
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT


class NelsonSiegelExplorer(QWidget):
    """Draw the fitted curve and bond residuals for one issuer ticker."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumSize(760, 560)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        if not self.rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Fit curves, then select a ticker to explore it.")
            painter.end()
            return

        rows = sorted(self.rows, key=lambda row: float(row["maturity_years"]))
        xs = [float(row["maturity_years"]) for row in rows]
        observed = [float(row["observed_yield_pct"]) for row in rows]
        fitted = [float(row["fitted_yield_pct"]) for row in rows]
        residuals = [float(row["yield_residual_pct"]) for row in rows]
        maturities = [float(row["maturity_years"]) for row in rows]
        x_min, x_max = min(xs), max(xs)
        if x_max <= x_min:
            x_max = x_min + 1.0
        y_min, y_max = min(observed + fitted), max(observed + fitted)
        y_pad = max(0.12, (y_max - y_min) * 0.18)
        y_min, y_max = y_min - y_pad, y_max + y_pad
        residual_max = max(max(abs(value) for value in residuals) * 1.25, 0.05)

        left, right = 82.0, float(self.width() - 26)
        top_top, top_bottom = 46.0, float(self.height() * 0.57)
        bot_top, bot_bottom = float(self.height() * 0.68), float(self.height() * 0.85)
        x_map = lambda value: left + (value - x_min) / (x_max - x_min) * (right - left)
        y_map = lambda value: top_bottom - (value - y_min) / (y_max - y_min) * (top_bottom - top_top)
        r_map = lambda value: bot_bottom - (value + residual_max) / (2 * residual_max) * (bot_bottom - bot_top)

        painter.setPen(QColor("#e2e8f0"))
        rmse = float(rows[0]["curve_rmse_pct"])
        loo_rmse = float(rows[0]["tau_selection_loo_rmse_pct"])
        zero_maturity_yield = float(rows[0]["implied_zero_maturity_yield_pct"])
        painter.drawText(
            QRectF(left, 12, right - left, 24),
            Qt.AlignmentFlag.AlignLeft,
            f"{rows[0]['ticker']} | NS curve | RMSE {rmse:.3f} pp | "
            f"0Y extrapolation {zero_maturity_yield:.2f}% | "
            f"LOO tau-selection RMSE {loo_rmse:.3f} pp",
        )
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(4, top_top, 70, 18), Qt.AlignmentFlag.AlignRight, "Yield (%)")
        painter.drawText(QRectF(4, bot_top, 70, 18), Qt.AlignmentFlag.AlignRight, "Residual (pp)")

        for step in range(5):
            fraction = step / 4
            y_value = y_min + fraction * (y_max - y_min)
            y = y_map(y_value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 68, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{y_value:.2f}")
            r_value = residual_max - fraction * (2 * residual_max)
            ry = r_map(r_value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, ry), QPointF(right, ry))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, ry - 9, 68, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{r_value:.2f}")

        painter.setPen(QPen(QColor("#64748b"), 1))
        painter.drawRect(QRectF(left, top_top, right - left, top_bottom - top_top))
        painter.drawRect(QRectF(left, bot_top, right - left, bot_bottom - bot_top))
        zero_y = r_map(0.0)
        painter.setPen(QPen(QColor("#e2e8f0"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(left, zero_y), QPointF(right, zero_y))

        beta0 = float(rows[0]["beta_level"])
        beta1 = float(rows[0]["beta_slope"])
        beta2 = float(rows[0]["beta_curvature"])
        tau = float(rows[0]["tau_years"])
        path = QPainterPath()
        for index in range(101):
            maturity = x_min + (x_max - x_min) * index / 100
            scaled = maturity / tau
            level = -math.expm1(-scaled) / scaled
            curvature = level - math.exp(-scaled)
            prediction = beta0 + beta1 * level + beta2 * curvature
            point = QPointF(x_map(maturity), y_map(prediction))
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(QColor("#38bdf8"), 2.5))
        painter.drawPath(path)

        for maturity, value in zip(maturities, observed):
            painter.setPen(QPen(QColor("#f8fafc"), 1))
            painter.setBrush(QColor("#f8fafc"))
            painter.drawEllipse(QPointF(x_map(maturity), y_map(value)), 4.2, 4.2)

        bar_width = max(4.0, min(24.0, (right - left) / max(len(rows) * 1.7, 1)))
        for row, maturity, residual in zip(rows, maturities, residuals):
            x = x_map(maturity)
            y = r_map(residual)
            bar_top = min(y, zero_y)
            height = max(1.5, abs(y - zero_y))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.setBrush(QColor("#34d399") if residual >= 0 else QColor("#fb7185"))
            painter.drawRoundedRect(QRectF(x - bar_width / 2, bar_top, bar_width, height), 2, 2)

        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, top_bottom + 5, right - left, 20), Qt.AlignmentFlag.AlignCenter, "Remaining maturity (years)")
        for maturity in (x_min, (x_min + x_max) / 2, x_max):
            x = x_map(maturity)
            painter.drawText(QRectF(x - 35, bot_bottom + 4, 70, 18), Qt.AlignmentFlag.AlignHCenter, f"{maturity:.1f}")

        painter.setPen(QColor("#e2e8f0"))
        painter.setBrush(QColor("#f8fafc"))
        painter.drawEllipse(QPointF(left + 8, self.height() - 15), 4, 4)
        painter.drawText(QRectF(left + 18, self.height() - 26, 100, 20), Qt.AlignmentFlag.AlignLeft, "Observed")
        painter.setPen(QPen(QColor("#38bdf8"), 2.5))
        painter.drawLine(QPointF(left + 125, self.height() - 15), QPointF(left + 145, self.height() - 15))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left + 150, self.height() - 26, 130, 20), Qt.AlignmentFlag.AlignLeft, f"Fitted (tau={tau:.2f}y)")
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(
            QRectF(left + 290, self.height() - 26, right - left - 290, 20),
            Qt.AlignmentFlag.AlignLeft,
            "Curve limited to observed maturities; positive residual means cheap vs curve",
        )
        painter.end()


class NelsonSiegelWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nelson-Siegel Curve Explorer")
        self.resize(1480, 760)
        self.results: list[dict[str, Any]] = []

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

        controls = QHBoxLayout()
        controls.addWidget(QLabel("As of"))
        self.as_of_date = QDateEdit(calendarPopup=True)
        self.as_of_date.setDisplayFormat("yyyy-MM-dd")
        self.as_of_date.setDate(QDate.currentDate().addDays(-1))
        controls.addWidget(self.as_of_date)
        controls.addWidget(QLabel("Minimum bonds per ticker"))
        self.minimum_bonds = QSpinBox()
        self.minimum_bonds.setRange(4, 100)
        self.minimum_bonds.setValue(5)
        controls.addWidget(self.minimum_bonds)
        run = QPushButton("Fit ticker curves")
        run.clicked.connect(self._fit)
        controls.addWidget(run)
        self.export_button = QPushButton("Export CSV...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.summary = QLabel(
            f"Database: {DATABASE_PATH}\nFits mapped bond curve tickers cross-sectionally using latest valid TRACE yields."
        )
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 14)
        self.table.setHorizontalHeaderLabels([
            "Ticker", "Status", "ISIN", "Maturity (years)", "Yield date", "Observed yield (%)",
            "Fitted yield (%)", "Residual (pp)", "Signal", "Tau (years)", "Curve RMSE (pp)",
            "Tau selection LOO RMSE (pp)", "0Y extrapolation (%)", "Reason",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.tabs = QTabWidget()
        explorer_page = QWidget()
        explorer_layout = QVBoxLayout(explorer_page)
        ticker_row = QHBoxLayout()
        ticker_row.addWidget(QLabel("Issuer ticker"))
        self.ticker_selector = QComboBox()
        self.ticker_selector.currentTextChanged.connect(self._select_ticker)
        ticker_row.addWidget(self.ticker_selector)
        ticker_row.addStretch(1)
        explorer_layout.addLayout(ticker_row)
        self.explorer = NelsonSiegelExplorer()
        explorer_layout.addWidget(self.explorer, 3)
        self.explorer_table = QTableWidget(0, 6)
        self.explorer_table.setHorizontalHeaderLabels(
            ["ISIN", "Maturity (years)", "Yield date", "Observed (%)", "Fitted (%)", "Residual (pp)"]
        )
        self.explorer_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.explorer_table.horizontalHeader().setStretchLastSection(True)
        explorer_layout.addWidget(self.explorer_table, 2)
        self.tabs.addTab(explorer_page, "Curve explorer")
        results_page = QWidget()
        results_layout = QVBoxLayout(results_page)
        results_layout.addWidget(self.table)
        self.tabs.addTab(results_page, "All results")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select bond list", self.bond_list.text(), "CSV files (*.csv)")
        if selected:
            self.bond_list.setText(selected)

    def _fit(self) -> None:
        try:
            self.results = fit_ticker_curves(
                as_of_date=date.fromisoformat(self.as_of_date.date().toString("yyyy-MM-dd")),
                bond_list_path=Path(self.bond_list.text()).expanduser(),
                database_path=DATABASE_PATH,
                minimum_bonds=self.minimum_bonds.value(),
            )
        except Exception as error:
            QMessageBox.warning(self, "Could not fit ticker curves", str(error))
            return
        self._populate()
        self._populate_tickers()
        fitted = [row for row in self.results if row["status"] == "fitted"]
        tickers = {row["ticker"] for row in fitted}
        self.summary.setText(
            f"As of {self.as_of_date.date().toString('yyyy-MM-dd')}: fit {len(tickers)} ticker curves across "
            f"{len(fitted)} bonds; {len(self.results) - len(fitted)} tickers lacked sufficient cross-sectional data. "
            "Positive yield residual means the bond yields above its fitted ticker curve (cheap vs curve); "
            "negative means below (rich vs curve). This is a relative-value signal candidate, not a validated return forecast."
        )
        self.export_button.setEnabled(bool(self.results))

    def _populate_tickers(self) -> None:
        fitted = sorted({row["ticker"] for row in self.results if row["status"] == "fitted"})
        self.ticker_selector.blockSignals(True)
        self.ticker_selector.clear()
        self.ticker_selector.addItems(fitted)
        self.ticker_selector.blockSignals(False)
        self._select_ticker(self.ticker_selector.currentText())

    def _select_ticker(self, ticker: str) -> None:
        rows = [row for row in self.results if row.get("ticker") == ticker and row.get("status") == "fitted"]
        self.explorer.set_rows(rows)
        self.explorer_table.setRowCount(len(rows))
        fields = ("isin", "maturity_years", "yield_date", "observed_yield_pct", "fitted_yield_pct", "yield_residual_pct")
        for row_index, row in enumerate(sorted(rows, key=lambda item: float(item["maturity_years"]))):
            for column, key in enumerate(fields):
                value = row.get(key, "")
                display = f"{value:.4f}" if isinstance(value, float) else str(value)
                self.explorer_table.setItem(row_index, column, QTableWidgetItem(display))
        self.explorer_table.resizeColumnsToContents()

    def _populate(self) -> None:
        fields = (
            "ticker", "status", "isin", "maturity_years", "yield_date", "observed_yield_pct",
            "fitted_yield_pct", "yield_residual_pct", "relative_value_signal", "tau_years", "curve_rmse_pct",
            "tau_selection_loo_rmse_pct", "implied_zero_maturity_yield_pct", "reason",
        )
        self.table.setRowCount(len(self.results))
        for row_index, row in enumerate(self.results):
            for column, key in enumerate(fields):
                value = row.get(key, "")
                display = f"{value:.4f}" if isinstance(value, float) else str(value)
                self.table.setItem(row_index, column, QTableWidgetItem(display))
        self.table.resizeColumnsToContents()

    def _export(self) -> None:
        if not self.results:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Nelson-Siegel results", "nelson_siegel_ticker_curves.csv", "CSV files (*.csv)")
        if not path:
            return
        columns = list(dict.fromkeys(key for row in self.results for key in row))
        try:
            with Path(path).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(self.results)
        except OSError as error:
            QMessageBox.warning(self, "Could not export results", str(error))
            return
        self.summary.setText(f"{self.summary.text()}\nExported: {path}")


def main() -> None:
    app = QApplication(sys.argv)
    window = NelsonSiegelWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
