"""Inspect FINRA TRACE history stored in the local DuckDB database."""

from __future__ import annotations

import json
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

from .trace_history import DATABASE_PATH, TRACE_DIR

PREPROCESSED_DIR = TRACE_DIR / "preprocessed"


def _measure_sql(measure: str) -> tuple[str, str]:
    if measure == "Yield":
        return "clean_last_sale_yield", "Yield (%)"
    return "clean_price", "Price (per 100 par)"


def _latest_preprocessed(run_id: str) -> dict[str, Any]:
    run_dir = PREPROCESSED_DIR / f"run_id={run_id}"
    reports = sorted(run_dir.glob("preprocess_id=*/diagnostics.json"), reverse=True)
    for report_path in reports:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            parquet_path = Path(report["clean_parquet"])
            if report.get("source_run_id") == run_id and parquet_path.is_file():
                schema_connection = duckdb.connect()
                try:
                    columns = {
                        row[0]
                        for row in schema_connection.execute(
                            "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet_path)]
                        ).fetchall()
                    }
                finally:
                    schema_connection.close()
                required = {
                    "isin", "trade_date", "product_type", "sector", "maturity_year",
                    "equity_ticker", "bond_ticker", "clean_price", "clean_last_sale_yield",
                    "price_clean_status", "yield_clean_status",
                }
                if not required.issubset(columns):
                    continue
                report["clean_parquet"] = str(parquet_path.resolve())
                return report
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise FileNotFoundError(
        f"No completed preprocessing output was found for history run {run_id}. "
        "Run ‘Preprocess TRACE history’ first."
    )


