"""Desktop analyzer for single-bond total and financing-excess returns."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCompleter,
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
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .bond_return_analysis import analyze_bond_return_series, available_treasury_tenors
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT

FAVORITES_PATH = Path.home() / ".parallax" / "bond_return_favorites.json"


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
        self._hover_index: int | None = None
        self.setMinimumHeight(340)
        self.setMouseTracking(True)

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
        series = (("cumulative_bond_index", "Bond total return (unhedged)", "#38bdf8"),)
        if self.rows[0].get("cumulative_financing_index") is not None:
            series += (
                ("cumulative_financing_index", "Funding cost (unhedged long)", "#fb7185"),
                ("cumulative_excess_index", "Bond less funding (unhedged)", "#34d399"),
            )
        if self.rows[0].get("cumulative_g_spread_hedged_index") is not None:
            series += (("cumulative_g_spread_hedged_index", "Treasury-hedged total return", "#c084fc"),)
            series += (("cumulative_g_spread_carry_index", "G-spread carry", "#2dd4bf"),)
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
                         "Cumulative return indices | 100 at start of window")
        for step in range(5):
            value = low + step * (high - low) / 4
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 70, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{value:.2f}")
        painter.save()
        painter.translate(15, (top + bottom) / 2)
        painter.rotate(-90)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(-90, -10, 180, 18), Qt.AlignmentFlag.AlignCenter, "Index (start = 100)")
        painter.restore()
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

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if len(self.rows) < 2:
            return
        left, right = 82.0, float(self.width() - 24)
        x = event.position().x()
        if not left <= x <= right:
            QToolTip.hideText()
            self._hover_index = None
            return
        index = round((x - left) / (right - left) * (len(self.rows) - 1))
        index = max(0, min(len(self.rows) - 1, index))
        if index == self._hover_index:
            return
        self._hover_index = index
        row = self.rows[index]
        details = [
            f"Date: {row['date']}",
            f"Unhedged bond total return: {float(row.get('bond_total_return_pct') or 0):.4f}%",
            f"Bond index: {float(row['cumulative_bond_index']):.3f}",
        ]
        if row.get("g_spread_bps") is not None:
            details.append(
                f"G-spread: {float(row['g_spread_bps']):.1f} bp "
                f"(observed {row.get('g_spread_observation_date')})"
            )
        if row.get("g_spread_carry_return_pct") is not None:
            details += [
                f"G-spread carry: {float(row['g_spread_carry_return_pct']):.4f}%",
                f"Spread price move: {float(row.get('g_spread_price_return_pct') or 0):.4f}%",
                f"Hedge modified duration: {float(row.get('hedge_modified_duration_years') or 0):.2f} years",
                f"Treasury benchmark deducted: {float(row.get('treasury_benchmark_return_pct') or 0):.4f}%",
                f"Treasury-hedged total return: {float(row.get('g_spread_hedged_return_pct') or 0):.4f}%",
                f"Hedged index: {float(row['cumulative_g_spread_hedged_index']):.3f}",
            ]
        if row.get("financing_cost_return_pct") is not None:
            details += [
                f"Unhedged funding cost: {float(row['financing_cost_return_pct']):.4f}%",
                f"Bond less funding index: {float(row['cumulative_excess_index']):.3f}",
            ]
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(details), self)
        super().mouseMoveEvent(event)


class GSpreadHistoryChart(QWidget):
    """Show actual, uncarried G-spread observations for the selected bond."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(190)
        self.setMouseTracking(True)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        points = [
            (index, float(row["g_spread_bps"]))
            for index, row in enumerate(self.rows)
            if row.get("g_spread_bps") is not None
            and row.get("g_spread_observation_date") == row.get("date")
        ]
        if len(points) < 2:
            painter.setPen(QColor("#94a3b8"))
            message = "No G-spread observations in this window; build a G-spread snapshot to populate this panel."
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
            painter.end()
            return
        left, right = 78.0, float(self.width() - 26)
        top, bottom = 32.0, float(self.height() - 38)
        values = [value for _index, value in points]
        low, high = min(values), max(values)
        pad = max((high - low) * 0.12, 5.0)
        low, high = low - pad, high + pad
        x_map = lambda index: left + index / max(1, len(self.rows) - 1) * (right - left)
        y_map = lambda value: bottom - (value - low) / (high - low) * (bottom - top)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 5, right - left, 20), Qt.AlignmentFlag.AlignLeft,
                         "Observed G-spread | matched Treasury curve (bp)")
        for step in range(5):
            value = low + step * (high - low) / 4
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 66, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{value:.0f}")
        path = QPainterPath()
        for point_index, (index, value) in enumerate(points):
            point = QPointF(x_map(index), y_map(value))
            if point_index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(QColor("#60a5fa"), 2.2))
        painter.drawPath(path)
        first_date = self.rows[points[0][0]]["date"]
        last_date = self.rows[points[-1][0]]["date"]
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, bottom + 5, right - left, 18), Qt.AlignmentFlag.AlignCenter,
                         f"{first_date} to {last_date} | {len(points):,} observations")
        painter.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        points = [
            (index, row)
            for index, row in enumerate(self.rows)
            if row.get("g_spread_bps") is not None
            and row.get("g_spread_observation_date") == row.get("date")
        ]
        if len(points) < 2:
            return
        left, right = 78.0, float(self.width() - 26)
        index = round((event.position().x() - left) / (right - left) * (len(self.rows) - 1))
        index = max(0, min(len(self.rows) - 1, index))
        _row_index, row = min(points, key=lambda point: abs(point[0] - index))
        text = (
            f"Observation date: {row['g_spread_observation_date']}\n"
            f"G-spread: {float(row['g_spread_bps']):.1f} bp\n"
            f"Matched Treasury yield: {float(row['treasury_hedge_yield_pct']):.3f}%"
        )
        QToolTip.showText(event.globalPosition().toPoint(), text, self)
        super().mouseMoveEvent(event)


class BondReturnAnalysisWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bond Total Return and Financing Excess")
        self.resize(1500, 940)
        self.result: dict[str, Any] | None = None
        self.worker: BondReturnWorker | None = None
        self.favorites = self._read_favorites()

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

        favorites_row = QHBoxLayout()
        favorites_row.addWidget(QLabel("Bond favorites"))
        self.favorite_picker = QComboBox()
        self.favorite_picker.setMinimumWidth(360)
        self.favorite_picker.currentIndexChanged.connect(self._favorite_selected)
        favorites_row.addWidget(self.favorite_picker, 1)
        add_favorite = QPushButton("Add selected bond")
        add_favorite.clicked.connect(self._add_favorite)
        favorites_row.addWidget(add_favorite)
        remove_favorite = QPushButton("Remove favorite")
        remove_favorite.clicked.connect(self._remove_favorite)
        favorites_row.addWidget(remove_favorite)
        layout.addLayout(favorites_row)

        rate_source = QHBoxLayout()
        self.treasury_tenor_label = QLabel("Treasury curve tenor")
        rate_source.addWidget(self.treasury_tenor_label)
        self.treasury_tenor = QComboBox()
        self.treasury_tenor.setMinimumWidth(180)
        self.treasury_tenor.setToolTip(f"Reads stored curve history from {DATABASE_PATH}")
        try:
            tenors = available_treasury_tenors(DATABASE_PATH)
        except (OSError, ValueError):
            tenors = []
        for tenor in tenors:
            label = f"{tenor * 12:.0f} month" if tenor < 1 else f"{tenor:g} year"
            self.treasury_tenor.addItem(label, tenor)
        short_tenor_index = next(
            (index for index in range(self.treasury_tenor.count())
             if abs(float(self.treasury_tenor.itemData(index)) - 1.0 / 12.0) < 1e-8),
            -1,
        )
        if short_tenor_index >= 0:
            self.treasury_tenor.setCurrentIndex(short_tenor_index)
        rate_source.addWidget(self.treasury_tenor)
        self.treasury_source_note = QLabel("(from shared Treasury curve history)")
        rate_source.addWidget(self.treasury_source_note)
        layout.addLayout(rate_source)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("ISIN"))
        self.isin = QComboBox()
        self.isin.setEditable(True)
        self.isin.setMinimumWidth(310)
        self.isin.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        bond_completer = QCompleter(self.isin.model(), self)
        bond_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        bond_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.isin.setCompleter(bond_completer)
        self.isin.setToolTip("Type any part of the ISIN, issuer, coupon or maturity to search.")
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
        self.financing_spread_label = QLabel("Financing spread (bp)")
        controls.addWidget(self.financing_spread_label)
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
        self.rate_age_label = QLabel("Max rate age (days)")
        controls.addWidget(self.rate_age_label)
        self.rate_age = QSpinBox()
        self.rate_age.setRange(0, 90)
        self.rate_age.setValue(7)
        controls.addWidget(self.rate_age)
        self.g_spread_hedge = QCheckBox("Use Treasury-hedged total return")
        self.g_spread_hedge.setChecked(True)
        self.g_spread_hedge.setToolTip(
            "Default on. Subtracts a duration-matched Treasury benchmark return from the bond's realized total return. "
            "G-spread carry and price moves are shown separately. Financing comparisons are hidden in this mode."
        )
        self.g_spread_hedge.toggled.connect(self._return_mode_changed)
        controls.addWidget(self.g_spread_hedge)
        self.run_button = QPushButton("Analyze return series")
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        self.method_note = QLabel()
        self.method_note.setWordWrap(True)
        layout.addWidget(self.method_note)
        self.summary = QLabel("Select a bond, dates and Treasury curve tenor, then run the analysis.")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.status = QLabel("Ready")
        layout.addWidget(self.status)
        self.chart = ReturnSeriesChart()
        layout.addWidget(self.chart, 2)
        layout.addWidget(QLabel("G-spread history"))
        self.g_spread_chart = GSpreadHistoryChart()
        layout.addWidget(self.g_spread_chart, 1)
        self.table = QTableWidget(0, 25)
        self.table.setHorizontalHeaderLabels([
            "Date", "Price mark date", "Price age (days)", "Clean price", "Period days",
            "Bond return (%)", "Rate observation date", "Rate age (days)", "Treasury yield (%)",
            "G-spread observation date", "G-spread (bp)", "Matched Treasury yield (%)",
            "Point-in-time hedge duration (years)",
            "Treasury benchmark return (%)", "G-spread carry (%)", "Spread price move (%)",
            "Treasury-hedged total return (%)",
            "Cumulative hedge index", "Financing rate (%)", "Financing cost (%)",
            "Unhedged funding cost log return", "Bond log return", "Excess log return",
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
        self._refresh_favorites()
        self._return_mode_changed()

    @staticmethod
    def _read_favorites() -> list[dict[str, str]]:
        try:
            data = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [
            {"isin": str(item["isin"]).strip().upper(), "label": str(item["label"])}
            for item in data
            if isinstance(item, dict) and item.get("isin") and item.get("label")
        ]

    def _save_favorites(self) -> None:
        try:
            FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
            FAVORITES_PATH.write_text(json.dumps(self.favorites, indent=2), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Could not save favorites", str(error))
            return
        self._refresh_favorites()

    def _refresh_favorites(self, selected_isin: str | None = None) -> None:
        if not hasattr(self, "favorite_picker"):
            return
        selected_isin = selected_isin or self.favorite_picker.currentData()
        self.favorite_picker.blockSignals(True)
        self.favorite_picker.clear()
        self.favorite_picker.addItem("Select a saved bond…", None)
        for favorite in self.favorites:
            self.favorite_picker.addItem(favorite["label"], favorite["isin"])
        index = self.favorite_picker.findData(selected_isin)
        self.favorite_picker.setCurrentIndex(index if index >= 0 else 0)
        self.favorite_picker.blockSignals(False)

    def _favorite_selected(self, index: int) -> None:
        if index <= 0:
            return
        isin = self.favorite_picker.itemData(index)
        bond_index = self.isin.findData(isin)
        if bond_index >= 0:
            self.isin.setCurrentIndex(bond_index)
        else:
            QMessageBox.information(
                self,
                "Bond not in current list",
                f"{isin} is saved as a favorite but does not appear in the current bond list.",
            )

    def _add_favorite(self) -> None:
        index = self.isin.currentIndex()
        isin = self.isin.currentData()
        if index < 0 or not isin:
            QMessageBox.information(self, "Select a bond", "Choose a bond from the searchable bond list first.")
            return
        label = self.isin.currentText()
        existing = next((item for item in self.favorites if item["isin"] == isin), None)
        if existing:
            existing["label"] = label
        else:
            self.favorites.append({"isin": str(isin), "label": label})
        self._save_favorites()
        self.favorite_picker.setCurrentIndex(self.favorite_picker.findData(isin))

    def _remove_favorite(self) -> None:
        isin = self.favorite_picker.currentData()
        if not isin:
            QMessageBox.information(self, "Select a favorite", "Choose a saved bond to remove.")
            return
        self.favorites = [item for item in self.favorites if item["isin"] != isin]
        self._save_favorites()

    def _return_mode_changed(self, *_args: Any) -> None:
        use_hedge = self.g_spread_hedge.isChecked()
        for widget in (
            self.treasury_tenor_label,
            self.treasury_tenor,
            self.treasury_source_note,
            self.financing_spread_label,
            self.spread,
            self.rate_age_label,
            self.rate_age,
        ):
            widget.setVisible(not use_hedge)
        for column in (6, 7, 8, 18, 19, 20, 22, 23):
            self.table.setColumnHidden(column, use_hedge)
        if use_hedge:
            self.method_note.setText(
                "Treasury-hedged total return = realized bond total return less a duration-matched Treasury benchmark return, "
                "including benchmark carry and price movement. Hedge duration is recalculated from each historical yield "
                "with QuantLib. G-spread carry and price moves are explanatory components; "
                "the separate financing comparison is hidden in this mode."
            )
        else:
            self.method_note.setText(
                "Unhedged financing cost assumes the full bond notional is funded at the selected Treasury tenor "
                "plus any additional spread. Cost is shown as negative; log excess return is bond log return less funding accrual."
            )
        if self.result is not None:
            self.result = None
            self.chart.set_rows([])
            self.g_spread_chart.set_rows([])
            self.table.setRowCount(0)
            self.export_button.setEnabled(False)
            self.summary.setText("Return mode changed. Run the analysis again to refresh the results.")
            self.status.setText("Ready")

    def _browse_bond_list(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select bond list", self.bond_list.text(), "CSV files (*.csv)")
        if selected:
            self.bond_list.setText(selected)

    def _refresh_bonds(self, *_args: Any) -> None:
        selected = self.isin.currentData() if hasattr(self, "isin") else ""
        entries: list[tuple[str, str]] = []
        try:
            with Path(self.bond_list.text()).expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    isin = (row.get("isin") or "").strip().upper()
                    if isin:
                        issuer = (row.get("issuer_name") or "").strip()
                        coupon = (row.get("coupon_pct") or "").strip()
                        maturity = (row.get("maturity_date") or "").strip()
                        label = f"{isin} | {issuer} | {coupon}% | {maturity}"
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
        include_financing = not self.g_spread_hedge.isChecked()
        if not selected_isin or (include_financing and self.treasury_tenor.currentData() is None):
            QMessageBox.warning(self, "Missing input", "Select an ISIN and a Treasury curve tenor.")
            return
        self.run_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.table.setRowCount(0)
        self.chart.set_rows([])
        self.g_spread_chart.set_rows([])
        self.status.setText(
            "Calculating bond and Treasury-hedged total returns..."
            if self.g_spread_hedge.isChecked()
            else "Calculating total and Treasury-financed excess returns..."
        )
        parameters = {
            "isin": selected_isin,
            "start_date": start,
            "end_date": end,
            "treasury_tenor_years": (
                float(self.treasury_tenor.currentData())
                if self.treasury_tenor.currentData() is not None else 1.0 / 12.0
            ),
            "financing_spread_bps": self.spread.value(),
            "include_g_spread_hedge": self.g_spread_hedge.isChecked(),
            "include_financing_comparison": include_financing,
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

        summary_parts = [
            f"{summary['isin']} | {summary['issuer_name']} | coupon {summary['coupon_pct']:.3f}% | "
            f"maturity {summary['maturity_date']} | snapshot modified duration "
            f"{summary['modified_duration_years'] if summary['modified_duration_years'] is not None else 'n/a'} | "
            f"{summary['requested_start_date']} to "
            f"{summary['effective_end_date']} | Bond total return {format_optional(summary['cumulative_bond_return_pct'], '%')}"
        ]
        if summary["financing_comparison_calculated"]:
            tenor_years = float(summary["treasury_tenor_years"])
            tenor_label = f"{tenor_years * 12:.0f}-month" if tenor_years < 1 else f"{tenor_years:g}-year"
            summary_parts.extend([
                f"Unhedged funding cost {format_optional(summary['cumulative_financing_return_pct'], '%')}",
                f"Bond less funding {format_optional(summary['cumulative_excess_return_pct'], '%')}",
                f"Annualized excess {format_optional(summary['annualized_log_excess_return_pct'], '%')}",
                f"Excess volatility {format_optional(summary['annualized_excess_volatility_pct'], '%')}",
                f"Excess Sharpe {format_optional(summary['excess_sharpe_ratio'])}",
                f"Funding rate: {tenor_label} Treasury + {summary['financing_spread_bps']:.1f} bp",
            ])
        if summary["include_g_spread_hedge"]:
            summary_parts.extend([
                f"G-spread carry {format_optional(summary['cumulative_g_spread_carry_return_pct'], '%')}",
                f"Treasury-hedged total return {format_optional(summary['cumulative_g_spread_hedged_return_pct'], '%')}",
            ])
        self.summary.setText(" | ".join(summary_parts))
        self.chart.set_rows(result["time_series"])
        self.g_spread_chart.set_rows(result["time_series"])
        fields = (
            "date", "price_observation_date", "price_age_days", "clean_price_per_100", "period_days",
            "bond_total_return_pct", "financing_rate_observation_date", "financing_rate_age_days",
            "base_short_rate_pct", "g_spread_observation_date", "g_spread_bps", "treasury_hedge_yield_pct",
            "hedge_modified_duration_years",
            "treasury_benchmark_return_pct", "g_spread_carry_return_pct", "g_spread_price_return_pct",
            "g_spread_hedged_return_pct",
            "cumulative_g_spread_hedged_index", "financing_rate_pct", "financing_cost_return_pct",
            "financing_log_return", "bond_log_return", "log_excess_return", "cumulative_excess_index",
            "mark_status",
        )
        rows = result["time_series"]
        for column in (6, 7, 8, 18, 19, 20, 22, 23):
            self.table.setColumnHidden(column, summary["include_g_spread_hedge"])
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
