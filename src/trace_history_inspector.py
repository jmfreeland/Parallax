"""Inspect FINRA TRACE history stored in the local DuckDB database."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .trace_history import DATABASE_PATH


def _measure_sql(measure: str) -> tuple[str, str]:
    if measure == "Yield":
        return (
            "CASE WHEN last_sale_yield IS NULL OR last_sale_yield = 0 "
            "THEN NULL ELSE last_sale_yield END",
            "Yield (%)",
        )
    return (
        "CASE WHEN price IS NULL OR price <= 0 OR price >= 10000 "
        "THEN NULL ELSE price END",
        "Price (per 100 par)",
    )


def load_bonds(database_path: Path = DATABASE_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not database_path.exists():
        raise FileNotFoundError(f"History database not found: {database_path}")
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        run = connection.execute(
            """SELECT run_id, status, start_date, end_date, total_bonds,
                      created_at_utc FROM history_runs
               ORDER BY created_at_utc DESC LIMIT 1"""
        ).fetchone()
        if not run:
            raise ValueError("The history database has no download runs yet.")
        run_info = dict(
            zip(
                ["run_id", "status", "start_date", "end_date", "total_bonds", "created_at_utc"],
                run,
            )
        )
        rows = connection.execute(
            """SELECT rb.isin, rb.issuer_name, rb.status,
                      count(o.trade_date) AS rows,
                      min(o.trade_date) AS first_date,
                      max(o.trade_date) AS last_date
               FROM run_bonds rb
               LEFT JOIN observations o ON o.isin = rb.isin
               WHERE rb.run_id = ?
               GROUP BY rb.isin, rb.issuer_name, rb.status
               ORDER BY rb.issuer_name, rb.isin""",
            [run_info["run_id"]],
        ).fetchall()
        return run_info, [
            dict(zip(["isin", "issuer_name", "status", "rows", "first_date", "last_date"], row))
            for row in rows
        ]
    finally:
        connection.close()


def load_bond_data(
    isin: str,
    measure: str,
    partition: str,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    value_sql, value_label = _measure_sql(measure)
    partition_sql = {"Month": "month", "Quarter": "quarter", "Year": "year"}[partition]
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        meta = connection.execute(
            """SELECT rb.issuer_name, rb.status, count(o.trade_date),
                      min(o.trade_date), max(o.trade_date),
                      count(*) FILTER (WHERE o.price = 0),
                      count(*) FILTER (WHERE o.last_sale_yield = 0),
                      count(*) FILTER (WHERE o.price >= 10000)
               FROM run_bonds rb
               LEFT JOIN observations o ON o.isin = rb.isin
               WHERE rb.isin = ? AND rb.run_id = (
                   SELECT run_id FROM history_runs ORDER BY created_at_utc DESC LIMIT 1
               )
               GROUP BY rb.issuer_name, rb.status
               ORDER BY max(o.trade_date) DESC LIMIT 1""",
            [isin],
        ).fetchone()
        if not meta:
            raise ValueError(f"No run metadata found for {isin}.")

        daily = connection.execute(
            f"""SELECT trade_date, {value_sql} AS value
                FROM observations WHERE isin = ? ORDER BY trade_date""",
            [isin],
        ).fetchall()
        partitions = connection.execute(
            f"""WITH clean AS (
                    SELECT trade_date, {value_sql} AS value
                    FROM observations WHERE isin = ?
                ), valid AS (
                    SELECT trade_date, value FROM clean WHERE value IS NOT NULL
                ), rolling AS (
                    SELECT trade_date, value,
                        stddev_samp(value) OVER (
                            ORDER BY trade_date ROWS BETWEEN 62 PRECEDING AND CURRENT ROW
                        ) AS vol_63,
                        stddev_samp(value) OVER (
                            ORDER BY trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
                        ) AS vol_252,
                        value - lag(value, 63) OVER (ORDER BY trade_date) AS mom_63,
                        value - lag(value, 252) OVER (ORDER BY trade_date) AS mom_252,
                        date_trunc('{partition_sql}', trade_date)::DATE AS bucket
                    FROM valid
                ), summary AS (
                    SELECT bucket, count(*) AS observations, avg(value) AS mean,
                        median(value) AS median,
                        quantile_cont(value, 0.25) AS p25,
                        quantile_cont(value, 0.75) AS p75,
                        min(value) AS minimum, max(value) AS maximum
                    FROM rolling GROUP BY bucket
                ), latest AS (
                    SELECT bucket, vol_63, vol_252, mom_63, mom_252,
                        row_number() OVER (PARTITION BY bucket ORDER BY trade_date DESC) AS row_num
                    FROM rolling
                )
                SELECT s.bucket, s.observations, s.mean, s.median, s.p25, s.p75,
                    s.minimum, s.maximum, l.vol_63, l.vol_252, l.mom_63, l.mom_252
                FROM summary s JOIN latest l USING (bucket)
                WHERE l.row_num = 1 ORDER BY s.bucket""",
            [isin],
        ).fetchall()
        return {
            "isin": isin,
            "issuer": meta[0] or "",
            "status": meta[1] or "",
            "raw_count": int(meta[2] or 0),
            "first_date": meta[3],
            "last_date": meta[4],
            "zero_price_count": int(meta[5] or 0),
            "zero_yield_count": int(meta[6] or 0),
            "sentinel_price_count": int(meta[7] or 0),
            "value_label": value_label,
            "daily": [(row[0], row[1]) for row in daily if row[1] is not None],
            "partitions": partitions,
        }
    finally:
        connection.close()


class HistoryChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(330)
        self.mode = "Daily observations"
        self.value_label = "Yield (%)"
        self.daily: list[tuple[date, float]] = []
        self.partitions: list[tuple[Any, ...]] = []

    def set_data(self, data: dict[str, Any], mode: str) -> None:
        self.daily = data["daily"]
        self.partitions = data["partitions"]
        self.value_label = data["value_label"]
        self.mode = mode
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("white"))
        left, right, top, bottom = 68, self.width() - 24, 20, self.height() - 48
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        if self.mode == "Daily observations":
            values = [point[1] for point in self.daily]
        else:
            values = [float(row[7]) for row in self.partitions if row[7] is not None]
            values += [float(row[6]) for row in self.partitions if row[6] is not None]
        if not values:
            painter.setPen(QColor("#667085"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No valid observations for this measure.")
            return
        low, high = min(values), max(values)
        if high == low:
            padding = max(abs(high) * 0.05, 0.1)
            low, high = low - padding, high + padding
        else:
            padding = (high - low) * 0.08
            low, high = low - padding, high + padding

        def y_pos(value: float) -> float:
            return bottom - (value - low) / (high - low) * plot_height

        painter.setFont(self.font())
        painter.setPen(QColor("#667085"))
        for step in range(5):
            value = high - (high - low) * step / 4
            y = top + plot_height * step / 4
            painter.setPen(QPen(QColor("#e7e9ed"), 1))
            painter.drawLine(left, int(y), right, int(y))
            painter.setPen(QColor("#667085"))
            painter.drawText(4, int(y + 5), f"{value:.2f}")
        painter.save()
        painter.translate(16, (top + bottom) / 2)
        painter.rotate(-90)
        painter.drawText(int(-plot_height / 2), 0, self.value_label)
        painter.restore()

        if self.mode == "Daily observations":
            first_day = self.daily[0][0].toordinal()
            day_span = max(1, self.daily[-1][0].toordinal() - first_day)
            if len(self.daily) == 1:
                xs = [left + plot_width / 2]
            else:
                xs = [
                    left + (observed_day.toordinal() - first_day) * plot_width / day_span
                    for observed_day, _ in self.daily
                ]
            path = QPainterPath()
            for index, ((_, value), x) in enumerate(zip(self.daily, xs)):
                y = y_pos(value)
                if index == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.setPen(QPen(QColor("#3976a8"), 1.7))
            painter.drawPath(path)
            first_label = self.daily[0][0].isoformat()
            last_label = self.daily[-1][0].isoformat()
        else:
            count = len(self.partitions)
            if count == 1:
                xs = [left + plot_width / 2]
            else:
                xs = [left + i * plot_width / (count - 1) for i in range(count)]
            means = QPainterPath()
            for x, row in zip(xs, self.partitions):
                if row[6] is None or row[7] is None:
                    continue
                painter.setPen(QPen(QColor("#98a2b3"), 1.1))
                painter.drawLine(int(x), int(y_pos(float(row[7]))), int(x), int(y_pos(float(row[6]))))
                box_top, box_bottom = y_pos(float(row[5])), y_pos(float(row[4]))
                painter.fillRect(int(x - 5), int(box_top), 10, max(2, int(box_bottom - box_top)), QColor("#d6e6f2"))
                painter.setPen(QPen(QColor("#7193ab"), 1))
                painter.drawRect(int(x - 5), int(box_top), 10, max(2, int(box_bottom - box_top)))
                if row[2] is not None:
                    y = y_pos(float(row[2]))
                    if means.elementCount() == 0:
                        means.moveTo(x, y)
                    else:
                        means.lineTo(x, y)
            painter.setPen(QPen(QColor("#cf6c36"), 1.8))
            painter.drawPath(means)
            first_label = str(self.partitions[0][0])
            last_label = str(self.partitions[-1][0])
        painter.setPen(QColor("#667085"))
        painter.drawText(left, self.height() - 12, first_label)
        painter.drawText(right - 92, self.height() - 12, last_label)
        painter.setPen(QColor("#667085"))
        legend = "Mean (orange), P25–P75 (blue box), min–max (line)" if self.mode != "Daily observations" else "One point per valid reported trade date; no fill-forward"
        painter.drawText(left + 12, top + 15, legend)


class TraceHistoryInspector(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TRACE Bond History Inspector")
        self.resize(1400, 900)
        self.run_info: dict[str, Any] = {}
        self.bonds: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}

        central = QWidget()
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Bond"))
        self.bond = QComboBox()
        self.bond.setEditable(True)
        self.bond.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.bond.currentIndexChanged.connect(self._selection_changed)
        controls.addWidget(self.bond, 2)
        controls.addWidget(QLabel("Measure"))
        self.measure = QComboBox()
        self.measure.addItems(["Yield", "Price"])
        self.measure.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.measure)
        controls.addWidget(QLabel("Partitions"))
        self.partition = QComboBox()
        self.partition.addItems(["Month", "Quarter", "Year"])
        self.partition.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.partition)
        controls.addWidget(QLabel("Chart"))
        self.chart_mode = QComboBox()
        self.chart_mode.addItems(["Daily observations", "Partition ranges"])
        self.chart_mode.currentTextChanged.connect(self._update_chart_mode)
        controls.addWidget(self.chart_mode)
        layout.addLayout(controls)

        self.summary = QLabel("Loading history database…")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        self.chart = HistoryChart()
        self.tabs.addTab(self.chart, "Chart")
        self.stats = QTableWidget(0, 12)
        self.stats.setHorizontalHeaderLabels(
            ["Period", "Observations", "Mean", "Median", "P25", "P75", "Min", "Max",
             "Vol 63 obs", "Vol 252 obs", "Mom 63 obs", "Mom 252 obs"]
        )
        self.stats.horizontalHeaderItem(8).setToolTip(
            "Sample standard deviation over the latest 63 valid observed-trade rows; not annualized."
        )
        self.stats.horizontalHeaderItem(9).setToolTip(
            "Sample standard deviation over the latest 252 valid observed-trade rows; not annualized."
        )
        self.stats.horizontalHeaderItem(10).setToolTip(
            "Current value minus the value 63 valid observed-trade rows earlier."
        )
        self.stats.horizontalHeaderItem(11).setToolTip(
            "Current value minus the value 252 valid observed-trade rows earlier."
        )
        self.stats.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats.setAlternatingRowColors(True)
        self.tabs.addTab(self.stats, "Partition statistics")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"DuckDB: {DATABASE_PATH}")
        self._load_bonds()

    def _load_bonds(self) -> None:
        try:
            self.run_info, self.bonds = load_bonds()
        except Exception as error:
            self.summary.setText(str(error))
            return
        self.bond.blockSignals(True)
        self.bond.clear()
        for row in self.bonds:
            label = f"{row['isin']} | {row['issuer_name']} | {row['rows']:,} rows"
            self.bond.addItem(label, row["isin"])
        self.bond.blockSignals(False)
        self.summary.setText(
            f"Run {self.run_info['run_id']} | status {self.run_info['status']} | "
            f"window {self.run_info['start_date']} to {self.run_info['end_date']} | "
            f"snapshot population {self.run_info['total_bonds']:,} | "
            f"database {DATABASE_PATH}"
        )
        if self.bonds:
            self.refresh()

    def _selection_changed(self, _index: int) -> None:
        if self.bond.currentData():
            self.refresh()

    def _update_chart_mode(self, mode: str) -> None:
        if self.data:
            self.chart.set_data(self.data, mode)

    def refresh(self, *_args: Any) -> None:
        isin = self.bond.currentData()
        if not isin:
            return
        try:
            self.data = load_bond_data(str(isin), self.measure.currentText(), self.partition.currentText())
        except Exception as error:
            self.statusBar().showMessage(f"Could not load bond history: {error}")
            return
        self.chart.set_data(self.data, self.chart_mode.currentText())
        self._fill_stats(self.data["partitions"])
        if self.measure.currentText() == "Yield":
            excl_label = f"zero yields excluded {self.data['zero_yield_count']:,}"
        else:
            excl_label = (
                f"zero prices {self.data['zero_price_count']:,}; "
                f"sentinel prices ≥10,000 {self.data['sentinel_price_count']:,}"
            )
        valid = len(self.data["daily"])
        self.summary.setText(
            f"{self.data['issuer']} | {isin} | {self.measure.currentText()} | "
            f"{self.data['status']} | {valid:,} valid observations / {self.data['raw_count']:,} raw rows | "
            f"{excl_label} | first {self.data['first_date']} | last {self.data['last_date']}"
        )
        self.statusBar().showMessage(
            f"Loaded {valid:,} valid observations. Rolling volatility and momentum use prior observed trades."
        )

    def _fill_stats(self, rows: list[tuple[Any, ...]]) -> None:
        self.stats.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            display = [row[0], row[1], *row[2:]]
            for column, value in enumerate(display):
                if value is None:
                    text = ""
                elif column == 0:
                    text = str(value)
                elif column == 1:
                    text = f"{int(value):,}"
                else:
                    text = f"{float(value):.3f}"
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.stats.setItem(row_index, column, item)
        self.stats.resizeColumnsToContents()


def main() -> None:
    app = QApplication(sys.argv)
    window = TraceHistoryInspector()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