def load_bonds(database_path: Path = DATABASE_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not database_path.exists():
        raise FileNotFoundError(f"History database not found: {database_path}")
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        run = connection.execute(
            """SELECT run_id, status, start_date, end_date, total_bonds,
                      created_at_utc, snapshot_path FROM history_runs
               ORDER BY created_at_utc DESC LIMIT 1"""
        ).fetchone()
        if not run:
            raise ValueError("The history database has no download runs yet.")
        run_info = dict(
            zip(
                ["run_id", "status", "start_date", "end_date", "total_bonds", "created_at_utc", "snapshot_path"],
                run,
            )
        )
        preprocessing = _latest_preprocessed(str(run_info["run_id"]))
        run_info["preprocessing"] = preprocessing
        rows = connection.execute(
            """SELECT rb.isin, rb.issuer_name, rb.status,
                      count(o.trade_date) AS rows,
                      min(o.trade_date) AS first_date,
                      max(o.trade_date) AS last_date
               FROM run_bonds rb
               LEFT JOIN read_parquet(?) o ON o.isin = rb.isin
               WHERE rb.run_id = ?
               GROUP BY rb.isin, rb.issuer_name, rb.status
               ORDER BY rb.issuer_name, rb.isin""",
            [preprocessing["clean_parquet"], run_info["run_id"]],
        ).fetchall()
        return run_info, [
            dict(zip(["isin", "issuer_name", "status", "rows", "first_date", "last_date"], row))
            for row in rows
        ]
    finally:
        connection.close()


def _dimension_sql(dimension: str) -> str:
    expressions = {
        "Market": "coalesce(nullif(trim(o.product_type), ''), 'Unknown')",
        "Sector": "coalesce(nullif(trim(o.sector), ''), 'Unmapped')",
        "Maturity year": "coalesce(nullif(trim(o.maturity_year), ''), 'Unmapped')",
        "Issuer ticker": "coalesce(nullif(trim(o.equity_ticker), ''), 'Unmapped')",
        "Bond ticker": "coalesce(nullif(trim(o.bond_ticker), ''), 'Unmapped')",
    }
    try:
        return expressions[dimension]
    except KeyError as error:
        raise ValueError(f"Unsupported partition dimension: {dimension}") from error


def load_group_values(
    dimension: str,
    run_info: dict[str, Any],
    database_path: Path = DATABASE_PATH,
) -> list[str]:
    connection = duckdb.connect()
    try:
        parquet_path = run_info["preprocessing"]["clean_parquet"]
        expression = _dimension_sql(dimension).replace("o.", "p.")
        values = connection.execute(
            f"""SELECT DISTINCT {expression}
                FROM read_parquet(?) p ORDER BY 1""",
            [parquet_path],
        ).fetchall()
        return [str(row[0]) for row in values]
    finally:
        connection.close()


def load_group_data(
    dimension: str,
    group_value: str,
    measure: str,
    interval: str,
    run_info: dict[str, Any],
    database_path: Path = DATABASE_PATH,
) -> list[tuple[Any, ...]]:
    dimension_expression = _dimension_sql(dimension)
    value_sql, _ = _measure_sql(measure)
    interval_unit = {"Daily": "day", "Monthly": "month", "Quarterly": "quarter", "Yearly": "year"}[interval]
    connection = duckdb.connect()
    try:
        return connection.execute(
            f"""WITH raw AS (
                    SELECT o.isin, o.trade_date, {dimension_expression} AS group_value,
                           o.{value_sql} AS value
                    FROM read_parquet(?) o
                ), per_bond AS (
                    SELECT group_value, isin,
                           date_trunc('{interval_unit}', trade_date)::DATE AS period,
                           avg(value) AS bond_value
                    FROM raw
                    WHERE value IS NOT NULL
                    GROUP BY group_value, isin, period
                ), cross_section AS (
                    SELECT group_value, period, count(*) AS bonds,
                        avg(bond_value) AS mean, median(bond_value) AS median,
                        quantile_cont(bond_value, 0.25) AS p25,
                        quantile_cont(bond_value, 0.75) AS p75,
                        min(bond_value) AS minimum, max(bond_value) AS maximum
                    FROM per_bond GROUP BY group_value, period
                ), rolling AS (
                    SELECT *,
                        stddev_samp(mean) OVER (
                            PARTITION BY group_value ORDER BY period
                            ROWS BETWEEN 62 PRECEDING AND CURRENT ROW
                        ) AS vol_63,
                        stddev_samp(mean) OVER (
                            PARTITION BY group_value ORDER BY period
                            ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
                        ) AS vol_252,
                        mean - lag(mean, 63) OVER (
                            PARTITION BY group_value ORDER BY period
                        ) AS mom_63,
                        mean - lag(mean, 252) OVER (
                            PARTITION BY group_value ORDER BY period
                        ) AS mom_252
                    FROM cross_section
                )
                SELECT period, bonds, mean, median, p25, p75, minimum, maximum,
                       vol_63, vol_252, mom_63, mom_252
                FROM rolling WHERE group_value=? ORDER BY period""",
            [run_info["preprocessing"]["clean_parquet"], group_value],
        ).fetchall()
    finally:
        connection.close()


def load_bond_data(
    isin: str,
    measure: str,
    partition: str,
    run_info: dict[str, Any],
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    value_sql, value_label = _measure_sql(measure)
    partition_sql = {
        "Daily": "day", "Monthly": "month", "Quarterly": "quarter", "Yearly": "year"
    }[partition]
    preprocessing = run_info["preprocessing"]
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        meta = connection.execute(
            """SELECT issuer_name, status FROM run_bonds
               WHERE isin = ? AND run_id = ?""",
            [isin, run_info["run_id"]],
        ).fetchone()
        if not meta:
            raise ValueError(f"No run metadata found for {isin}.")

        summary = connection.execute(
            """SELECT count(*), min(trade_date), max(trade_date),
                      count(*) FILTER (WHERE price_clean_status='zero_is_missing'),
                      count(*) FILTER (WHERE yield_clean_status='zero_is_missing'),
                      count(*) FILTER (WHERE price_clean_status='sentinel'
                                       OR price_clean_status='outside_absolute_limit'),
                      count(*) FILTER (WHERE yield_clean_status='sentinel'
                                       OR yield_clean_status='outside_absolute_limit'),
                      count(*) FILTER (WHERE price_clean_status='missing'),
                      count(*) FILTER (WHERE yield_clean_status='missing')
               FROM read_parquet(?) WHERE isin=?""",
            [preprocessing["clean_parquet"], isin],
        ).fetchone()
        daily = connection.execute(
            f"""SELECT trade_date, {value_sql} AS value
                FROM read_parquet(?) WHERE isin = ? ORDER BY trade_date""",
            [preprocessing["clean_parquet"], isin],
        ).fetchall()
        partitions = connection.execute(
            f"""WITH clean AS (
                    SELECT trade_date, {value_sql} AS value
                    FROM read_parquet(?) WHERE isin = ?
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
            [preprocessing["clean_parquet"], isin],
        ).fetchall()
        return {
            "isin": isin,
            "issuer": meta[0] or "",
            "status": meta[1] or "",
            "raw_count": int(summary[0] or 0),
            "first_date": summary[1],
            "last_date": summary[2],
            "zero_price_count": int(summary[3] or 0),
            "zero_yield_count": int(summary[4] or 0),
            "sentinel_price_count": int(summary[5] or 0),
            "sentinel_yield_count": int(summary[6] or 0),
            "null_price_count": int(summary[7] or 0),
            "null_yield_count": int(summary[8] or 0),
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
        painter.fillRect(self.rect(), QColor("#151a23"))
        left, right, top, bottom = 68, self.width() - 24, 20, self.height() - 48
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        if self.mode == "Daily observations":
            values = [point[1] for point in self.daily]
        else:
            values = [float(row[7]) for row in self.partitions if row[7] is not None]
            values += [float(row[6]) for row in self.partitions if row[6] is not None]
        if not values:
            painter.setPen(QColor("#aab6c6"))
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
        painter.setPen(QColor("#aab6c6"))
        for step in range(5):
            value = high - (high - low) * step / 4
            y = top + plot_height * step / 4
            painter.setPen(QPen(QColor("#303947"), 1))
            painter.drawLine(left, int(y), right, int(y))
            painter.setPen(QColor("#aab6c6"))
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
            painter.setPen(QPen(QColor("#53b7d8"), 1.9))
            painter.drawPath(path)
            first_label = self.daily[0][0].isoformat()
            last_label = self.daily[-1][0].isoformat()
        else:
            count = len(self.partitions)
            first_day = self.partitions[0][0].toordinal()
            day_span = max(1, self.partitions[-1][0].toordinal() - first_day)
            if count == 1:
                xs = [left + plot_width / 2]
            else:
                xs = [
                    left + (row[0].toordinal() - first_day) * plot_width / day_span
                    for row in self.partitions
                ]
            means = QPainterPath()
            for x, row in zip(xs, self.partitions):
                if row[6] is None or row[7] is None:
                    continue
                painter.setPen(QPen(QColor("#8d9caf"), 1.1))
                painter.drawLine(int(x), int(y_pos(float(row[7]))), int(x), int(y_pos(float(row[6]))))
                box_top, box_bottom = y_pos(float(row[5])), y_pos(float(row[4]))
                painter.fillRect(int(x - 5), int(box_top), 10, max(2, int(box_bottom - box_top)), QColor("#244359"))
                painter.setPen(QPen(QColor("#53a9c7"), 1))
                painter.drawRect(int(x - 5), int(box_top), 10, max(2, int(box_bottom - box_top)))
                if row[2] is not None:
                    y = y_pos(float(row[2]))
                    if means.elementCount() == 0:
                        means.moveTo(x, y)
                    else:
                        means.lineTo(x, y)
            painter.setPen(QPen(QColor("#f0a35e"), 1.9))
            painter.drawPath(means)
            first_label = str(self.partitions[0][0])
            last_label = str(self.partitions[-1][0])
        painter.setPen(QColor("#aab6c6"))
        painter.drawText(left, self.height() - 12, first_label)
        painter.drawText(right - 92, self.height() - 12, last_label)
        painter.setPen(QColor("#aab6c6"))
        legend = "Mean (orange), P25–P75 (blue box), min–max (line)" if self.mode != "Daily observations" else "One point per valid reported trade date; no fill-forward"
        painter.drawText(left + 12, top + 15, legend)


class TraceHistoryInspector(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TRACE Bond History Inspector")
        self.resize(1400, 900)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #10141c; color: #e3e9f1; }
            QLabel { color: #d6deea; }
            QComboBox {
                background: #1a2230; color: #e3e9f1;
                border: 1px solid #354255; border-radius: 4px; padding: 5px 8px;
                selection-background-color: #315b72;
            }
            QComboBox:hover { border-color: #53a9c7; }
            QComboBox QAbstractItemView {
                background: #1a2230; color: #e3e9f1;
                selection-background-color: #315b72; border: 1px solid #354255;
            }
            QTabWidget::pane { border: 1px solid #303947; background: #151a23; }
            QTabBar::tab {
                background: #171d27; color: #aab6c6; padding: 7px 14px;
                border: 1px solid #303947; border-bottom: none;
            }
            QTabBar::tab:selected { background: #151a23; color: #e3e9f1; }
            QTableWidget {
                background: #151a23; alternate-background-color: #191f2a;
                color: #dce3ed; gridline-color: #303947;
                selection-background-color: #29475b; selection-color: #ffffff;
                border: 1px solid #303947;
            }
            QHeaderView::section {
                background: #1d2531; color: #c6d0df; padding: 6px;
                border: 1px solid #303947;
            }
            QStatusBar { background: #10141c; color: #aab6c6; }
            """
        )
        self.run_info: dict[str, Any] = {}
        self.bonds: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}

        central = QWidget()
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("View"))
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Individual bond", "Group partition"])
        self.view_mode.currentTextChanged.connect(self._view_changed)
        controls.addWidget(self.view_mode)
        self.bond_label = QLabel("Bond")
        controls.addWidget(self.bond_label)
        self.bond = QComboBox()
        self.bond.setEditable(True)
        self.bond.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.bond.currentIndexChanged.connect(self._selection_changed)
        controls.addWidget(self.bond, 2)
        self.group_by_label = QLabel("Group by")
        controls.addWidget(self.group_by_label)
        self.group_by = QComboBox()
        self.group_by.addItems(["Market", "Sector", "Maturity year", "Issuer ticker", "Bond ticker"])
        self.group_by.setToolTip(
            "Market groups use FINRA's TRACE product type; issuer and bond tickers come from the run snapshot."
        )
        self.group_by.currentTextChanged.connect(self._group_dimension_changed)
        controls.addWidget(self.group_by)
        self.group_value_label = QLabel("Group")
        controls.addWidget(self.group_value_label)
        self.group_value = QComboBox()
        self.group_value.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.group_value, 1)
        controls.addWidget(QLabel("Measure"))
        self.measure = QComboBox()
        self.measure.addItems(["Yield", "Price"])
        self.measure.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.measure)
        controls.addWidget(QLabel("Time interval"))
        self.partition = QComboBox()
        self.partition.addItems(["Daily", "Monthly", "Quarterly", "Yearly"])
        self.partition.setCurrentText("Monthly")
        self.partition.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.partition)
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
             "Vol 63", "Vol 252", "Mom 63", "Mom 252"]
        )
        self.stats.horizontalHeaderItem(8).setToolTip(
            "Trailing sample standard deviation; the selected view and interval define the window."
        )
        self.stats.horizontalHeaderItem(9).setToolTip(
            "Trailing sample standard deviation; the selected view and interval define the window."
        )
        self.stats.horizontalHeaderItem(10).setToolTip(
            "Current value minus the value 63 selected-view intervals earlier."
        )
        self.stats.horizontalHeaderItem(11).setToolTip(
            "Current value minus the value 252 selected-view intervals earlier."
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
            f"preprocessed {self.run_info['preprocessing']['preprocess_id']} "
            f"({self.run_info['preprocessing']['policy_version']})"
        )
        self.statusBar().showMessage(
            f"Clean data: {self.run_info['preprocessing']['clean_parquet']} | DuckDB metadata: {DATABASE_PATH}"
        )
        if self.bonds:
            self._load_group_values()
            self.refresh()
        self._view_changed(self.view_mode.currentText())

    def _selection_changed(self, _index: int) -> None:
        if self.bond.currentData():
            self.refresh()

    def _view_changed(self, mode: str) -> None:
        individual = mode == "Individual bond"
        for widget in (self.bond_label, self.bond):
            widget.setVisible(individual)
        for widget in (self.group_by_label, self.group_by, self.group_value_label, self.group_value):
            widget.setVisible(not individual)
        self.refresh()

    def _group_dimension_changed(self, _dimension: str) -> None:
        self._load_group_values()
        self.refresh()

    def _load_group_values(self) -> None:
        if not self.run_info:
            return
        try:
            values = load_group_values(self.group_by.currentText(), self.run_info)
        except Exception as error:
            self.statusBar().showMessage(f"Could not load partition values: {error}")
            return
        current = self.group_value.currentText()
        self.group_value.blockSignals(True)
        self.group_value.clear()
        self.group_value.addItems(values)
        index = self.group_value.findText(current)
        if index >= 0:
            self.group_value.setCurrentIndex(index)
        self.group_value.blockSignals(False)

    def refresh(self, *_args: Any) -> None:
        if not self.run_info:
            return
        if self.view_mode.currentText() == "Group partition":
            group_value = self.group_value.currentText().strip()
            if not group_value:
                return
            try:
                rows = load_group_data(
                    self.group_by.currentText(), group_value,
                    self.measure.currentText(), self.partition.currentText(), self.run_info,
                )
            except Exception as error:
                self.statusBar().showMessage(f"Could not load partition history: {error}")
                return
            if not rows:
                self.chart.daily = []
                self.chart.partitions = []
                self.chart.mode = "Partition ranges"
                self.chart.update()
                self.stats.setRowCount(0)
                self.summary.setText(
                    f"{self.group_by.currentText()} = {group_value} | no valid observations in run window"
                )
                return
            self.chart.set_data(
                {"daily": [], "partitions": rows,
                 "value_label": _measure_sql(self.measure.currentText())[1]},
                "Partition ranges",
            )
            self._fill_stats(rows, group_mode=True)
            first_date, last_date = rows[0][0], rows[-1][0]
            self.summary.setText(
                f"{self.group_by.currentText()} = {group_value} | {self.measure.currentText()} | "
                f"{self.partition.currentText().lower()} cross-section | {first_date} to {last_date} | "
                f"each bond has equal weight within a time bucket"
            )
            self.statusBar().showMessage(
                "Range shows cross-sectional bond dispersion; rolling measures use the group mean time series."
            )
            return

        isin = self.bond.currentData()
        if not isin:
            return
        try:
            self.data = load_bond_data(
                str(isin), self.measure.currentText(), self.partition.currentText(), self.run_info
            )
        except Exception as error:
            self.statusBar().showMessage(f"Could not load bond history: {error}")
            return
        self.chart.set_data(self.data, "Daily observations")
        self._fill_stats(self.data["partitions"], group_mode=False)
        if self.measure.currentText() == "Yield":
            excl_label = (
                f"zero yields {self.data['zero_yield_count']:,}; "
                f"sentinel yields {self.data['sentinel_yield_count']:,}; "
                f"null yields {self.data['null_yield_count']:,}"
            )
        else:
            excl_label = (
                f"zero prices {self.data['zero_price_count']:,}; "
                f"sentinel prices {self.data['sentinel_price_count']:,}; "
                f"null prices {self.data['null_price_count']:,}"
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

    def _fill_stats(self, rows: list[tuple[Any, ...]], group_mode: bool) -> None:
        self.stats.setRowCount(len(rows))
        self.tabs.setTabText(1, "Group statistics" if group_mode else "Bond time-bucket statistics")
        self.stats.setHorizontalHeaderLabels(
            ["Period", "Bonds" if group_mode else "Observations", "Mean", "Median", "P25", "P75", "Min", "Max",
             "Vol 63", "Vol 252", "Mom 63", "Mom 252"]
        )
        unit = self.partition.currentText().lower()
        scope = "group mean" if group_mode else "bond series"
        self.stats.horizontalHeaderItem(8).setToolTip(
            f"Sample standard deviation of the {scope} over up to 63 prior {unit} points; not annualized."
        )
        self.stats.horizontalHeaderItem(9).setToolTip(
            f"Sample standard deviation of the {scope} over up to 252 prior {unit} points; not annualized."
        )
        self.stats.horizontalHeaderItem(10).setToolTip(
            f"Current {scope} value minus its value 63 {unit} points earlier."
        )
        self.stats.horizontalHeaderItem(11).setToolTip(
            f"Current {scope} value minus its value 252 {unit} points earlier."
        )
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
