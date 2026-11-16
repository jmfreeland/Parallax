"""Visual Treasury curve updater and latest-vintage viewer."""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
from PySide6.QtCore import QPointF, QRectF, QThread, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from .trace_history import DATABASE_PATH, connect_database
from .treasury_curves import NODE_FIELDS, update_treasury_curve


class CurveUpdateWorker(QThread):
    succeeded = Signal(object, object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            rows, manifest = update_treasury_curve(database_path=DATABASE_PATH)
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
        central = QWidget()
        layout = QVBoxLayout(central)
        self.status = QLabel("Loading the latest stored Treasury curve...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.chart = TreasuryCurveChart()
        layout.addWidget(self.chart, 1)
        self.update_button = QPushButton("Update curve from U.S. Treasury")
        self.update_button.clicked.connect(self._update_curve)
        layout.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.setCentralWidget(central)
        self._load_latest_curve()

    def _load_latest_curve(self) -> None:
        if not DATABASE_PATH.exists():
            self.chart.set_curve(None)
            self.status.setText("No local Treasury curve database exists yet.")
            return
        connection = connect_database(DATABASE_PATH, initialize=False)
        try:
            latest = connection.execute(
                """SELECT curve_run_id, retrieved_at_utc FROM treasury_curve_runs
                   ORDER BY retrieved_at_utc DESC LIMIT 1"""
            ).fetchone()
            if not latest:
                self.chart.set_curve(None)
                self.status.setText("No Treasury curve has been stored yet.")
                return
            run_id, retrieved_at = latest
            curve_row = connection.execute(
                """SELECT observation_date, tenor_years, par_yield_pct
                   FROM treasury_curve_observations WHERE curve_run_id=?
                   ORDER BY observation_date DESC, tenor_years""",
                [run_id],
            ).fetchall()
        except duckdb.Error as error:
            self.chart.set_curve(None)
            self.status.setText(f"Could not read the stored Treasury curve: {error}")
            return
        finally:
            connection.close()
        if not curve_row:
            self.chart.set_curve(None)
            self.status.setText("The latest Treasury run contains no curve nodes.")
            return
        latest_date = curve_row[0][0]
        values = {float(tenor): rate for observed, tenor, rate in curve_row if observed == latest_date}
        row: dict[str, object] = {"date": latest_date}
        for tenor in NODE_FIELDS.values():
            row[f"yield_{tenor:g}y_pct"] = values.get(tenor)
        self.chart.set_curve(row)
        self.status.setText(
            f"Latest stored vintage: {latest_date} | retrieved {retrieved_at} UTC | "
            f"{len(values)} published tenor nodes | run {str(run_id)[:8]}"
        )

    def _update_curve(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.status.setText("Downloading Treasury observations and saving a new curve vintage...")
        self.worker = CurveUpdateWorker(self)
        self.worker.succeeded.connect(self._update_succeeded)
        self.worker.failed.connect(self._update_failed)
        self.worker.finished.connect(lambda: self.update_button.setEnabled(True))
        self.worker.start()

    def _update_succeeded(self, rows: list[dict[str, object]], manifest: dict[str, object]) -> None:
        self._load_latest_curve()
        self.status.setText(
            f"Updated successfully: {manifest['observation_count']} Treasury dates, "
            f"{manifest['first_observation']} to {manifest['last_observation']} | "
            f"Latest curve run {str(manifest['curve_run_id'])[:8]} | "
            f"CSV: data/treasury/nominal_par_curve.csv"
        )

    def _update_failed(self, message: str) -> None:
        self.status.setText(f"Treasury curve update failed: {message}")


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = TreasuryCurveWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
