"""Visual Treasury curve updater and latest-vintage viewer."""

from __future__ import annotations

from datetime import date
import time
from typing import Any

import duckdb
from PySide6.QtCore import QPointF, QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .trace_history import DATABASE_PATH, connect_database
from .treasury_curves import NODE_FIELDS, update_treasury_curve


class CurveUpdateWorker(QThread):
    succeeded = Signal(object, object)
    failed = Signal(str)
    progress = Signal(int, int, str, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.started_at = 0.0

    def _report_progress(self, completed: int, total: int, message: str) -> None:
        self.progress.emit(completed, total, message, time.monotonic() - self.started_at)

    def run(self) -> None:
        self.started_at = time.monotonic()
        try:
            rows, manifest = update_treasury_curve(
                database_path=DATABASE_PATH,
                on_progress=self._report_progress,
            )
            self.succeeded.emit(rows, manifest)
        except Exception as error:
            self.failed.emit(str(error))


class TreasuryCurveChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(400)
        self.points: list[tuple[float, float]] = []
        self.observation_date: date | None = None

    def set_curve(self, row: dict[str, object] | None) -> None:
        self.points = []
        self.observation_date = None
        if row:
            self.observation_date = row["date"]  # type: ignore[assignment]
            self.points = [
                (tenor, float(row[f"yield_{tenor:g}y_pct"]))
                for tenor in NODE_FIELDS.values()
                if row.get(f"yield_{tenor:g}y_pct") is not None
            ]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        if not self.points:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No Treasury curve is stored yet. Select Update curve to download one.")
            painter.end()
            return

        left, right = 82.0, float(self.width() - 26)
        top, bottom = 42.0, float(self.height() - 54)
        x_max = max(point[0] for point in self.points)
        y_min = min(point[1] for point in self.points)
        y_max = max(point[1] for point in self.points)
        pad = max(0.25, (y_max - y_min) * 0.12)
        y_min, y_max = y_min - pad, y_max + pad
        x_map = lambda value: left + value / x_max * (right - left)
        y_map = lambda value: bottom - (value - y_min) / (y_max - y_min) * (bottom - top)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(QRectF(left, 8, right - left, 22), Qt.AlignmentFlag.AlignLeft,
                         f"U.S. Treasury nominal par yield curve | {self.observation_date} | percent")
        for step in range(5):
            value = y_min + step * (y_max - y_min) / 4
            y = y_map(value)
            painter.setPen(QPen(QColor("#263449"), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(4, y - 9, 68, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{value:.2f}")
        path = QPainterPath()
        for index, (tenor, yield_pct) in enumerate(self.points):
            point = QPointF(x_map(tenor), y_map(yield_pct))
            path.moveTo(point) if index == 0 else path.lineTo(point)
        painter.setPen(QPen(QColor("#38bdf8"), 2.6))
        painter.drawPath(path)
        painter.setBrush(QColor("#38bdf8"))
        for tenor, yield_pct in self.points:
            x, y = x_map(tenor), y_map(yield_pct)
            painter.drawEllipse(QPointF(x, y), 3.5, 3.5)
            label = f"{tenor:g}y" if tenor >= 1 else f"{tenor * 12:g}m"
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(QRectF(x - 22, bottom + 6, 44, 18), Qt.AlignmentFlag.AlignCenter, label)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(4, top - 23, 70, 18), Qt.AlignmentFlag.AlignRight, "Yield (%)")
        painter.end()


class TreasuryCurveWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Treasury Curve Updater")
        self.resize(1080, 660)
        self.worker: CurveUpdateWorker | None = None
        self._progress_message = ""
        self._progress_completed = 0
        self._progress_total = 1
        self._progress_elapsed = 0.0
        self._progress_received_at = 0.0
        self.curves_by_date: dict[str, dict[str, object]] = {}
        self.retrieved_at_by_date: dict[str, str] = {}
        central = QWidget()
        layout = QVBoxLayout(central)
        self.status = QLabel("Loading the latest stored Treasury curve...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setToolTip(
            "Each annual Treasury XML request is a batch; final steps write the CSV, DuckDB vintage and manifest."
        )
        layout.addWidget(self.progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._refresh_progress_status)
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("Observation date"))
        self.observation_date = QComboBox()
        self.observation_date.currentIndexChanged.connect(self._show_selected_curve)
        date_row.addWidget(self.observation_date)
        date_row.addStretch(1)
        layout.addLayout(date_row)
        self.history_range = QLabel("Stored curve history: unavailable")
        layout.addWidget(self.history_range)
        self.chart = TreasuryCurveChart()
        layout.addWidget(self.chart, 1)
        self.update_button = QPushButton("Update curve from U.S. Treasury")
        self.update_button.clicked.connect(self._update_curve)
        layout.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.setCentralWidget(central)
        self._load_latest_curve()

    def _load_latest_curve(self) -> None:
        self.observation_date.blockSignals(True)
        self.observation_date.clear()
        self.observation_date.blockSignals(False)
        self.curves_by_date = {}
        self.retrieved_at_by_date = {}
        if not DATABASE_PATH.exists():
            self.chart.set_curve(None)
            self.history_range.setText("Stored curve history: unavailable")
            self.status.setText("No local Treasury curve database exists yet.")
            return
        connection = connect_database(DATABASE_PATH, initialize=False)
        try:
            curve_rows = connection.execute(
                """WITH ranked_observations AS (
                       SELECT o.observation_date, o.tenor_years, o.par_yield_pct,
                              r.retrieved_at_utc,
                              row_number() OVER (
                                  PARTITION BY o.observation_date, o.tenor_years
                                  ORDER BY r.retrieved_at_utc DESC, o.curve_run_id DESC
                              ) AS revision_rank
                       FROM treasury_curve_observations o
                       JOIN treasury_curve_runs r USING (curve_run_id)
                   )
                   SELECT observation_date, tenor_years, par_yield_pct, retrieved_at_utc
                   FROM ranked_observations WHERE revision_rank=1
                   ORDER BY observation_date DESC, tenor_years"""
            ).fetchall()
        except duckdb.Error as error:
            self.chart.set_curve(None)
            self.history_range.setText("Stored curve history: unavailable")
            self.status.setText(f"Could not read the stored Treasury curve: {error}")
            return
        finally:
            connection.close()
        if not curve_rows:
            self.chart.set_curve(None)
            self.history_range.setText("Stored curve history: unavailable")
            self.status.setText("The latest Treasury run contains no curve nodes.")
            return
        for observed_date, tenor, rate, retrieved_at in curve_rows:
            date_key = observed_date.isoformat()
            row = self.curves_by_date.setdefault(date_key, {"date": observed_date})
            row[f"yield_{float(tenor):g}y_pct"] = rate
            self.retrieved_at_by_date[date_key] = str(retrieved_at)
        available_dates = sorted(self.curves_by_date, reverse=True)
        self.history_range.setText(
            f"Stored curve history: {available_dates[-1]} to {available_dates[0]} "
            f"| {len(available_dates):,} observation dates"
        )
        self.observation_date.blockSignals(True)
        for date_key in available_dates:
            self.observation_date.addItem(date_key, date_key)
        self.observation_date.setCurrentIndex(0)
        self.observation_date.blockSignals(False)
        self._show_selected_curve()

    def _show_selected_curve(self, *_args: Any) -> None:
        date_key = self.observation_date.currentData()
        if not date_key or date_key not in self.curves_by_date:
            self.chart.set_curve(None)
            return
        row = self.curves_by_date[date_key]
        self.chart.set_curve(row)
        node_count = sum(row.get(f"yield_{tenor:g}y_pct") is not None for tenor in NODE_FIELDS.values())
        latest_date = next(iter(self.curves_by_date))
        label = "Most recent" if date_key == latest_date else "Selected"
        self.status.setText(
            f"{label} curve: {date_key} | latest stored revision retrieved "
            f"{self.retrieved_at_by_date[date_key]} UTC | {node_count} published tenor nodes"
        )

    def _update_curve(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self._progress_message = "Preparing the Treasury curve update"
        self._progress_completed = 0
        self._progress_total = 1
        self._progress_elapsed = 0.0
        self._progress_received_at = time.monotonic()
        self.status.setText(self._progress_message)
        self.progress_timer.start()
        self.worker = CurveUpdateWorker(self)
        self.worker.progress.connect(self._update_progress)
        self.worker.succeeded.connect(self._update_succeeded)
        self.worker.failed.connect(self._update_failed)
        self.worker.finished.connect(lambda: self.update_button.setEnabled(True))
        self.worker.start()

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, remaining_seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {remaining_seconds:02d}s"
        return f"{remaining_seconds}s"

    def _update_progress(self, completed: int, total: int, message: str, elapsed: float) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(completed)
        self._progress_message = message
        self._progress_completed = completed
        self._progress_total = total
        self._progress_elapsed = elapsed
        self._progress_received_at = time.monotonic()
        self._refresh_progress_status()

    def _refresh_progress_status(self) -> None:
        elapsed = self._progress_elapsed + max(0.0, time.monotonic() - self._progress_received_at)
        annual_batches = max(1, self._progress_total - 3)
        estimate_text = f" | elapsed {self._format_elapsed(elapsed)}"
        if 0 < self._progress_completed < annual_batches:
            average_batch_seconds = elapsed / self._progress_completed
            remaining_batches = annual_batches - self._progress_completed
            eta = self._format_elapsed(average_batch_seconds * remaining_batches)
            estimate_text += f" | about {eta} for remaining downloads"
        self.status.setText(f"{self._progress_message}{estimate_text}")

    def _update_succeeded(self, rows: list[dict[str, object]], manifest: dict[str, object]) -> None:
        self.progress_timer.stop()
        self._load_latest_curve()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.status.setText(
            f"Updated successfully: {manifest['observation_count']} Treasury dates, "
            f"{manifest['first_observation']} to {manifest['last_observation']} | "
            f"Latest curve run {str(manifest['curve_run_id'])[:8]} | "
            f"CSV: data/treasury/nominal_par_curve.csv"
        )

    def _update_failed(self, message: str) -> None:
        self.progress_timer.stop()
        annual_batches = max(1, self._progress_total - 3)
        batches_done = min(self._progress_completed, annual_batches)
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.status.setText(
            f"Treasury curve update failed after {batches_done}/{annual_batches} annual batches: {message}"
        )


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = TreasuryCurveWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
