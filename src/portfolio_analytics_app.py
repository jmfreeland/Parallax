"""Desktop horizon P&L and attribution for archived exploratory portfolios."""

from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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

from .portfolio_analytics import PortfolioHorizonAnalysis, analyze_portfolio_horizon
from .portfolio_archive import load_archive
from .portfolio_covariance import calculate_portfolio_covariance
from .trace_history import DATABASE_PATH


class AnalysisWorker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        archive: dict[str, Any],
        end_date: date,
        starting_nav: float,
        max_price_age_days: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.archive = archive
        self.end_date = end_date
        self.starting_nav = starting_nav
        self.max_price_age_days = max_price_age_days

    def run(self) -> None:
        try:
            result = analyze_portfolio_horizon(
                self.archive,
                horizon_end_date=self.end_date,
                starting_nav=self.starting_nav,
                max_price_age_days=self.max_price_age_days,
                database_path=DATABASE_PATH,
                on_progress=self.progress.emit,
            )
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


class CovarianceWorker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        archive: dict[str, Any],
        as_of_date: date,
        lookback_days: int,
        minimum_pair_observations: int,
        max_price_age_days: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.archive = archive
        self.as_of_date = as_of_date
        self.lookback_days = lookback_days
        self.minimum_pair_observations = minimum_pair_observations
        self.max_price_age_days = max_price_age_days

    def run(self) -> None:
        try:
            result = calculate_portfolio_covariance(
                self.archive,
                as_of_date=self.as_of_date,
                lookback_days=self.lookback_days,
                minimum_pair_observations=self.minimum_pair_observations,
                max_price_age_days=self.max_price_age_days,
                database_path=DATABASE_PATH,
                on_progress=self.progress.emit,
            )
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


class PortfolioBarChart(QWidget):
    """Small labelled horizontal bar chart for exposures or P&L contribution."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = ""
        self.rows: list[tuple[str, float]] = []
        self.value_format = "number"
        self.setMinimumSize(600, 340)

    def set_data(self, title: str, rows: list[tuple[str, float]], value_format: str) -> None:
        self.title = title
        self.rows = rows
        self.value_format = value_format
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(14, 8, self.width() - 28, 24), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Run an analysis to display a chart.")
            painter.end()
            return

        values = [value for _, value in self.rows]
        minimum, maximum = min(0.0, min(values)), max(0.0, max(values))
        span = maximum - minimum or 1.0
        left, right = 176.0, float(self.width() - 92)
        top, bottom = 42.0, float(self.height() - 22)
        row_height = max(11.0, (bottom - top) / len(self.rows))
        zero_x = left + (0.0 - minimum) / span * (right - left)

        for step in range(5):
            value = minimum + span * step / 4
            x = left + (value - minimum) / span * (right - left)
            painter.setPen(QPen(QColor("#334155"), 1))
            painter.drawLine(QPointF(x, top - 5), QPointF(x, bottom))
            painter.setPen(QColor("#94a3b8"))
            label = f"{value:,.1f}%" if self.value_format == "percent" else f"{value:,.0f}"
            painter.drawText(QRectF(x - 38, bottom + 2, 76, 16), Qt.AlignmentFlag.AlignHCenter, label)
        painter.setPen(QPen(QColor("#64748b"), 1))
        painter.drawLine(QPointF(zero_x, top - 5), QPointF(zero_x, bottom))

        for index, (label, value) in enumerate(self.rows):
            y = top + index * row_height
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(
                QRectF(8, y, left - 16, row_height - 2),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label[:24],
            )
            x_value = left + (value - minimum) / span * (right - left)
            bar_left, bar_right = sorted((zero_x, x_value))
            color = QColor("#38bdf8") if value >= 0 else QColor("#fb7185")
            painter.fillRect(QRectF(bar_left, y + 4, max(1.0, bar_right - bar_left), row_height - 9), color)
            painter.setPen(QColor("#e2e8f0"))
            value_label = f"{value:,.2f}%" if self.value_format == "percent" else f"{value:,.2f}"
            value_x = min(right + 4, x_value + 5) if value >= 0 else max(left + 4, x_value - 78)
            painter.drawText(
                QRectF(value_x, y, 82, row_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                value_label,
            )
        painter.end()


class PortfolioTimeSeriesChart(QWidget):
    """Cumulative horizon P&L plotted at daily weekday marks."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: tuple[dict[str, Any], ...] = ()
        self.setMinimumSize(700, 420)

    def set_data(self, rows: tuple[dict[str, Any], ...]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(16, 8, self.width() - 32, 24), Qt.AlignmentFlag.AlignLeft,
                         "Cumulative portfolio P&L (NAV units)")
        if not self.rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Run an analysis to display the time series.")
            painter.end()
            return

        left, right, top, bottom = 76.0, float(self.width() - 30), 48.0, float(self.height() - 48)
        values = [float(row["cumulative_pnl"]) for row in self.rows]
        low, high = min(0.0, min(values)), max(0.0, max(values))
        span = high - low or 1.0
        x_span = max(1.0, right - left)
        y_span = max(1.0, bottom - top)
        for step in range(5):
            value = low + span * step / 4
            y = bottom - (value - low) / span * y_span
            painter.setPen(QPen(QColor("#334155"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(2, y - 9, left - 8, 18), Qt.AlignmentFlag.AlignRight, f"{value:,.0f}")

        first_day = self.rows[0]["date"].toordinal()
        last_day = self.rows[-1]["date"].toordinal()
        path_points = []
        for row in self.rows:
            fraction = (row["date"].toordinal() - first_day) / max(1, last_day - first_day)
            x = left + fraction * x_span
            y = bottom - (float(row["cumulative_pnl"]) - low) / span * y_span
            path_points.append(QPointF(x, y))
        painter.setPen(QPen(QColor("#38bdf8"), 2))
        for previous, current in zip(path_points, path_points[1:]):
            painter.drawLine(previous, current)
        if len(path_points) <= 250:
            painter.setBrush(QColor("#38bdf8"))
            for point in path_points:
                painter.drawEllipse(point, 2.0, 2.0)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, bottom + 8, 120, 20), Qt.AlignmentFlag.AlignLeft,
                         self.rows[0]["date"].isoformat())
        painter.drawText(QRectF(right - 120, bottom + 8, 120, 20), Qt.AlignmentFlag.AlignRight,
                         self.rows[-1]["date"].isoformat())
        coverage = self.rows[-1]["coverage_pct"]
        stale = self.rows[-1]["stale_weight_pct"]
        painter.drawText(QRectF(right - 240, 8, 220, 24), Qt.AlignmentFlag.AlignRight,
                         f"Marked {coverage:.1f}% | stale {stale:.1f}%")
        painter.end()


class PortfolioAnalyticsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Portfolio Horizon P&L and Attribution")
        self.resize(1440, 900)
        self.archives: list[dict[str, Any]] = []
        self.analysis: PortfolioHorizonAnalysis | None = None
        self.worker: AnalysisWorker | None = None
        self.covariance_result: dict[str, Any] | None = None
        self.covariance_worker: CovarianceWorker | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Archived portfolio"))
        self.archive_selector = QComboBox()
        self.archive_selector.currentIndexChanged.connect(self._archive_changed)
        controls.addWidget(self.archive_selector, 2)
        controls.addWidget(QLabel("Horizon end"))
        self.end_date = QDateEdit(calendarPopup=True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        controls.addWidget(self.end_date)
        controls.addWidget(QLabel("Max price age (days)"))
        self.max_price_age = QSpinBox()
        self.max_price_age.setRange(0, 365)
        self.max_price_age.setValue(30)
        controls.addWidget(self.max_price_age)
        controls.addWidget(QLabel("Starting NAV"))
        self.starting_nav = QDoubleSpinBox()
        self.starting_nav.setRange(1.0, 1_000_000_000_000.0)
        self.starting_nav.setDecimals(2)
        self.starting_nav.setSingleStep(100_000.0)
        self.starting_nav.setValue(1_000_000.0)
        controls.addWidget(self.starting_nav)
        self.analyze_button = QPushButton("Calculate P&L")
        self.analyze_button.clicked.connect(self._analyze)
        controls.addWidget(self.analyze_button)
        self.export_button = QPushButton("Export attribution CSV...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)

        self.status = QLabel(f"Database: {DATABASE_PATH}")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        self.summary = QLabel("Select an archived portfolio and calculate a horizon.")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        overview_layout.addWidget(self.summary)
        chart_controls = QHBoxLayout()
        chart_controls.addWidget(QLabel("Chart"))
        self.chart_mode = QComboBox()
        self.chart_mode.addItems(
            [
                "P&L by sector",
                "P&L by issuer",
                "P&L by maturity bucket",
                "P&L by bond",
                "Exposure by sector",
                "Exposure by issuer",
                "Exposure by maturity bucket",
            ]
        )
        self.chart_mode.currentTextChanged.connect(self._update_chart)
        chart_controls.addWidget(self.chart_mode)
        chart_controls.addWidget(QLabel("Largest |P&L| contributors"))
        self.top_contributors = QSpinBox()
        self.top_contributors.setRange(1, 25)
        self.top_contributors.setValue(12)
        self.top_contributors.valueChanged.connect(self._refresh_contributor_views)
        chart_controls.addWidget(self.top_contributors)
        chart_controls.addStretch(1)
        overview_layout.addLayout(chart_controls)
        self.chart = PortfolioBarChart()
        overview_layout.addWidget(self.chart, 1)
        self.tabs.addTab(overview, "Overview")

        time_series_page = QWidget()
        time_series_layout = QVBoxLayout(time_series_page)
        self.performance_summary = QLabel("Calculate a portfolio horizon to view performance statistics.")
        self.performance_summary.setWordWrap(True)
        self.performance_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        time_series_layout.addWidget(self.performance_summary)
        self.time_series_note = QLabel(
            "Weekday marks use the latest valid TRACE clean price, carried forward across missing observations. "
            "Accrued interest and cash flows update by valuation date; stale marked weight is flagged."
        )
        self.time_series_note.setWordWrap(True)
        time_series_layout.addWidget(self.time_series_note)
        self.time_series_chart = PortfolioTimeSeriesChart()
        time_series_layout.addWidget(self.time_series_chart, 1)
        self.tabs.addTab(time_series_page, "Time series")

        self.position_table = QTableWidget(0, 16)
        self.position_table.setHorizontalHeaderLabels(
            [
                "ISIN", "Issuer", "Sector", "Maturity", "Bucket", "Weight %",
                "Start price date", "End price date", "Start age days", "End age days",
                "Price move P&L", "Carry P&L", "Principal P&L", "Total P&L", "Bond return %", "Status / reason",
            ]
        )
        self._configure_table(self.position_table)
        self.tabs.addTab(self.position_table, "Bond attribution")

        self.sector_table = QTableWidget(0, 9)
        self.sector_table.setHorizontalHeaderLabels(
            ["Sector", "Positions", "Exposure %", "Coverage %", "Price move P&L", "Carry P&L", "Principal P&L", "Total P&L", "P&L % NAV"]
        )
        self._configure_table(self.sector_table)
        self.tabs.addTab(self.sector_table, "Sector attribution")

        self.issuer_table = QTableWidget(0, 9)
        self.issuer_table.setHorizontalHeaderLabels(
            ["Issuer", "Positions", "Exposure %", "Coverage %", "Price move P&L", "Carry P&L", "Principal P&L", "Total P&L", "P&L % NAV"]
        )
        self._configure_table(self.issuer_table)
        self.tabs.addTab(self.issuer_table, "Issuer attribution")

        self.maturity_table = QTableWidget(0, 9)
        self.maturity_table.setHorizontalHeaderLabels(
            ["Maturity bucket", "Positions", "Exposure %", "Coverage %", "Price move P&L", "Carry P&L", "Principal P&L", "Total P&L", "P&L % NAV"]
        )
        self._configure_table(self.maturity_table)
        self.tabs.addTab(self.maturity_table, "Maturity attribution")

        characteristics = QWidget()
        characteristics_layout = QHBoxLayout(characteristics)
        self.sector_exposure_table = QTableWidget(0, 3)
        self.sector_exposure_table.setHorizontalHeaderLabels(["Sector", "Positions", "Exposure %"])
        self._configure_table(self.sector_exposure_table)
        self.maturity_exposure_table = QTableWidget(0, 3)
        self.maturity_exposure_table.setHorizontalHeaderLabels(["Maturity bucket", "Positions", "Exposure %"])
        self._configure_table(self.maturity_exposure_table)
        self.issuer_exposure_table = QTableWidget(0, 3)
        self.issuer_exposure_table.setHorizontalHeaderLabels(["Issuer", "Positions", "Exposure %"])
        self._configure_table(self.issuer_exposure_table)
        characteristics_layout.addWidget(self.sector_exposure_table)
        characteristics_layout.addWidget(self.maturity_exposure_table)
        characteristics_layout.addWidget(self.issuer_exposure_table)
        self.tabs.addTab(characteristics, "Characteristics")

        covariance_page = QWidget()
        covariance_layout = QVBoxLayout(covariance_page)
        covariance_controls = QHBoxLayout()
        covariance_controls.addWidget(QLabel("As of"))
        self.covariance_as_of = QDateEdit(calendarPopup=True)
        self.covariance_as_of.setDisplayFormat("yyyy-MM-dd")
        covariance_controls.addWidget(self.covariance_as_of)
        covariance_controls.addWidget(QLabel("Lookback (calendar days)"))
        self.covariance_lookback = QSpinBox()
        self.covariance_lookback.setRange(30, 3650)
        self.covariance_lookback.setValue(365)
        covariance_controls.addWidget(self.covariance_lookback)
        covariance_controls.addWidget(QLabel("Minimum pair observations"))
        self.covariance_min_observations = QSpinBox()
        self.covariance_min_observations.setRange(2, 1000)
        self.covariance_min_observations.setValue(20)
        covariance_controls.addWidget(self.covariance_min_observations)
        covariance_controls.addWidget(QLabel("Max price age (days)"))
        self.covariance_price_age = QSpinBox()
        self.covariance_price_age.setRange(0, 90)
        self.covariance_price_age.setValue(7)
        covariance_controls.addWidget(self.covariance_price_age)
        self.covariance_run_button = QPushButton("Calculate covariance")
        self.covariance_run_button.clicked.connect(self._calculate_covariance)
        covariance_controls.addWidget(self.covariance_run_button)
        covariance_layout.addLayout(covariance_controls)

        covariance_view_controls = QHBoxLayout()
        covariance_view_controls.addWidget(QLabel("Display"))
        self.covariance_metric = QComboBox()
        self.covariance_metric.addItem("Covariance (bps²)", "covariance_bps_squared")
        self.covariance_metric.addItem("Correlation", "correlation")
        self.covariance_metric.currentIndexChanged.connect(self._covariance_metric_changed)
        covariance_view_controls.addWidget(self.covariance_metric)
        covariance_view_controls.addWidget(QLabel("Partition view"))
        self.covariance_partition = QComboBox()
        self.covariance_partition.addItem("Individual bonds", "bond")
        self.covariance_partition.addItem("Sector", "sector")
        self.covariance_partition.addItem("Issuer", "issuer_name")
        self.covariance_partition.addItem("Maturity bucket", "maturity_bucket")
        self.covariance_partition.currentIndexChanged.connect(self._render_covariance)
        covariance_view_controls.addWidget(self.covariance_partition)
        covariance_view_controls.addWidget(QLabel("Color lower"))
        self.covariance_color_min = QDoubleSpinBox()
        self.covariance_color_min.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.covariance_color_min.setDecimals(4)
        self.covariance_color_min.setValue(-1.0)
        self.covariance_color_min.valueChanged.connect(self._render_covariance)
        covariance_view_controls.addWidget(self.covariance_color_min)
        covariance_view_controls.addWidget(QLabel("Color upper"))
        self.covariance_color_max = QDoubleSpinBox()
        self.covariance_color_max.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.covariance_color_max.setDecimals(4)
        self.covariance_color_max.setValue(1.0)
        self.covariance_color_max.valueChanged.connect(self._render_covariance)
        covariance_view_controls.addWidget(self.covariance_color_max)
        self.covariance_auto_scale = QPushButton("Auto scale")
        self.covariance_auto_scale.clicked.connect(self._auto_scale_covariance)
        covariance_view_controls.addWidget(self.covariance_auto_scale)
        covariance_layout.addLayout(covariance_view_controls)

        self.covariance_status = QLabel("Calculate a covariance matrix for the selected archived portfolio.")
        self.covariance_status.setWordWrap(True)
        covariance_layout.addWidget(self.covariance_status)
        covariance_layout.addWidget(QLabel(
            "Daily clean-price returns use weekday marks carried forward for at most the selected price age; "
            "coupon and carry are excluded. Estimates use pairwise overlapping dates and can be noisy or non-PSD. "
            "Partition cells average the bond-pair estimates; diagonal cells include bond variances or self-correlations. "
            "Colors run from blue (low/negative) through white near zero to red (high/positive)."
        ))
        self.covariance_table = QTableWidget(0, 0)
        self.covariance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.covariance_table.setAlternatingRowColors(False)
        self.covariance_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.covariance_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        covariance_layout.addWidget(self.covariance_table, 1)
        self.tabs.addTab(covariance_page, "Covariance explorer")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0b1220; color: #e2e8f0; }
            QLineEdit, QDateEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget {
                background: #111827; color: #e2e8f0; border: 1px solid #334155;
                selection-background-color: #1d4ed8; selection-color: #ffffff;
            }
            QComboBox QAbstractItemView { background: #111827; color: #e2e8f0; }
            QHeaderView::section { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 5px; }
            QPushButton { background: #1e293b; color: #e2e8f0; border: 1px solid #475569; border-radius: 4px; padding: 5px 10px; }
            QPushButton:hover { background: #334155; }
            QPushButton:disabled { color: #64748b; background: #111827; }
            QTabBar::tab { background: #1e293b; color: #cbd5e1; padding: 7px 12px; }
            QTabBar::tab:selected { background: #334155; color: #ffffff; }
            """
        )
        self._load_archives()

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)

    def _load_archives(self) -> None:
        self.archives = load_archive()
        self.archive_selector.blockSignals(True)
        self.archive_selector.clear()
        for record in self.archives:
            method = record.get("methodology", {}).get("weighting_method", "unknown")
            label = (
                f"{record.get('as_of_date', 'unknown')} | {method} | "
                f"{len(record.get('positions', [])):,} bonds | {str(record.get('archive_id', ''))[:8]}"
            )
            self.archive_selector.addItem(label)
        self.archive_selector.blockSignals(False)
        if self.archives:
            self._archive_changed(0)
        else:
            self.status.setText("No archived portfolios found. Save a portfolio in the builder first.")

    def _archive_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.archives):
            return
        as_of_date = date.fromisoformat(self.archives[index]["as_of_date"])
        initial_end = max(date.today(), as_of_date + timedelta(days=90))
        self.end_date.setDate(QDate(initial_end.year, initial_end.month, initial_end.day))
        self.covariance_as_of.setDate(QDate(as_of_date.year, as_of_date.month, as_of_date.day))
        self.analysis = None
        self.covariance_result = None
        self.covariance_table.clear()
        self.covariance_table.setRowCount(0)
        self.covariance_table.setColumnCount(0)
        self.covariance_status.setText("Calculate a covariance matrix for the selected archived portfolio.")
        self.export_button.setEnabled(False)

    def _calculate_covariance(self) -> None:
        if self.covariance_worker is not None and self.covariance_worker.isRunning():
            return
        archive_index = self.archive_selector.currentIndex()
        if archive_index < 0:
            QMessageBox.information(self, "No archived portfolio", "Save or select a portfolio first.")
            return
        as_of_date = date.fromisoformat(self.covariance_as_of.date().toString("yyyy-MM-dd"))
        self.covariance_run_button.setEnabled(False)
        self.covariance_status.setText("Loading TRACE clean prices and estimating pairwise covariance...")
        self.progress.setRange(0, len(self.archives[archive_index].get("positions", [])))
        self.progress.setValue(0)
        self.covariance_worker = CovarianceWorker(
            self.archives[archive_index],
            as_of_date,
            self.covariance_lookback.value(),
            self.covariance_min_observations.value(),
            self.covariance_price_age.value(),
            self,
        )
        self.covariance_worker.progress.connect(self._covariance_progress)
        self.covariance_worker.succeeded.connect(self._covariance_succeeded)
        self.covariance_worker.failed.connect(self._covariance_failed)
        self.covariance_worker.start()

    def _covariance_progress(self, completed: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(completed)
        self.covariance_status.setText(f"Building marked daily return series: {completed:,}/{total:,} bonds")

    def _covariance_succeeded(self, result: dict[str, Any]) -> None:
        self.covariance_worker = None
        self.covariance_run_button.setEnabled(True)
        archive_index = self.archive_selector.currentIndex()
        if archive_index < 0 or self.archives[archive_index].get("archive_id") != result.get("archive_id"):
            return
        self.covariance_result = result
        self.covariance_status.setText(
            f"{result['series_count']:,} bonds | {result['window_start_date']} to {result['window_end_date']} | "
            f"{result['eligible_pair_count']:,}/{result['total_pair_count']:,} bond pairs meet the "
            f"{result['minimum_pair_observations']}-observation minimum | "
            f"median shared observations: {result['median_eligible_pair_observations'] or 0:.0f}. "
            "Change metric, partition, or color bounds without recalculating."
        )
        self._auto_scale_covariance()
        self._render_covariance()

    def _covariance_failed(self, message: str) -> None:
        self.covariance_worker = None
        self.covariance_run_button.setEnabled(True)
        self.covariance_status.setText("Covariance calculation failed.")
        QMessageBox.warning(self, "Could not calculate covariance", message)

    def _covariance_metric_changed(self, *_args: Any) -> None:
        if self.covariance_result is not None:
            self._auto_scale_covariance()
        self._render_covariance()

    def _auto_scale_covariance(self) -> None:
        if self.covariance_result is None:
            return
        matrix_key = self.covariance_metric.currentData()
        matrix = self.covariance_result[matrix_key]
        values = [abs(value) for row in matrix for value in row if value is not None]
        maximum = max(values, default=1.0)
        if matrix_key == "correlation":
            lower, upper = -1.0, 1.0
        else:
            maximum = maximum or 1.0
            lower, upper = -maximum, maximum
        self.covariance_color_min.blockSignals(True)
        self.covariance_color_max.blockSignals(True)
        self.covariance_color_min.setValue(lower)
        self.covariance_color_max.setValue(upper)
        self.covariance_color_min.blockSignals(False)
        self.covariance_color_max.blockSignals(False)

    @staticmethod
    def _blend_color(start: QColor, end: QColor, fraction: float) -> QColor:
        fraction = min(1.0, max(0.0, fraction))
        return QColor(
            round(start.red() + (end.red() - start.red()) * fraction),
            round(start.green() + (end.green() - start.green()) * fraction),
            round(start.blue() + (end.blue() - start.blue()) * fraction),
        )

    def _covariance_color(self, value: float) -> QColor:
        lower = self.covariance_color_min.value()
        upper = self.covariance_color_max.value()
        blue = QColor("#2563eb")
        neutral = QColor("#f8fafc")
        red = QColor("#dc2626")
        if lower < 0 < upper:
            if value <= 0:
                return self._blend_color(blue, neutral, (value - lower) / -lower)
            return self._blend_color(neutral, red, value / upper)
        if lower >= 0:
            return self._blend_color(neutral, red, (value - lower) / (upper - lower))
        return self._blend_color(blue, neutral, (value - lower) / (upper - lower))

    def _render_covariance(self, *_args: Any) -> None:
        if self.covariance_result is None:
            return
        lower, upper = self.covariance_color_min.value(), self.covariance_color_max.value()
        if upper <= lower:
            self.covariance_status.setText("Color upper bound must be greater than the lower bound.")
            return
        metric_key = self.covariance_metric.currentData()
        source_matrix = self.covariance_result[metric_key]
        observation_counts = self.covariance_result["observation_counts"]
        positions = self.covariance_result["positions"]
        partition = self.covariance_partition.currentData()
        if partition == "bond":
            labels = [position["label"] for position in positions]
            matrix = source_matrix
            tooltips = [
                [
                    f"{positions[row]['label']} × {positions[column]['label']}\n"
                    f"{self.covariance_result['minimum_pair_observations']} observations required; "
                    f"{observation_counts[row][column]:,} shared daily observations"
                    for column in range(len(positions))
                ]
                for row in range(len(positions))
            ]
        else:
            grouped: dict[str, list[int]] = {}
            for index, position in enumerate(positions):
                group = str(position.get(partition) or "Unclassified")
                grouped.setdefault(group, []).append(index)
            labels = sorted(grouped)
            matrix = []
            tooltips = []
            for row_label in labels:
                matrix_row = []
                tooltip_row = []
                for column_label in labels:
                    values: list[float] = []
                    counts: list[int] = []
                    for row_index in grouped[row_label]:
                        for column_index in grouped[column_label]:
                            value = source_matrix[row_index][column_index]
                            if value is not None:
                                values.append(float(value))
                                counts.append(int(observation_counts[row_index][column_index]))
                    matrix_row.append(sum(values) / len(values) if values else None)
                    mean_count = sum(counts) / len(counts) if counts else 0.0
                    tooltip_row.append(
                        f"Mean of {len(values):,} available bond-pair estimates\n"
                        f"Mean shared daily observations per estimate: {mean_count:.1f}"
                    )
                matrix.append(matrix_row)
                tooltips.append(tooltip_row)

        self.covariance_table.clear()
        self.covariance_table.setRowCount(len(labels))
        self.covariance_table.setColumnCount(len(labels))
        self.covariance_table.setHorizontalHeaderLabels(labels)
        self.covariance_table.setVerticalHeaderLabels(labels)
        is_correlation = metric_key == "correlation"
        for row_index, row_values in enumerate(matrix):
            for column_index, value in enumerate(row_values):
                item = QTableWidgetItem("—" if value is None else (f"{float(value):.3f}" if is_correlation else f"{float(value):,.2f}"))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(tooltips[row_index][column_index])
                if value is None:
                    item.setBackground(QColor("#334155"))
                    item.setForeground(QColor("#cbd5e1"))
                else:
                    color = self._covariance_color(float(value))
                    item.setBackground(color)
                    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
                    item.setForeground(QColor("#0b1220" if luminance > 145 else "#ffffff"))
                self.covariance_table.setItem(row_index, column_index, item)
        unit = "correlation" if is_correlation else "bps²"
        self.covariance_status.setText(
            f"{self.covariance_result['series_count']:,} bonds | {self.covariance_result['window_start_date']} to "
            f"{self.covariance_result['window_end_date']} | showing {len(labels):,} "
            f"{('bonds' if partition == 'bond' else self.covariance_partition.currentText().lower() + ' groups')} | "
            f"{unit}; blanks have fewer than {self.covariance_result['minimum_pair_observations']} shared observations."
        )

    def _analyze(self) -> None:
        index = self.archive_selector.currentIndex()
        if index < 0:
            QMessageBox.information(self, "No archived portfolio", "Save or select a portfolio first.")
            return
        end_date = date.fromisoformat(self.end_date.date().toString("yyyy-MM-dd"))
        self.analyze_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.progress.setRange(0, len(self.archives[index].get("positions", [])))
        self.progress.setValue(0)
        self.status.setText("Calculating horizon P&L from dated TRACE prices and bond cash flows...")
        self.worker = AnalysisWorker(
            self.archives[index],
            end_date,
            self.starting_nav.value(),
            self.max_price_age.value(),
            self,
        )
        self.worker.progress.connect(self._progress_changed)
        self.worker.succeeded.connect(self._analysis_succeeded)
        self.worker.failed.connect(self._analysis_failed)
        self.worker.start()

    def _progress_changed(self, completed: int, total: int) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(completed)
        self.status.setText(f"Calculating position returns and daily marks: {completed:,} of {total:,}")

    def _analysis_succeeded(self, result: PortfolioHorizonAnalysis) -> None:
        self.worker = None
        self.analyze_button.setEnabled(True)
        self.analysis = result
        self.export_button.setEnabled(True)
        self._populate_results()

    def _analysis_failed(self, message: str) -> None:
        self.worker = None
        self.analyze_button.setEnabled(True)
        self.status.setText("Portfolio analysis failed.")
        QMessageBox.warning(self, "Could not calculate P&L", message)

    def _populate_results(self) -> None:
        if self.analysis is None:
            return
        analysis = self.analysis
        summary = analysis.summary
        average_coupon = summary["weighted_average_coupon_pct"]
        average_yield = summary["weighted_average_reported_yield_pct"]
        average_maturity = summary["weighted_average_maturity_years"]
        average_duration = summary["weighted_average_modified_duration_years"]
        covered_return = summary["return_on_covered_positions_pct"]
        summary_lines = [
            f"Portfolio {analysis.archive_id[:8]} | formed {analysis.portfolio_as_of_date} | horizon {analysis.horizon_end_date}",
            f"Positions: {summary['position_count']:,} | priced: {summary['priced_position_count']:,} | "
            f"unpriced: {summary['unpriced_position_count']:,} | marked coverage: {summary['coverage_pct']:.1f}% "
            f"({summary['covered_weight']:.1%} of original weights) | stale end marks: "
            f"{summary['stale_mark_weight_pct']:.1f}% weight",
            f"Covered-position P&L: {summary['covered_pnl']:,.2f} NAV units | "
            f"contribution to starting NAV: {summary['covered_return_contribution_pct_nav']:.3f}% | "
            f"return on priced subset: {covered_return:.3f}%" if covered_return is not None else
            f"Covered-position P&L: {summary['covered_pnl']:,.2f} NAV units | no priced return coverage",
            f"Starting NAV: {analysis.starting_nav:,.2f} | max start price age: "
            f"{analysis.max_price_age_days if analysis.max_price_age_days is not None else 'unlimited'} days",
            f"Weighted coupon: {average_coupon:.2f}% ({summary['coupon_weight_coverage_pct']:.1f}% weight covered)"
            if average_coupon is not None else "Weighted coupon: unavailable",
            f"Weighted reported YTM: {average_yield:.2f}% ({summary['yield_weight_coverage_pct']:.1f}% weight covered)"
            if average_yield is not None else "Weighted reported YTM: unavailable",
            f"Weighted modified duration: {average_duration:.2f} years "
            f"({summary['duration_weight_coverage_pct']:.1f}% weight covered)"
            if average_duration is not None else "Weighted modified duration: unavailable",
            f"Weighted remaining maturity: {average_maturity:.2f} years | "
            f"issuers: {summary['number_of_issuers']:,} | sectors: {summary['number_of_sectors']:,} | "
            f"largest issuer: {summary['largest_issuer']} ({summary['largest_issuer_weight_pct']:.1f}%)",
            "P&L attribution: clean-price move + coupon cash and accrued-interest change + principal repayment. "
            "Original weights are held constant with no rebalancing; unpriced weights are not redistributed. "
            "Coupon and redemption proceeds earn zero after receipt.",
        ]
        self.summary.setText("\n".join(summary_lines))
        self.status.setText("Analysis complete. See coverage and unpriced reasons in Bond attribution.")
        self._populate_position_table()
        self._refresh_contributor_views()
        self.time_series_chart.set_data(analysis.time_series)
        self.performance_summary.setText(self._format_performance_summary(summary))
        self._populate_exposure_table(
            self.sector_exposure_table,
            analysis.sector_attribution,
            "Sector",
        )
        self._populate_exposure_table(
            self.maturity_exposure_table,
            analysis.maturity_attribution,
            "Maturity bucket",
        )
        self._populate_exposure_table(
            self.issuer_exposure_table,
            analysis.issuer_attribution,
            "Issuer",
        )
        self._update_chart()

    @staticmethod
    def _format_performance_summary(summary: dict[str, Any]) -> str:
        def fmt(key: str, digits: int = 2, suffix: str = "") -> str:
            value = summary.get(key)
            return f"{value:.{digits}f}{suffix}" if value is not None else "n/a"

        peak_date = summary.get("max_drawdown_peak_date")
        trough_date = summary.get("max_drawdown_trough_date")
        drawdown_dates = (
            f"{peak_date} to {trough_date}"
            if peak_date is not None and trough_date is not None
            else "n/a"
        )
        return (
            f"Cumulative return {fmt('cumulative_return_pct', suffix='%')} | "
            f"Annualized return {fmt('annualized_return_pct', suffix='%')} | "
            f"Annualized volatility {fmt('annualized_volatility_pct', suffix='%')} | "
            f"Sharpe (0% risk-free) {fmt('sharpe_ratio')} | Sortino (0% target) {fmt('sortino_ratio')} | "
            f"Calmar {fmt('calmar_ratio')}\n"
            f"Max drawdown {fmt('max_drawdown_pct', suffix='%')} ({drawdown_dates}; "
            f"{summary.get('max_drawdown_duration_days', 'n/a')} calendar days) | "
            f"Positive marks {fmt('positive_day_pct', suffix='%')} | "
            f"Best/worst day {fmt('best_day_return_pct', suffix='%')} / "
            f"{fmt('worst_day_return_pct', suffix='%')} | "
            f"Historical 95% VaR / CVaR {fmt('historical_var_95_pct', suffix='%')} / "
            f"{fmt('historical_cvar_95_pct', suffix='%')}\n"
            f"Sample: {summary.get('performance_observation_count', 0):,} daily intervals. "
            "Annualization assumes 252 weekday marks. Unpriced weights contribute zero P&L; "
            "the daily series carries forward the last available price mark."
        )

    def _populate_position_table(self) -> None:
        if self.analysis is None:
            return
        table = self.position_table
        table.setRowCount(len(self.analysis.positions))
        keys = (
            "isin", "issuer_name", "sector", "maturity_date", "maturity_bucket", "weight",
            "start_price_date", "end_price_date", "start_price_age_days", "end_price_age_days",
            "price_move_pnl", "carry_pnl", "principal_pnl", "pnl", "bond_total_return_pct", "error",
        )
        for row_index, position in enumerate(self.analysis.positions):
            for column, key in enumerate(keys):
                value = position.get("status") if key == "error" and position["status"] == "priced" else position.get(key)
                self._set_item(table, row_index, column, value, key)

    def _populate_group_table(self, table: QTableWidget, rows: tuple[dict[str, Any], ...]) -> None:
        rows = tuple(sorted(rows, key=lambda item: abs(float(item["pnl"])), reverse=True))
        rows = rows[: self.top_contributors.value()]
        table.setRowCount(len(rows))
        keys = (
            "name", "positions", "exposure_pct", "coverage_pct", "price_move_pnl",
            "carry_pnl", "principal_pnl", "pnl", "pnl_pct_nav",
        )
        for row_index, item in enumerate(rows):
            for column, key in enumerate(keys):
                self._set_item(table, row_index, column, item.get(key), key)

    def _refresh_contributor_views(self, *_args: Any) -> None:
        if self.analysis is None:
            return
        self._populate_group_table(self.sector_table, self.analysis.sector_attribution)
        self._populate_group_table(self.issuer_table, self.analysis.issuer_attribution)
        self._populate_group_table(self.maturity_table, self.analysis.maturity_attribution)
        count = self.top_contributors.value()
        self.tabs.setTabText(self.tabs.indexOf(self.sector_table), f"Sector attribution (top {count})")
        self.tabs.setTabText(self.tabs.indexOf(self.issuer_table), f"Issuer attribution (top {count})")
        self.tabs.setTabText(self.tabs.indexOf(self.maturity_table), f"Maturity attribution (top {count})")
        self._update_chart()

    def _populate_exposure_table(
        self,
        table: QTableWidget,
        rows: tuple[dict[str, Any], ...],
        first_header: str,
    ) -> None:
        table.setHorizontalHeaderItem(0, QTableWidgetItem(first_header))
        table.setRowCount(len(rows))
        for row_index, item in enumerate(sorted(rows, key=lambda row: row["exposure_pct"], reverse=True)):
            self._set_item(table, row_index, 0, item["name"], "name")
            self._set_item(table, row_index, 1, item["positions"], "positions")
            self._set_item(table, row_index, 2, item["exposure_pct"], "exposure_pct")

    @staticmethod
    def _set_item(table: QTableWidget, row: int, column: int, value: Any, key: str) -> None:
        if value is None:
            display = ""
        elif isinstance(value, date):
            display = value.isoformat()
        elif key == "weight":
            display = f"{float(value):.2%}"
        elif key.endswith("pct") or key.endswith("pct_nav") or key == "exposure_pct":
            display = f"{float(value):.2f}%"
        elif isinstance(value, (int, float)):
            display = f"{value:,.2f}"
        else:
            display = str(value)
        table.setItem(row, column, QTableWidgetItem(display))

    def _update_chart(self, *_args: Any) -> None:
        if self.analysis is None:
            self.chart.set_data("Portfolio attribution", [], "number")
            return
        mode = self.chart_mode.currentText()
        if mode == "P&L by sector":
            rows = sorted(
                ((item["name"], item["pnl"]) for item in self.analysis.sector_attribution),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            rows = rows[: self.top_contributors.value()]
            self.chart.set_data("P&L by sector (NAV units)", rows, "number")
        elif mode == "P&L by issuer":
            rows = sorted(
                ((item["name"], item["pnl"]) for item in self.analysis.issuer_attribution),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            rows = rows[: self.top_contributors.value()]
            self.chart.set_data("P&L by issuer (NAV units)", rows, "number")
        elif mode == "P&L by maturity bucket":
            rows = sorted(
                ((item["name"], item["pnl"]) for item in self.analysis.maturity_attribution),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            rows = rows[: self.top_contributors.value()]
            self.chart.set_data("P&L by maturity bucket (NAV units)", rows, "number")
        elif mode == "P&L by bond":
            rows = sorted(
                ((f"{item['issuer_name']} | {item['isin']}", float(item.get("pnl", 0.0)))
                 for item in self.analysis.positions if item["status"] == "priced"),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            rows = rows[: self.top_contributors.value()]
            self.chart.set_data(
                f"Top {len(rows)} bond P&L contributions (NAV units)", rows, "number"
            )
        elif mode == "Exposure by sector":
            rows = sorted(
                ((item["name"], item["exposure_pct"]) for item in self.analysis.sector_attribution),
                key=lambda item: item[1],
                reverse=True,
            )
            self.chart.set_data("Exposure by sector (%)", rows, "percent")
        elif mode == "Exposure by issuer":
            rows = sorted(
                ((item["name"], item["exposure_pct"]) for item in self.analysis.issuer_attribution),
                key=lambda item: item[1],
                reverse=True,
            )
            self.chart.set_data("Exposure by issuer (%)", rows, "percent")
        else:
            rows = sorted(
                ((item["name"], item["exposure_pct"]) for item in self.analysis.maturity_attribution),
                key=lambda item: item[1],
                reverse=True,
            )
            self.chart.set_data("Exposure by maturity bucket (%)", rows, "percent")

    def _export(self) -> None:
        if self.analysis is None:
            return
        suggested = Path.cwd() / (
            f"horizon_attribution_{self.analysis.portfolio_as_of_date}_"
            f"{self.analysis.horizon_end_date}_{self.analysis.archive_id[:8]}.csv"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self, "Export bond attribution", str(suggested), "CSV files (*.csv)"
        )
        if not selected:
            return
        metadata = {
            "archive_id": self.analysis.archive_id,
            "portfolio_as_of_date": self.analysis.portfolio_as_of_date.isoformat(),
            "horizon_end_date": self.analysis.horizon_end_date.isoformat(),
            "starting_nav": self.analysis.starting_nav,
            "coverage_pct": self.analysis.summary["coverage_pct"],
        }
        position_fields = list(dict.fromkeys(
            key for position in self.analysis.positions for key in position
        ))
        fieldnames = list(metadata) + position_fields
        try:
            with Path(selected).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for position in self.analysis.positions:
                    writer.writerow(
                        {
                            **metadata,
                            **{
                                key: value.isoformat() if isinstance(value, date) else value
                                for key, value in position.items()
                            },
                        }
                    )
        except OSError as error:
            QMessageBox.warning(self, "Could not export attribution", str(error))
            return
        self.status.setText(f"Exported bond attribution: {selected}")


def main() -> None:
    app = QApplication(sys.argv)
    window = PortfolioAnalyticsWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
