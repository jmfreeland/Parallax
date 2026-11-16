"""Desktop point-in-time evaluation for exploratory credit predictors."""

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
    QCheckBox,
    QComboBox,
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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .signal_evaluation import (
    PREDICTOR_NELSON_SIEGEL,
    PREDICTOR_PARTITION_PERCENTILE,
    evaluate_nelson_siegel_signal,
)
from .trace_history import DATABASE_PATH, DEFAULT_SNAPSHOT, TRACE_DIR
from .trace_cleaning import TRACE_SERIES_CLEANING_VERSION


class HorizonCurvesChart(QWidget):
    """Show event-time side-adjusted performance and cross-sectional IC."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(300)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        rows = self.rows
        if not rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Run an evaluation to plot horizon curves.")
            painter.end()
            return

        deciles = [row["decile_average_outperformance_bps"] for row in rows]
        left, right = 76.0, float(self.width() - 24)
        top, bottom = 38.0, float(self.height() - 76)
        x_max = max(1.0, float(rows[-1]["horizon_days"]))
        valid_values = [float(value) for series in deciles for value in series if value is not None]
        y_min = min(0.0, min(valid_values, default=0.0))
        y_max = max(0.0, max(valid_values, default=0.0))
        pad = max(1.0, (y_max - y_min) * 0.12)
        y_min, y_max = y_min - pad, y_max + pad
        x_map = lambda day: left + day / x_max * (right - left)
        y_map = lambda value: bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 24), Qt.AlignmentFlag.AlignLeft,
                         "D1 = lowest signal, D10 = highest; cumulative yield outperformance vs. population")
        for step in range(5):
            fraction = step / 4
            y_value = y_min + fraction * (y_max - y_min)
            y = y_map(y_value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 66, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{y_value:.1f}")
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(4, top - 24, 70, 18), Qt.AlignmentFlag.AlignRight, "Outperf. (bp)")
        tick_step = max(1, round(x_max / 4))
        tick_days = sorted({0, 1, *range(tick_step, int(x_max), tick_step), int(x_max)})
        for tick_day in tick_days:
            x = x_map(float(tick_day))
            painter.setPen(QPen(QColor("#1b293b"), 1))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(x - 24, bottom + 2, 48, 16), Qt.AlignmentFlag.AlignCenter,
                             f"T+{tick_day}")
        painter.drawText(QRectF(left, bottom + 20, right - left, 18), Qt.AlignmentFlag.AlignCenter,
                         "T+0 is the signal date; forward outcome observations begin at T+1")
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(left, y_map(0.0)), QPointF(right, y_map(0.0)))

        colors = ("#64748b", "#38bdf8", "#22c55e", "#a3e635", "#facc15",
                  "#fb923c", "#f97316", "#f43f5e", "#e879f9", "#c084fc")
        def draw_series(values: list[Any], color: str) -> None:
            path = QPainterPath()
            path.moveTo(QPointF(x_map(0.0), y_map(0.0)))
            drawing = True
            for row, value in zip(rows, values):
                if value is None:
                    drawing = False
                    continue
                point = QPointF(x_map(float(row["horizon_days"])), y_map(float(value)))
                if drawing:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                drawing = True
            painter.setPen(QPen(QColor(color), 2.0))
            painter.drawPath(path)
            for row, value in zip(rows, values):
                if value is not None:
                    painter.setBrush(QColor(color))
                    painter.setPen(QPen(QColor(color), 1))
                    painter.drawEllipse(QPointF(x_map(float(row["horizon_days"])), y_map(float(value))), 2.4, 2.4)

        legend_y = self.height() - 16
        column_width = max(60, (right - left) / 5)
        for decile, color in enumerate(colors):
            draw_series([series[decile] for series in deciles], color)
            legend_row, legend_col = divmod(decile, 5)
            legend_x = left + legend_col * column_width
            y = legend_y - legend_row * 16
            painter.setPen(QPen(QColor(color), 2.0))
            painter.drawLine(QPointF(legend_x, y), QPointF(legend_x + 16, y))
            painter.setPen(QColor("#e2e8f0"))
            painter.drawText(QRectF(legend_x + 20, y - 8, column_width - 22, 16),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             f"D{decile + 1}")
        painter.end()


class CorrelationHorizonChart(QWidget):
    """Show predictive correlations separately from decile returns."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(260)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        if not self.rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Run an evaluation to plot correlations.")
            painter.end()
            return
        left, right = 76.0, float(self.width() - 24)
        top, bottom = 38.0, float(self.height() - 44)
        x_min, x_max = float(self.rows[0]["horizon_days"]), float(self.rows[-1]["horizon_days"])
        if x_max <= x_min:
            x_max = x_min + 1.0
        x_map = lambda day: left + (day - x_min) / (x_max - x_min) * (right - left)
        y_map = lambda value: bottom - (value + 1.0) / 2.0 * (bottom - top)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 22), Qt.AlignmentFlag.AlignLeft,
                         "Mean daily Pearson and Spearman IC")
        for step in range(5):
            value = -1.0 + step * 0.5
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(8, y - 9, 60, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{value:.1f}")
        colors = (("mean_daily_side_adjusted_pearson", "#38bdf8"),
                  ("mean_daily_side_adjusted_spearman", "#fbbf24"))
        for field, color in colors:
            path = QPainterPath()
            drawing = False
            for row in self.rows:
                value = row[field]
                if value is None:
                    drawing = False
                    continue
                point = QPointF(x_map(float(row["horizon_days"])), y_map(float(value)))
                if drawing:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                drawing = True
            painter.setPen(QPen(QColor(color), 2.2))
            painter.drawPath(path)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, bottom + 6, right - left, 18), Qt.AlignmentFlag.AlignCenter,
                         "Calendar days after signal")
        painter.setPen(QPen(QColor("#38bdf8"), 2.2))
        painter.drawLine(QPointF(left + 10, self.height() - 12), QPointF(left + 30, self.height() - 12))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left + 36, self.height() - 20, 90, 16), Qt.AlignmentFlag.AlignLeft, "Pearson")
        painter.setPen(QPen(QColor("#fbbf24"), 2.2))
        painter.drawLine(QPointF(left + 130, self.height() - 12), QPointF(left + 150, self.height() - 12))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left + 156, self.height() - 20, 100, 16), Qt.AlignmentFlag.AlignLeft, "Spearman")
        painter.end()


class SignalReturnScatterChart(QWidget):
    """Plot one signal-date cross-section against its forward yield outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(360)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        if not self.rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Select a signal date with available forward observations.")
            painter.end()
            return

        xs = [float(row["signal_value_pct"]) for row in self.rows]
        ys = [float(row["side_adjusted_forward_yield_change_bps"]) for row in self.rows]
        left, right = 82.0, float(self.width() - 28)
        top, bottom = 38.0, float(self.height() - 64)
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x_pad = max((x_max - x_min) * 0.08, 0.05)
        y_pad = max((y_max - y_min) * 0.08, 1.0)
        x_min, x_max = x_min - x_pad, x_max + x_pad
        y_min, y_max = y_min - y_pad, y_max + y_pad
        x_map = lambda value: left + (value - x_min) / (x_max - x_min) * (right - left)
        y_map = lambda value: bottom - (value - y_min) / (y_max - y_min) * (bottom - top)
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        x_variance_sum = sum((value - x_mean) ** 2 for value in xs)
        fit_line: tuple[float, float] | None = None
        if len(xs) >= 2 and x_variance_sum > 0:
            slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / x_variance_sum
            fit_line = (slope, y_mean - slope * x_mean)

        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 22), Qt.AlignmentFlag.AlignLeft,
                         f"{len(self.rows):,} bonds | side-adjusted yield-change outperformance vs population")
        for fraction in range(5):
            ratio = fraction / 4
            y_value = y_min + ratio * (y_max - y_min)
            y = y_map(y_value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 72, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{y_value:.1f}")
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(4, top - 23, 72, 18), Qt.AlignmentFlag.AlignRight, "Outperf. (bp)")
        painter.drawText(QRectF(left, bottom + 22, right - left, 20), Qt.AlignmentFlag.AlignCenter,
                         "Signal value (percentile 0–100 or yield residual, pp)")
        if y_min <= 0 <= y_max:
            painter.setPen(QPen(QColor("#64748b"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left, y_map(0.0)), QPointF(right, y_map(0.0)))
        if fit_line is not None:
            slope, intercept = fit_line
            painter.setPen(QPen(QColor("#fbbf24"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(x_map(min(xs)), y_map(slope * min(xs) + intercept)),
                QPointF(x_map(max(xs)), y_map(slope * max(xs) + intercept)),
            )
        painter.setPen(QPen(QColor("#38bdf8"), 1))
        painter.setBrush(QColor(56, 189, 248, 170))
        for x_value, y_value in zip(xs, ys):
            painter.drawEllipse(QPointF(x_map(x_value), y_map(y_value)), 4.0, 4.0)
        if fit_line is not None:
            painter.setPen(QPen(QColor("#fbbf24"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left + 18, self.height() - 13), QPointF(left + 44, self.height() - 13))
            painter.setPen(QColor("#e2e8f0"))
            painter.drawText(QRectF(left + 50, self.height() - 22, 125, 18), Qt.AlignmentFlag.AlignLeft, "OLS best-fit line")
        painter.end()


class RebalancedPortfolioChart(QWidget):
    """Plot raw rebalanced portfolio returns and financing-adjusted long-only returns."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.setMinimumHeight(300)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        rows = self.rows
        if not rows:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No adjacent signal dates had eligible long and short holdings.")
            painter.end()
            return
        series = (
            ("covered_total_return_nav_index", "Long-short total return", "#38bdf8"),
            ("long_only_gross_nav_index", "Long-only gross", "#fbbf24"),
            ("long_only_net_nav_index", "Long-only net of financing", "#fb7185"),
            ("yield_only_nav_index", "Yield-only L/S proxy", "#34d399"),
        )
        values = [
            float(row[key]) for row in rows for key, _, _ in series if row.get(key) is not None
        ]
        if not values:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No rebalanced return observations are available.")
            painter.end()
            return
        minimum, maximum = min(values), max(values)
        pad = max(0.1, (maximum - minimum) * 0.12)
        minimum, maximum = minimum - pad, maximum + pad
        left, right = 76.0, float(self.width() - 24)
        top, bottom = 42.0, float(self.height() - 52)
        x_map = lambda index: left + index / max(len(rows) - 1, 1) * (right - left)
        y_map = lambda value: bottom - (value - minimum) / (maximum - minimum) * (bottom - top)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 24), Qt.AlignmentFlag.AlignLeft,
                         "Raw rebalanced indices; financing-adjusted long-only comparison; no de-meaning")
        for step in range(5):
            fraction = step / 4
            value = minimum + fraction * (maximum - minimum)
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 66, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:.2f}")
        for field, _label, color in series:
            path = QPainterPath()
            drawing = False
            for index, row in enumerate(rows):
                value = row.get(field)
                if value is None:
                    drawing = False
                    continue
                point = QPointF(x_map(index), y_map(float(value)))
                if not drawing:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
                drawing = True
            painter.setPen(QPen(QColor(color), 2.5))
            painter.drawPath(path)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(left, bottom + 7, right - left, 20), Qt.AlignmentFlag.AlignCenter,
                         f"{rows[0]['holding_date']}  to  {rows[-1]['holding_date']}  |  Initial index 100")
        legend_width = (right - left) / len(series)
        for index, (_field, label, color) in enumerate(series):
            legend_x = left + index * legend_width
            painter.setPen(QPen(QColor(color), 2.5))
            painter.drawLine(QPointF(legend_x, self.height() - 13), QPointF(legend_x + 18, self.height() - 13))
            painter.setPen(QColor("#e2e8f0"))
            painter.drawText(QRectF(legend_x + 24, self.height() - 22, legend_width - 28, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()


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
        self.setWindowTitle("Signal Evaluation - Credit Predictors")
        self.resize(1500, 900)
        self.worker: EvaluationWorker | None = None
        self.result: dict[str, Any] | None = None
        self.scatter_rows_by_date: dict[str, list[dict[str, Any]]] = {}

        central = QWidget()
        layout = QVBoxLayout(central)
        source = QHBoxLayout()
        source.addWidget(QLabel("Bond list"))
        self.bond_list = QLineEdit(str(DEFAULT_SNAPSHOT))
        self.bond_list.textChanged.connect(self._refresh_partition_fields)
        source.addWidget(self.bond_list, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        source.addWidget(browse)
        layout.addLayout(source)

        data_source_row = QHBoxLayout()
        data_source_row.addWidget(QLabel("TRACE data source"))
        self.data_source = QComboBox()
        self.data_source.addItem("Cleaned Parquet (default)", "cleaned_parquet")
        self.data_source.addItem("Preprocessed Parquet", "preprocessed_parquet")
        self.data_source.addItem("Raw DuckDB history", "raw_duckdb")
        data_source_row.addWidget(self.data_source)
        self.data_path = QLineEdit()
        data_source_row.addWidget(self.data_path, 1)
        self.data_browse = QPushButton("Browse…")
        self.data_browse.clicked.connect(self._browse_data_source)
        data_source_row.addWidget(self.data_browse)
        self.data_source_paths = {
            "cleaned_parquet": self._latest_dataset_path("clean_id="),
            "preprocessed_parquet": self._latest_dataset_path("preprocess_id="),
        }
        self.data_path.textChanged.connect(self._remember_data_path)
        self.data_source.currentIndexChanged.connect(self._data_source_changed)
        self._data_source_changed()
        layout.addLayout(data_source_row)

        predictor_row = QHBoxLayout()
        predictor_row.addWidget(QLabel("Predictor"))
        self.predictor = QComboBox()
        self.predictor.addItem("Nelson-Siegel ticker residual", PREDICTOR_NELSON_SIEGEL)
        self.predictor.addItem("Historical partition percentile", PREDICTOR_PARTITION_PERCENTILE)
        self.predictor.currentIndexChanged.connect(self._predictor_changed)
        predictor_row.addWidget(self.predictor)
        predictor_row.addWidget(QLabel("Partition field"))
        self.partition_field = QComboBox()
        predictor_row.addWidget(self.partition_field)
        predictor_row.addWidget(QLabel("Percentile lookback (days)"))
        self.percentile_lookback = QSpinBox()
        self.percentile_lookback.setRange(30, 3650)
        self.percentile_lookback.setValue(1095)
        predictor_row.addWidget(self.percentile_lookback)
        predictor_row.addWidget(QLabel("Minimum history points"))
        self.minimum_history = QSpinBox()
        self.minimum_history.setRange(5, 500)
        self.minimum_history.setValue(20)
        predictor_row.addWidget(self.minimum_history)
        layout.addLayout(predictor_row)

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
        row_one.addWidget(QLabel("Minimum bonds per group"))
        self.minimum_bonds = QSpinBox()
        self.minimum_bonds.setRange(1, 100)
        self.minimum_bonds.setValue(5)
        row_one.addWidget(self.minimum_bonds)
        layout.addLayout(row_one)

        row_two = QHBoxLayout()
        row_two.addWidget(QLabel("Max signal age (days)"))
        self.signal_age = QSpinBox()
        self.signal_age.setRange(0, 365)
        self.signal_age.setValue(30)
        row_two.addWidget(self.signal_age)
        row_two.addWidget(QLabel("Max gap between yield changes (days)"))
        self.change_gap = QSpinBox()
        self.change_gap.setRange(1, 90)
        self.change_gap.setValue(7)
        row_two.addWidget(self.change_gap)
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

        self.status = QLabel(f"Credit signal evaluation | default cleaned dataset under: {TRACE_DIR / 'preprocessed'}")
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
            "Select a predictor, partition, and test range. Positive scores mean high yield versus the selected reference. "
            "Forward yield changes use the predictor's matching outcome convention and are side-adjusted so positive is favorable. "
            "TRACE yield changes are a spread proxy (no Treasury benchmark). Historical returns "
            "and the rebalanced NAV proxy are shown separately. TRACE observations are not stored in immutable vintages, "
            "so later revisions to past observations may affect historical results."
        )
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dates_layout.addWidget(self.summary)
        self.date_table = QTableWidget(0, 11)
        self.date_table.setHorizontalHeaderLabels([
            "Signal date", "Horizon end", "Signals with forward changes", "Groups/signals",
            "Side-adjust Pearson IC", "Side-adjust Spearman IC", "Long positions", "Short positions",
            "L/S total return (%)", "Price coverage (%)", "Yield-only proxy (bps)",
        ])
        self.date_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.date_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.date_table.horizontalHeader().setStretchLastSection(True)
        self.date_table.currentCellChanged.connect(self._show_date_details)
        dates_layout.addWidget(self.date_table, 1)
        self.tabs.addTab(dates_page, "Date performance")

        horizon_page = QWidget()
        horizon_layout = QVBoxLayout(horizon_page)
        self.horizon_chart = HorizonCurvesChart()
        horizon_layout.addWidget(self.horizon_chart, 3)
        self.horizon_table = QTableWidget(0, 11)
        self.horizon_table.setHorizontalHeaderLabels([
            "Horizon (days)", *[f"D{decile} outperformance (bps)" for decile in range(1, 11)],
        ])
        self.horizon_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.horizon_table.horizontalHeader().setStretchLastSection(True)
        horizon_layout.addWidget(self.horizon_table, 2)
        self.tabs.addTab(horizon_page, "Horizon performance")

        correlation_page = QWidget()
        correlation_layout = QVBoxLayout(correlation_page)
        self.correlation_chart = CorrelationHorizonChart()
        correlation_layout.addWidget(self.correlation_chart, 2)
        self.correlation_table = QTableWidget(0, 6)
        self.correlation_table.setHorizontalHeaderLabels([
            "Horizon (days)", "Dates with IC", "Mean daily Pearson", "Mean daily Spearman",
            "Pooled Pearson", "Pooled Spearman",
        ])
        self.correlation_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.correlation_table.horizontalHeader().setStretchLastSection(True)
        correlation_layout.addWidget(self.correlation_table, 1)
        self.tabs.addTab(correlation_page, "Signal correlations")

        scatter_page = QWidget()
        scatter_layout = QVBoxLayout(scatter_page)
        scatter_controls = QHBoxLayout()
        scatter_controls.addWidget(QLabel("Signal date"))
        self.scatter_date = QComboBox()
        self.scatter_date.currentIndexChanged.connect(self._update_scatter_plot)
        scatter_controls.addWidget(self.scatter_date, 1)
        scatter_controls.addWidget(QLabel("Forward horizon T+ (calendar days)"))
        self.scatter_horizon = QSpinBox()
        self.scatter_horizon.setRange(1, 365)
        self.scatter_horizon.setValue(5)
        self.scatter_horizon.valueChanged.connect(self._update_scatter_plot)
        scatter_controls.addWidget(self.scatter_horizon)
        scatter_layout.addLayout(scatter_controls)
        self.scatter_status = QLabel("Select a signal date to inspect its cross-section.")
        scatter_layout.addWidget(self.scatter_status)
        self.scatter_chart = SignalReturnScatterChart()
        scatter_layout.addWidget(self.scatter_chart, 1)
        self.tabs.addTab(scatter_page, "Signal vs. outcome")

        portfolio_page = QWidget()
        portfolio_layout = QVBoxLayout(portfolio_page)
        self.rebalance_financing = QCheckBox(
            "Apply 1-month Treasury + bond G-spread financing to long-only returns"
        )
        self.rebalance_financing.setChecked(True)
        self.rebalance_financing.setToolTip(
            "Proxy for financing: ACT/365 accrual on the latest 1-month Treasury rate plus the bond's latest G-spread. "
            "Uses the latest stored G-spread snapshot."
        )
        portfolio_layout.addWidget(self.rebalance_financing)
        self.rebalance_summary = QLabel(
            "Annualized volatility will be shown after evaluation. Funding uses the latest available G-spread snapshot."
        )
        self.rebalance_summary.setWordWrap(True)
        portfolio_layout.addWidget(self.rebalance_summary)
        self.portfolio_chart = RebalancedPortfolioChart()
        portfolio_layout.addWidget(self.portfolio_chart, 2)
        self.rebalance_table = QTableWidget(0, 13)
        self.rebalance_table.setHorizontalHeaderLabels([
            "Signal date", "Holding date", "Period days", "Long positions", "Short positions",
            "L/S total return (%)", "Yield-only L/S proxy (bps)", "Long-only gross (%)",
            "Financing cost (%)", "Long-only net (%)", "Financing coverage (%)",
            "Long-leg return coverage (%)", "L/S return coverage (%)",
        ])
        self.rebalance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rebalance_table.horizontalHeader().setStretchLastSection(True)
        portfolio_layout.addWidget(self.rebalance_table, 3)
        self.tabs.addTab(portfolio_page, "Rebalanced portfolio")

        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        self.detail_status = QLabel("Select a signal date to inspect the included bond observations and strategy legs.")
        details_layout.addWidget(self.detail_status)
        self.detail_table = QTableWidget(0, 13)
        self.detail_table.setHorizontalHeaderLabels([
            "Signal date", "Partition", "ISIN", "Signal value", "Signal yield date",
            "Raw cumulative change (bps)", "De-meaned cumulative change (bps)", "Side-adjusted change (bps)",
            "Strategy leg", "Bond total return (%)", "Return status", "Duration (years)", "Issuer",
        ])
        self.detail_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        details_layout.addWidget(self.detail_table)
        self.tabs.addTab(details_page, "Bond observations")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._refresh_partition_fields()

    @staticmethod
    def _latest_dataset_path(parent_prefix: str) -> str:
        root = TRACE_DIR / "preprocessed"
        if not root.exists():
            return ""
        candidates = [
            path for path in root.rglob("observations.parquet")
            if path.parent.name.startswith(parent_prefix)
            and (
                parent_prefix != "clean_id="
                or SignalEvaluationWindow._has_current_cleaning_version(path)
            )
        ]
        if not candidates:
            return ""
        return str(max(candidates, key=lambda path: path.stat().st_mtime))

    @staticmethod
    def _has_current_cleaning_version(path: Path) -> bool:
        try:
            report = json.loads((path.parent / "diagnostics.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return report.get("policy_version") == TRACE_SERIES_CLEANING_VERSION

    def _data_source_changed(self, *_args: Any) -> None:
        source = self.data_source.currentData()
        is_raw = source == "raw_duckdb"
        if is_raw:
            path = str(DATABASE_PATH)
        else:
            path = self.data_source_paths.get(source, "")
        self.data_path.setText(path)
        self.data_path.setReadOnly(is_raw)
        self.data_browse.setEnabled(not is_raw)
        if hasattr(self, "status"):
            self.status.setText(f"Selected {self.data_source.currentText()}: {path or 'no dataset selected'}")

    def _remember_data_path(self, path: str) -> None:
        source = self.data_source.currentData()
        if source != "raw_duckdb":
            self.data_source_paths[source] = path

    def _browse_data_source(self) -> None:
        source = self.data_source.currentData()
        current = self.data_path.text() or str(TRACE_DIR / "preprocessed")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select TRACE Parquet dataset",
            current,
            "Parquet files (*.parquet)",
        )
        if path:
            self.data_source_paths[source] = path
            self.data_path.setText(path)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Select bond list", self.bond_list.text(), "CSV files (*.csv)"
        )
        if selected:
            self.bond_list.setText(selected)

    def _refresh_partition_fields(self, *_args: Any) -> None:
        selected = self.partition_field.currentText()
        fields: list[str] = []
        try:
            with Path(self.bond_list.text()).expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
                fields = list(csv.DictReader(handle).fieldnames or [])
        except OSError:
            fields = []
        self.partition_field.blockSignals(True)
        self.partition_field.clear()
        self.partition_field.addItems(fields)
        if selected in fields:
            self.partition_field.setCurrentText(selected)
        elif "sector" in fields:
            self.partition_field.setCurrentText("sector")
        self.partition_field.blockSignals(False)
        self._predictor_changed()

    def _predictor_changed(self, *_args: Any) -> None:
        percentile_mode = self.predictor.currentData() == PREDICTOR_PARTITION_PERCENTILE
        self.partition_field.setEnabled(percentile_mode)
        self.percentile_lookback.setEnabled(percentile_mode)
        self.minimum_history.setEnabled(percentile_mode)
        minimum_bonds = 1 if percentile_mode else 5
        self.minimum_bonds.setMinimum(minimum_bonds)
        if self.minimum_bonds.value() < minimum_bonds:
            self.minimum_bonds.setValue(minimum_bonds)

    def _run_evaluation(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        start = date.fromisoformat(self.start_date.date().toString("yyyy-MM-dd"))
        end = date.fromisoformat(self.end_date.date().toString("yyyy-MM-dd"))
        if end < start:
            QMessageBox.warning(self, "Invalid date range", "Test end must be on or after test start.")
            return
        selected_data_source = self.data_source.currentData()
        selected_data_path = None
        if selected_data_source != "raw_duckdb":
            selected_data_path = Path(self.data_path.text()).expanduser() if self.data_path.text().strip() else None
            if selected_data_path is None or not selected_data_path.is_file():
                expected = "cleaned" if selected_data_source == "cleaned_parquet" else "preprocessed"
                QMessageBox.warning(
                    self,
                    "TRACE dataset required",
                    f"Select a {expected} observations.parquet dataset. To create a cleaned dataset, use the separate cleaner from the launcher.",
                )
                return
        parameters = {
            "start_date": start,
            "end_date": end,
            "horizon_days": self.horizon_days.value(),
            "bond_list_path": Path(self.bond_list.text()).expanduser(),
            "database_path": DATABASE_PATH,
            "data_source": selected_data_source,
            "data_path": selected_data_path,
            "minimum_bonds": self.minimum_bonds.value(),
            "maximum_signal_age_days": self.signal_age.value(),
            "maximum_yield_change_gap_days": self.change_gap.value(),
            "maximum_price_age_days": self.price_age.value(),
            "long_short_quantile": self.quantile.value(),
            "predictor_id": self.predictor.currentData(),
            "partition_field": self.partition_field.currentText() or "sector",
            "percentile_lookback_days": self.percentile_lookback.value(),
            "minimum_history_observations": self.minimum_history.value(),
            "include_financing_cost": self.rebalance_financing.isChecked(),
        }
        self.run_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.export_report_button.setEnabled(False)
        self.result = None
        self.date_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.status.setText(
            f"Evaluating from {self.data_source.currentText()}: "
            f"{selected_data_path if selected_data_path is not None else DATABASE_PATH}"
        )
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

    def _update_scatter_plot(self, *_args: Any) -> None:
        if self.result is None:
            return
        as_of = self.scatter_date.currentText()
        horizon = self.scatter_horizon.value()
        scatter_details = []
        if as_of:
            for item in self.scatter_rows_by_date.get(as_of, []):
                outcomes = item["side_adjusted_outcomes_by_horizon_bps"]
                if horizon > len(outcomes) or outcomes[horizon - 1] is None:
                    continue
                scatter_details.append({
                    **item,
                    "horizon_days": horizon,
                    "side_adjusted_forward_yield_change_bps": outcomes[horizon - 1],
                })
        self.scatter_chart.set_rows(scatter_details)
        self.scatter_status.setText(
            f"{as_of or 'Select a signal date'} | T+{horizon} calendar days | "
            f"{len(scatter_details):,} bonds with a forward TRACE yield observation"
        )

    def _show_results(self, result: dict[str, Any]) -> None:
        self.result = result
        date_rows = result["date_results"]
        detail_rows = result["bond_results"]
        self.scatter_rows_by_date = {}
        for item in result["scatter_results"]:
            self.scatter_rows_by_date.setdefault(item["as_of_date"], []).append(item)
        self.scatter_horizon.setMaximum(max(1, result["summary"]["horizon_days"]))
        self.scatter_date.blockSignals(True)
        self.scatter_date.clear()
        self.scatter_date.addItems([row["as_of_date"] for row in date_rows])
        self.scatter_date.blockSignals(False)
        self._fill_table(
            self.date_table,
            date_rows,
            (
                "as_of_date", "horizon_end_date", "signal_bonds_with_forward_change", "ticker_curves_fitted",
                "pearson_side_adjusted_outcome", "spearman_side_adjusted_outcome",
                "long_positions", "short_positions", "long_short_total_return_pct",
                "long_short_total_return_coverage_pct", "side_adjusted_long_short_yield_return_proxy_bps",
            ),
        )
        self.date_table.setHorizontalHeaderItem(
            3,
            QTableWidgetItem(
                "Ticker curves" if result["summary"]["signal_id"] == PREDICTOR_NELSON_SIEGEL
                else "Eligible partitions"
            ),
        )
        event_rows = result["event_curve_results"]
        self.horizon_chart.set_rows(event_rows)
        self._fill_table(
            self.horizon_table,
            event_rows,
            ("horizon_days", *[f"decile_{decile}_outperformance_bps" for decile in range(1, 11)]),
        )
        for row_index, row in enumerate(event_rows):
            dates_by_decile = row["decile_dates_with_returns"]
            bonds_by_decile = row["decile_mean_bond_counts"]
            for decile in range(10):
                cell = self.horizon_table.item(row_index, decile + 1)
                if cell is not None:
                    mean_bonds = bonds_by_decile[decile]
                    bonds_text = f"{mean_bonds:.1f}" if mean_bonds is not None else "n/a"
                    cell.setToolTip(
                        f"Averaged across {dates_by_decile[decile]} signal dates; "
                        f"mean observed bonds per date: {bonds_text}"
                    )
        self.correlation_chart.set_rows(event_rows)
        self._fill_table(
            self.correlation_table,
            event_rows,
            (
                "horizon_days", "dates_with_correlation", "mean_daily_side_adjusted_pearson",
                "mean_daily_side_adjusted_spearman", "pooled_side_adjusted_pearson",
                "pooled_side_adjusted_spearman",
            ),
        )
        rebalance_rows = result["rebalance_results"]
        self.portfolio_chart.set_rows(rebalance_rows)
        self._fill_table(
            self.rebalance_table,
            rebalance_rows,
            (
                "signal_date", "holding_date", "holding_period_days", "long_positions", "short_positions",
                "long_short_total_return_pct", "side_adjusted_yield_return_proxy_bps",
                "long_only_gross_return_pct", "long_only_financing_cost_pct",
                "long_only_net_return_pct", "financing_coverage_pct",
                "long_only_total_return_coverage_pct", "total_return_coverage_pct",
            ),
        )
        if date_rows:
            self.date_table.setCurrentCell(0, 0)
            self._show_date_details(0, 0, -1, -1)
        else:
            self.detail_table.setRowCount(0)
        summary = result["summary"]
        def format_volatility(value: Any) -> str:
            return f"{float(value):.2f}%" if value is not None else "n/a"
        def format_index(value: Any) -> str:
            return f"{float(value):.2f}" if value is not None else "n/a"

        if summary["rebalanced_financing_cost_included"]:
            self.rebalance_summary.setText(
                f"Long-short annualized volatility: "
                f"{format_volatility(summary['rebalanced_long_short_annualized_volatility_pct'])} | "
                f"Long-only gross/net annualized volatility: "
                f"{format_volatility(summary['rebalanced_long_only_gross_annualized_volatility_pct'])} / "
                f"{format_volatility(summary['rebalanced_long_only_net_annualized_volatility_pct'])} | "
                f"Long-only net ending index: "
                f"{format_index(summary['rebalanced_long_only_net_nav_index'])} | "
                f"Funding snapshot: {str(summary['rebalanced_financing_snapshot_id'])[:8]} | "
                "1-month Treasury + bond G-spread; raw period returns, without de-meaning."
            )
        else:
            self.rebalance_summary.setText(
                f"Long-short annualized volatility: "
                f"{format_volatility(summary['rebalanced_long_short_annualized_volatility_pct'])} | "
                f"Long-only gross annualized volatility: "
                f"{format_volatility(summary['rebalanced_long_only_gross_annualized_volatility_pct'])} | "
                "Financing cost disabled; long-only net comparison is omitted."
            )
        percentile_mode = summary["signal_id"] == PREDICTOR_PARTITION_PERCENTILE

        def display(value: Any, suffix: str = "") -> str:
            return "n/a" if value is None else f"{float(value):.3f}{suffix}"

        predictor_description = (
            f"{summary['partition_field']} group median TRACE yield ranked against strictly prior "
            f"{summary['percentile_lookback_days']}-day history; higher percentile means higher-yield regime. "
            f"Requires {summary['minimum_history_observations']} prior group observations. "
            "This is a yield-based spread proxy until government-relative spreads are available. "
            if percentile_mode else
            "Positive Nelson-Siegel residual means high yield versus the fitted issuer curve. "
        )
        outcome_description = (
            "Forward outcomes use raw, non-demeaned yield changes. "
            if percentile_mode else "Forward outcomes use cross-sectionally de-meaned yield changes. "
        )
        outcome_description += (
            "For stale signal yields, the first later print is excluded because it spans pre-signal time; "
            "forward changes resume after two post-signal prints. "
        )
        data_source_note = (
            "This cleaned dataset includes retrospective reversal flags, but the evaluator uses unchanged observed-value columns and ignores flags and carried marks. "
            if summary["data_source_contains_retrospective_outlier_flags"] else ""
        )
        self.summary.setText(
            f"{summary['signal_name']} | Source: {summary['data_source']} ({summary['data_source_path']}) | "
            f"{summary['test_start_date']} to {summary['test_end_date']} | "
            f"At T+{summary['horizon_days']}, mean daily side-adjusted Pearson/Spearman IC: "
            f"{display(summary['mean_daily_pearson_correlation_side_adjusted'])} / "
            f"{display(summary['mean_daily_spearman_correlation_side_adjusted'])}; pooled Spearman IC: "
            f"{display(summary['pooled_spearman_correlation_side_adjusted'])}. "
            f"Mean daily side-adjusted long-short performance: "
            f"{display(summary['mean_daily_long_short_yield_return_proxy_bps'], ' bps')}. "
            f"Mean actual total-return long-short: {display(summary['mean_daily_long_short_total_return_pct'], '%')} "
            f"on {summary['dates_with_total_return']} dates with priced positions (unpriced bonds contribute zero; mean leg coverage "
            f"{display(summary['mean_long_short_total_return_coverage_pct'], '%')}; positive on "
            f"{display(summary['total_return_positive_date_fraction'] * 100 if summary['total_return_positive_date_fraction'] is not None else None, '%')}). "
            f"Rebalanced indices from 100: long-short total return "
            f"{display(summary['rebalanced_covered_total_return_nav_index'])}, long-only net of financing "
            f"{display(summary['rebalanced_long_only_net_nav_index'])}, yield-only proxy "
            f"{display(summary['rebalanced_yield_only_nav_index'])}, across {summary['rebalanced_periods']} periods. "
            f"{predictor_description}{outcome_description}{data_source_note}"
            "positive side-adjusted IC/performance is favorable. "
            "The horizon total return uses a synthetic coupon schedule, treats unpriced positions as zero contribution, and excludes costs/defaults; horizon windows overlap. "
            "Rebalanced portfolio returns use raw, non-demeaned holding-period returns. See the Rebalanced portfolio tab for financing coverage and annualized volatility. Past TRACE revisions and the current bond-list snapshot may affect results."
        )
        self.status.setText(
            f"Completed {summary['signal_dates']:,} signal dates; "
            f"{summary['dates_with_correlation']:,} dates had enough pairs for correlation. "
            f"Excluded {summary['stale_first_prints_excluded']:,} first post-signal prints from stale yield baselines. "
            f"Source: {summary['data_source']}."
        )
        self.export_button.setEnabled(bool(detail_rows))
        self.export_report_button.setEnabled(bool(date_rows))

    def _show_date_details(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        if self.result is None or row < 0 or row >= len(self.result["date_results"]):
            self.detail_table.setRowCount(0)
            self.scatter_chart.set_rows([])
            return
        as_of = self.result["date_results"][row]["as_of_date"]
        details = [item for item in self.result["bond_results"] if item["as_of_date"] == as_of]
        self.scatter_date.blockSignals(True)
        self.scatter_date.setCurrentText(as_of)
        self.scatter_date.blockSignals(False)
        self._update_scatter_plot()
        percentile_mode = self.result["summary"]["signal_id"] == PREDICTOR_PARTITION_PERCENTILE
        self.detail_table.setHorizontalHeaderItem(
            1, QTableWidgetItem("Partition" if percentile_mode else "Ticker")
        )
        self.detail_table.setHorizontalHeaderItem(
            6,
            QTableWidgetItem(
                "Cumulative yield change (bps)" if percentile_mode
                else "De-meaned cumulative change (bps)"
            ),
        )
        self.detail_table.setHorizontalHeaderItem(
            3,
            QTableWidgetItem("Percentile (0-100)" if percentile_mode else "Yield residual (pp)"),
        )
        self.detail_status.setText(f"{as_of}: {len(details):,} bond-date observations; selected strategy legs are labelled Long/Short.")
        self._fill_table(
            self.detail_table,
            details,
            (
                "as_of_date", "partition_value" if percentile_mode else "ticker", "isin", "signal_value_pct", "yield_date",
                "raw_cumulative_yield_change_bps", "cumulative_demeaned_yield_change_bps",
                "side_adjusted_yield_change_bps", "strategy_leg", "bond_total_return_pct",
                "bond_total_return_status", "modified_duration_years", "issuer_name",
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
            f"{self.result['summary']['signal_id']}_observations.csv",
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
            f"{self.result['summary']['signal_id']}_report.json",
            "JSON files (*.json)",
        )
        if not path:
            return
        report = {
            "summary": self.result["summary"],
            "date_results": self.result["date_results"],
            "event_curve_results": self.result["event_curve_results"],
            "rebalance_results": self.result["rebalance_results"],
        }
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
