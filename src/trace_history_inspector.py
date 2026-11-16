"""Inspect FINRA TRACE history stored in the local DuckDB database."""

from __future__ import annotations

import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .trace_history import DATABASE_PATH, TRACE_DIR

PREPROCESSED_DIR = TRACE_DIR / "preprocessed"


def _measure_sql(measure: str) -> tuple[str, str]:
    if measure == "Yield":
        return "clean_last_sale_yield", "Yield (%)"
    if measure == "G-spread":
        return "g_spread_bps", "G-spread (bp)"
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
    minimum_coverage_pct: float = 50.0,
    database_path: Path = DATABASE_PATH,
) -> tuple[list[tuple[Any, ...]], int, int]:
    dimension_expression = _dimension_sql(dimension)
    value_sql, _ = _measure_sql(measure)
    interval_unit = {"Daily": "day", "Monthly": "month", "Quarterly": "quarter", "Yearly": "year"}[interval]
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            f"""WITH raw AS (
                    SELECT o.isin, o.issuer_name, o.trade_date,
                           {dimension_expression} AS group_value,
                           o.{value_sql} AS value
                    FROM read_parquet(?) o
                ), first_seen AS (
                    SELECT group_value, isin, min(trade_date) AS first_date
                    FROM raw GROUP BY group_value, isin
                ), per_bond AS (
                    SELECT group_value, isin, any_value(issuer_name) AS description,
                           date_trunc('{interval_unit}', trade_date)::DATE AS period,
                           avg(value) AS bond_value
                    FROM raw
                    WHERE value IS NOT NULL
                    GROUP BY group_value, isin, period
                ), ranked_bonds AS (
                    SELECT *,
                           row_number() OVER (
                               PARTITION BY group_value, period ORDER BY bond_value, isin
                           ) AS min_rank,
                           row_number() OVER (
                               PARTITION BY group_value, period ORDER BY bond_value DESC, isin
                           ) AS max_rank
                    FROM per_bond
                ), extrema AS (
                    SELECT group_value, period,
                           max(isin) FILTER (WHERE min_rank=1) AS min_isin,
                           max(description) FILTER (WHERE min_rank=1) AS min_description,
                           max(isin) FILTER (WHERE max_rank=1) AS max_isin,
                           max(description) FILTER (WHERE max_rank=1) AS max_description
                    FROM ranked_bonds GROUP BY group_value, period
                ), cross_section AS (
                    SELECT group_value, period, count(*) AS bonds,
                        avg(bond_value) AS mean, median(bond_value) AS median,
                        quantile_cont(bond_value, 0.25) AS p25,
                        quantile_cont(bond_value, 0.75) AS p75,
                        min(bond_value) AS minimum, max(bond_value) AS maximum,
                        stddev_samp(bond_value) AS cross_section_std
                    FROM per_bond GROUP BY group_value, period
                ), cross_section_extrema AS (
                    SELECT c.*, e.min_isin, e.min_description, e.max_isin, e.max_description
                    FROM cross_section c
                    JOIN extrema e USING (group_value, period)
                ), coverage AS (
                    SELECT c.group_value, c.period, c.bonds,
                           count(f.isin) AS active_bonds,
                           c.bonds * 100.0 / nullif(count(f.isin), 0) AS coverage_pct,
                           c.mean, c.median, c.p25, c.p75, c.minimum, c.maximum,
                           c.cross_section_std,
                           c.min_isin, c.min_description, c.max_isin, c.max_description
                    FROM cross_section_extrema c
                    LEFT JOIN first_seen f ON f.group_value=c.group_value
                         AND f.first_date < c.period + INTERVAL '1 {interval_unit}'
                    GROUP BY c.group_value, c.period, c.bonds, c.mean, c.median,
                             c.p25, c.p75, c.minimum, c.maximum, c.min_isin,
                             c.cross_section_std, c.min_description, c.max_isin, c.max_description
                ), coverage_counts AS (
                    SELECT group_value, count(*) AS total_periods,
                           count(*) FILTER (WHERE coverage_pct >= ?) AS eligible_periods
                    FROM coverage GROUP BY group_value
                ), qualified AS (
                    SELECT * FROM coverage WHERE coverage_pct >= ?
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
                    FROM qualified
                )
                SELECT r.period, r.bonds, r.mean, r.median, r.p25, r.p75, r.minimum, r.maximum,
                       r.vol_63, r.vol_252, r.mom_63, r.mom_252, r.coverage_pct, r.active_bonds,
                       cc.total_periods, cc.eligible_periods,
                       r.min_isin, r.min_description, r.max_isin, r.max_description,
                       r.cross_section_std
                FROM coverage_counts cc
                LEFT JOIN rolling r ON r.group_value=cc.group_value
                WHERE cc.group_value=? ORDER BY r.period""",
            [
                run_info["preprocessing"]["clean_parquet"],
                minimum_coverage_pct,
                minimum_coverage_pct,
                group_value,
            ],
        ).fetchall()
        if not rows:
            return [], 0, 0
        total_periods = int(rows[0][14] or 0)
        eligible_periods = int(rows[0][15] or 0)
        eligible_rows = [row[:14] + row[16:20] + row[20:21] for row in rows if row[0] is not None]
        return eligible_rows, total_periods - eligible_periods, total_periods
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

        spread_snapshot_id = None
        if measure == "G-spread":
            try:
                snapshot = connection.execute(
                    """SELECT snapshot_id FROM spread_snapshot_runs
                       WHERE trace_run_id=? ORDER BY generated_at_utc DESC LIMIT 1""",
                    [run_info["run_id"]],
                ).fetchone()
            except duckdb.CatalogException as error:
                raise ValueError("No G-spread snapshot is available. Build a G-spread snapshot first.") from error
            if not snapshot:
                raise ValueError("No G-spread snapshot is available for this TRACE run.")
            spread_snapshot_id = snapshot[0]
            source_sql = "bond_spread_observations"
            source_filter = "isin=? AND snapshot_id=? AND NOT yield_reversal_flagged"
            source_params = [isin, spread_snapshot_id]
        else:
            source_sql = "read_parquet(?)"
            source_filter = "isin=?"
            source_params = [preprocessing["clean_parquet"], isin]

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
                FROM {source_sql} WHERE {source_filter} ORDER BY trade_date""",
            source_params,
        ).fetchall()
        partitions = connection.execute(
            f"""WITH clean AS (
                    SELECT trade_date, {value_sql} AS value
                    FROM {source_sql} WHERE {source_filter}
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
            source_params,
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
            "spread_snapshot_id": spread_snapshot_id,
            "daily": [(row[0], row[1]) for row in daily if row[1] is not None],
            "partitions": partitions,
        }
    finally:
        connection.close()


def _rolling_interval_metrics(
    rows: list[tuple[Any, ...]],
    interval: str,
) -> tuple[list[date], dict[str, list[float | None]], dict[str, list[float | None]]]:
    dates = [row[0] for row in rows]
    values = [float(row[2]) if row[2] is not None else None for row in rows]
    volatility: dict[str, list[float | None]] = {}
    rate_of_change: dict[str, list[float | None]] = {}
    windows = {
        "Daily": (63, 252),
        "Monthly": (3, 12),
        "Quarterly": (4, 12),
        "Yearly": (2, 3),
    }[interval]
    unit = "trade" if interval == "Daily" else interval.lower().removesuffix("ly")
    for window in windows:
        vol_values: list[float | None] = []
        roc_values: list[float | None] = []
        for index, current in enumerate(values):
            sample = [
                value for value in values[max(0, index - window + 1): index + 1]
                if value is not None
            ]
            vol_values.append(statistics.stdev(sample) if len(sample) > 1 else None)
            previous_index = index - window
            previous = values[previous_index] if previous_index >= 0 else None
            roc_values.append(
                (current / previous - 1) * 100
                if current is not None and previous not in (None, 0)
                else None
            )
        label = f"{window}-{unit}"
        volatility[label] = vol_values
        rate_of_change[label] = roc_values
    return dates, volatility, rate_of_change


class HistoryChart(QWidget):
    viewChanged = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(250)
        self.mode = "Daily observations"
        self.value_label = "Yield (%)"
        self.daily: list[tuple[date, float]] = []
        self.partitions: list[tuple[Any, ...]] = []
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self._x_positions: list[float] = []
        self._plot_left = 68
        self._plot_right = 0
        self._drag_origin: QPointF | None = None
        self._drag_zoom: tuple[float, float] | None = None
        self.setMouseTracking(True)

    def set_data(self, data: dict[str, Any], mode: str) -> None:
        self.daily = data["daily"]
        self.partitions = data["partitions"]
        self.value_label = data["value_label"]
        self.mode = mode
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self._x_positions = []
        self.update()
        self.viewChanged.emit(self.zoom_start, self.zoom_end)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self.daily and not self.partitions:
            return
        plot_width = max(1, self._plot_right - self._plot_left)
        anchor = max(0.0, min(1.0, (event.position().x() - self._plot_left) / plot_width))
        width = self.zoom_end - self.zoom_start
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        new_width = max(0.025, min(1.0, width * factor))
        anchor_date = self.zoom_start + anchor * width
        start = anchor_date - anchor * new_width
        self.zoom_start = max(0.0, min(1.0 - new_width, start))
        self.zoom_end = self.zoom_start + new_width
        self.update()
        self.viewChanged.emit(self.zoom_start, self.zoom_end)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position()
            self._drag_zoom = (self.zoom_start, self.zoom_end)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is not None and self._drag_zoom is not None:
            width = self._drag_zoom[1] - self._drag_zoom[0]
            plot_width = max(1, self._plot_right - self._plot_left)
            shift = -(event.position().x() - self._drag_origin.x()) / plot_width * width
            start = max(0.0, min(1.0 - width, self._drag_zoom[0] + shift))
            self.zoom_start, self.zoom_end = start, start + width
            self.update()
            self.viewChanged.emit(self.zoom_start, self.zoom_end)
            return
        if self._x_positions:
            x = event.position().x()
            index = min(range(len(self._x_positions)), key=lambda i: abs(self._x_positions[i] - x))
            if abs(self._x_positions[index] - x) <= 28:
                self._show_point_tooltip(index, event.globalPosition())
            else:
                self.setToolTip("Scroll to zoom · drag to pan · double-click to reset")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = None
            self._drag_zoom = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.zoom_start, self.zoom_end = 0.0, 1.0
        self.update()
        self.viewChanged.emit(self.zoom_start, self.zoom_end)
        event.accept()

    def _show_point_tooltip(self, index: int, position: QPointF) -> None:
        if self.mode == "Daily observations":
            point_date, value = self.daily[index]
            gap = (point_date - self.daily[index - 1][0]).days if index else None
            details = [f"{self.value_label}: {value:.3f}"]
        else:
            row = self.partitions[index]
            point_date = row[0]
            gap = (point_date - self.partitions[index - 1][0]).days if index else None
            details = [
                f"Contributing bonds: {int(row[1]):,}",
                f"Coverage: {float(row[12]):.1f}% of {int(row[13]):,} bonds first seen by interval end",
                f"Mean: {row[2]:.3f}" if row[2] is not None else "Mean: —",
                f"Median: {row[3]:.3f}" if row[3] is not None else "Median: —",
                f"P25–P75: {row[4]:.3f}–{row[5]:.3f}" if row[4] is not None and row[5] is not None else "P25–P75: —",
                f"Min: {row[6]:.3f} | {row[15]} | ISIN {row[14]}"
                if row[6] is not None else "Min: —",
                f"Max: {row[7]:.3f} | {row[17]} | ISIN {row[16]}"
                if row[7] is not None else "Max: —",
            ]
        details.insert(0, str(point_date))
        if gap is not None:
            details.append(f"Gap from prior point: {gap} days")
        self.setToolTip("\n".join(details))
        QToolTip.showText(position.toPoint(), "\n".join(details), self)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#151a23"))
        left, right, top, bottom = 68, self.width() - 24, 20, self.height() - 56
        self._plot_left, self._plot_right = left, right
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        if self.mode == "Daily observations":
            values = [point[1] for point in self.daily]
        else:
            values = [
                float(row[index])
                for row in self.partitions
                for index in (4, 5, 6, 7)
                if row[index] is not None
            ]
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

        series_dates = (
            [point[0] for point in self.daily]
            if self.mode == "Daily observations"
            else [row[0] for row in self.partitions]
        )
        first_ordinal = series_dates[0].toordinal()
        last_ordinal = series_dates[-1].toordinal()
        full_span = max(1, last_ordinal - first_ordinal)
        visible_start = first_ordinal + full_span * self.zoom_start
        visible_end = first_ordinal + full_span * self.zoom_end
        visible_span = max(1.0, visible_end - visible_start)

        def x_pos(point_date: date) -> float:
            return left + (point_date.toordinal() - visible_start) / visible_span * plot_width

        if self.mode == "Daily observations":
            xs = [x_pos(observed_day) for observed_day, _ in self.daily]
            self._x_positions = xs
            visible = [
                (point, x)
                for point, x in zip(self.daily, xs)
                if left <= x <= right
            ]
            painter.setPen(QPen(QColor("#53b7d8"), 1))
            painter.setBrush(QColor("#53b7d8"))
            for (_observed_day, value), x in visible:
                painter.drawEllipse(QPointF(x, y_pos(value)), 2.5, 2.5)
        else:
            xs = [x_pos(row[0]) for row in self.partitions]
            self._x_positions = xs
            band_points = [
                (x, row[4], row[5])
                for x, row in zip(xs, self.partitions)
                if left <= x <= right and row[4] is not None and row[5] is not None
            ]
            if band_points:
                def smooth_boundary(points: list[tuple[float, float]]) -> QPainterPath:
                    path = QPainterPath()
                    path.moveTo(*points[0])
                    for index in range(len(points) - 1):
                        x0, y0 = points[index]
                        x1, y1 = points[index + 1]
                        xp, yp = points[index - 1] if index else points[index]
                        xn, yn = points[index + 2] if index + 2 < len(points) else points[index + 1]
                        path.cubicTo(
                            x0 + (x1 - xp) / 6,
                            y0 + (y1 - yp) / 6,
                            x1 - (xn - x0) / 6,
                            y1 - (yn - y0) / 6,
                            x1,
                            y1,
                        )
                    return path

                upper = smooth_boundary(
                    [(x, y_pos(float(p75))) for x, _p25, p75 in band_points]
                )
                lower = smooth_boundary(
                    [(x, y_pos(float(p25))) for x, p25, _p75 in reversed(band_points)]
                )
                painter.setPen(QPen(QColor("#53b7d8"), 1.8))
                painter.drawPath(upper)
                painter.drawPath(lower)

                mean_points = [
                    (x, y_pos(float(row[2])))
                    for x, row in zip(xs, self.partitions)
                    if left <= x <= right and row[2] is not None
                ]
                median_points = [
                    (x, y_pos(float(row[3])))
                    for x, row in zip(xs, self.partitions)
                    if left <= x <= right and row[3] is not None
                ]
                if mean_points:
                    painter.setPen(QPen(QColor("#f0a35e"), 2.0))
                    painter.drawPath(smooth_boundary(mean_points))
                if median_points:
                    median_pen = QPen(QColor("#c6a6e8"), 1.4, Qt.PenStyle.DashLine)
                    painter.setPen(median_pen)
                    painter.drawPath(smooth_boundary(median_points))

                painter.setBrush(QColor(190, 202, 216, 100))
                painter.setPen(QPen(QColor(190, 202, 216, 110), 1))
                for x, row in zip(xs, self.partitions):
                    if not left <= x <= right:
                        continue
                    for value in (row[6], row[7]):
                        if value is not None:
                            painter.drawEllipse(QPointF(x, y_pos(float(value))), 2.0, 2.0)
        painter.setPen(QColor("#aab6c6"))
        tick_count = 5
        for tick in range(tick_count):
            fraction = tick / (tick_count - 1)
            x = left + fraction * plot_width
            tick_date = date.fromordinal(round(visible_start + fraction * visible_span))
            painter.setPen(QPen(QColor("#303947"), 1))
            painter.drawLine(int(x), bottom, int(x), bottom + 4)
            painter.setPen(QColor("#aab6c6"))
            text = tick_date.strftime("%Y-%m")
            painter.drawText(int(x - 30), self.height() - 12, text)
        painter.setPen(QColor("#aab6c6"))
        legend = (
            "P25–P75 shaded band · mean (orange) · median (dashed) · min/max (faint points) · "
            "scroll to zoom, drag to pan, double-click to reset"
            if self.mode != "Daily observations"
            else "One point per valid reported trade date · scroll to zoom, drag to pan, double-click to reset"
        )
        painter.drawText(left + 12, top + 15, legend)


class MetricChart(QWidget):
    """Compact date-series chart for rolling and partition diagnostics."""

    def __init__(self, empty_message: str) -> None:
        super().__init__()
        self.setMinimumHeight(220)
        self.dates: list[date] = []
        self.series: dict[str, list[float | None]] = {}
        self.value_label = ""
        self.empty_message = empty_message
        self._x_positions: list[float] = []
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self.setMouseTracking(True)

    def set_series(
        self,
        dates: list[date],
        series: dict[str, list[float | None]],
        value_label: str,
        empty_message: str | None = None,
    ) -> None:
        self.dates = dates
        self.series = series
        self.value_label = value_label
        if empty_message is not None:
            self.empty_message = empty_message
        self._x_positions = []
        self.update()

    def set_view_range(self, start: float, end: float) -> None:
        self.zoom_start, self.zoom_end = start, end
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._x_positions:
            x = event.position().x()
            index = min(range(len(self._x_positions)), key=lambda i: abs(self._x_positions[i] - x))
            if abs(self._x_positions[index] - x) <= 30:
                lines = [self.dates[index].isoformat()]
                for name, values in self.series.items():
                    value = values[index]
                    lines.append(f"{name}: {value:.3f}" if value is not None else f"{name}: —")
                tooltip = "\n".join(lines)
                self.setToolTip(tooltip)
                QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
        super().mouseMoveEvent(event)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#151a23"))
        left, right, top, bottom = 68, self.width() - 24, 24, self.height() - 54
        width, height = max(1, right - left), max(1, bottom - top)
        values = [value for row in self.series.values() for value in row if value is not None]
        if not self.dates or not values:
            painter.setPen(QColor("#aab6c6"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_message)
            self._x_positions = []
            return

        low, high = min(values), max(values)
        padding = max((high - low) * 0.08, abs(high) * 0.02, 0.01)
        low, high = low - padding, high + padding

        def y_pos(value: float) -> float:
            return bottom - (value - low) / (high - low) * height

        first_day = self.dates[0].toordinal()
        span = max(1, self.dates[-1].toordinal() - first_day)
        visible_start = first_day + span * self.zoom_start
        visible_end = first_day + span * self.zoom_end
        visible_span = max(1.0, visible_end - visible_start)
        xs = [left + (item.toordinal() - visible_start) * width / visible_span for item in self.dates]
        self._x_positions = xs

        for step in range(5):
            y = top + height * step / 4
            value = high - (high - low) * step / 4
            painter.setPen(QPen(QColor("#303947"), 1))
            painter.drawLine(left, int(y), right, int(y))
            painter.setPen(QColor("#aab6c6"))
            painter.drawText(4, int(y + 5), f"{value:.2f}")
        painter.save()
        painter.translate(16, (top + bottom) / 2)
        painter.rotate(-90)
        painter.setPen(QColor("#aab6c6"))
        painter.drawText(int(-height / 2), 0, self.value_label)
        painter.restore()

        colors = ("#7dd3fc", "#f0a35e", "#c6a6e8", "#7bd88f")
        for series_index, (name, values_for_series) in enumerate(self.series.items()):
            path = QPainterPath()
            started = False
            for x, value in zip(xs, values_for_series):
                if x < left or x > right:
                    started = False
                    continue
                if value is None:
                    started = False
                elif not started:
                    path.moveTo(x, y_pos(value))
                    started = True
                else:
                    path.lineTo(x, y_pos(value))
            painter.setPen(QPen(QColor(colors[series_index % len(colors)]), 1.8))
            painter.drawPath(path)

        for tick in range(5):
            fraction = tick / 4
            x = left + fraction * width
            tick_date = date.fromordinal(round(visible_start + fraction * visible_span))
            painter.setPen(QColor("#aab6c6"))
            painter.drawText(int(x - 30), self.height() - 12, tick_date.strftime("%Y-%m"))
        painter.setPen(QColor("#aab6c6"))
        legend_x = left + 8
        for index, name in enumerate(self.series):
            color = QColor(colors[index % len(colors)])
            painter.setPen(QPen(color, 2))
            painter.drawLine(legend_x, top + 10, legend_x + 18, top + 10)
            painter.setPen(QColor("#d6deea"))
            painter.drawText(legend_x + 24, top + 14, name)
            legend_x += 150


class TraceHistoryInspector(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TRACE Bond History Inspector")
        self.resize(1720, 900)
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
        self.measure.addItems(["Yield", "Price", "G-spread"])
        self.measure.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.measure)
        controls.addWidget(QLabel("Time interval"))
        self.partition = QComboBox()
        self.partition.addItems(["Daily", "Monthly", "Quarterly", "Yearly"])
        self.partition.setCurrentText("Monthly")
        self.partition.currentTextChanged.connect(self.refresh)
        controls.addWidget(self.partition)
        self.minimum_coverage_label = QLabel("Minimum coverage")
        controls.addWidget(self.minimum_coverage_label)
        self.minimum_coverage = QDoubleSpinBox()
        self.minimum_coverage.setRange(0, 100)
        self.minimum_coverage.setDecimals(0)
        self.minimum_coverage.setSingleStep(5)
        self.minimum_coverage.setSuffix("%")
        self.minimum_coverage.setValue(50)
        self.minimum_coverage.setToolTip(
            "An interval is shown only when this share of bonds whose first TRACE observation "
            "has occurred by interval end has a valid observation in that interval."
        )
        self.minimum_coverage.valueChanged.connect(self.refresh)
        controls.addWidget(self.minimum_coverage)
        layout.addLayout(controls)

        self.summary = QLabel("Loading history database…")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        self.chart = HistoryChart()
        self.volatility_chart = MetricChart("No trailing-volatility series for this selection.")
        self.rate_chart = MetricChart("No rate-of-change series for this selection.")
        self.dispersion_chart = MetricChart("Cross-sectional dispersion is available in group view.")
        chart_splitter = QSplitter(Qt.Orientation.Vertical)
        chart_splitter.setChildrenCollapsible(False)
        chart_splitter.addWidget(self.chart)
        secondary_panel = QWidget()
        secondary_layout = QVBoxLayout(secondary_panel)
        secondary_layout.setContentsMargins(0, 0, 0, 0)
        secondary_controls = QHBoxLayout()
        secondary_controls.addWidget(QLabel("Secondary panel"))
        self.secondary_metric = QComboBox()
        self.secondary_metric.addItems(
            ["Trailing volatility", "Rate of change", "Partition standard deviation"]
        )
        self.secondary_stack = QStackedWidget()
        self.secondary_stack.addWidget(self.volatility_chart)
        self.secondary_stack.addWidget(self.rate_chart)
        self.secondary_stack.addWidget(self.dispersion_chart)
        self.secondary_metric.currentIndexChanged.connect(self.secondary_stack.setCurrentIndex)
        secondary_controls.addWidget(self.secondary_metric)
        secondary_controls.addStretch(1)
        secondary_layout.addLayout(secondary_controls)
        secondary_layout.addWidget(self.secondary_stack, 1)
        chart_splitter.addWidget(secondary_panel)
        chart_splitter.setStretchFactor(0, 3)
        chart_splitter.setStretchFactor(1, 2)
        chart_splitter.setSizes([390, 230])
        self.chart.viewChanged.connect(self._sync_secondary_view)
        self.tabs.addTab(chart_splitter, "Charts")
        self.stats = QTableWidget(0, 13)
        self.stats.setHorizontalHeaderLabels(
            ["Period", "Observations", "Mean", "Median", "P25", "P75", "Min", "Max",
             "Vol 63", "Vol 252", "Mom 63", "Mom 252", "Coverage %"]
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

    def _sync_secondary_view(self, start: float, end: float) -> None:
        for metric_chart in (
            self.volatility_chart,
            self.rate_chart,
            self.dispersion_chart,
        ):
            metric_chart.set_view_range(start, end)

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
        spread_item = self.measure.model().item(2)
        if spread_item is not None:
            spread_item.setEnabled(individual)
        if not individual and self.measure.currentText() == "G-spread":
            self.measure.setCurrentText("Yield")
        for widget in (self.bond_label, self.bond):
            widget.setVisible(individual)
        for widget in (self.group_by_label, self.group_by, self.group_value_label, self.group_value):
            widget.setVisible(not individual)
        self.minimum_coverage_label.setVisible(not individual)
        self.minimum_coverage.setVisible(not individual)
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

    def _update_secondary_charts(
        self,
        rows: list[tuple[Any, ...]],
        group_mode: bool,
    ) -> None:
        dates, volatility, rate_of_change = _rolling_interval_metrics(
            rows, self.partition.currentText()
        )
        value_unit = "Yield (percentage points)" if self.measure.currentText() == "Yield" else "Price units"
        self.volatility_chart.set_series(
            dates,
            volatility,
            f"Trailing standard deviation · {value_unit}",
        )
        self.rate_chart.set_series(
            dates,
            rate_of_change,
            "Rate of change (%)",
        )
        if group_mode and rows:
            self.dispersion_chart.set_series(
                dates,
                {"Cross-sectional standard deviation": [
                    float(row[18]) if row[18] is not None else None for row in rows
                ]},
                value_unit,
                "No cross-sectional standard-deviation values for this selection.",
            )
        else:
            self.dispersion_chart.set_series(
                [],
                {},
                value_unit,
                "Cross-sectional dispersion is available in group view.",
            )

    def refresh(self, *_args: Any) -> None:
        if not self.run_info:
            return
        if self.view_mode.currentText() == "Group partition":
            group_value = self.group_value.currentText().strip()
            if not group_value:
                return
            try:
                rows, omitted_periods, total_periods = load_group_data(
                    self.group_by.currentText(), group_value,
                    self.measure.currentText(), self.partition.currentText(), self.run_info,
                    self.minimum_coverage.value(),
                )
            except Exception as error:
                self.statusBar().showMessage(f"Could not load partition history: {error}")
                return
            if not rows:
                self.chart.daily = []
                self.chart.partitions = []
                self.chart._x_positions = []
                self.chart.mode = "Partition ranges"
                self.chart.update()
                self.stats.setRowCount(0)
                self._update_secondary_charts([], group_mode=True)
                self.summary.setText(
                    f"{self.group_by.currentText()} = {group_value} | no intervals meet the "
                    f"minimum coverage of {self.minimum_coverage.value():.0f}% "
                    f"({omitted_periods}/{total_periods} intervals omitted)"
                )
                return
            self.chart.set_data(
                {"daily": [], "partitions": rows,
                 "value_label": _measure_sql(self.measure.currentText())[1]},
                "Partition ranges",
            )
            self._update_secondary_charts(rows, group_mode=True)
            self._fill_stats(rows, group_mode=True)
            first_date, last_date = rows[0][0], rows[-1][0]
            self.summary.setText(
                f"{self.group_by.currentText()} = {group_value} | {self.measure.currentText()} | "
                f"{self.partition.currentText().lower()} cross-section | {first_date} to {last_date} | "
                f"each bond has equal weight within a time bucket | "
                f"minimum {self.minimum_coverage.value():.0f}% coverage; "
                f"omitted {omitted_periods}/{total_periods} low-coverage intervals"
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
        self._update_secondary_charts(self.data["partitions"], group_mode=False)
        self._fill_stats(self.data["partitions"], group_mode=False)
        if self.measure.currentText() == "Yield":
            excl_label = (
                f"zero yields {self.data['zero_yield_count']:,}; "
                f"sentinel yields {self.data['sentinel_yield_count']:,}; "
                f"null yields {self.data['null_yield_count']:,}"
            )
        elif self.measure.currentText() == "Price":
            excl_label = (
                f"zero prices {self.data['zero_price_count']:,}; "
                f"sentinel prices {self.data['sentinel_price_count']:,}; "
                f"null prices {self.data['null_price_count']:,}"
            )
        else:
            excl_label = "latest cleaned-TRACE G-spread snapshot; reversal-flagged observations excluded"
        valid = len(self.data["daily"])
        self.summary.setText(
            f"{self.data['issuer']} | {isin} | {self.measure.currentText()} | "
            f"{self.data['status']} | {valid:,} valid observations / {self.data['raw_count']:,} raw rows | "
            f"{excl_label} | first {self.data['first_date']} | last {self.data['last_date']}"
        )
        self.statusBar().showMessage(
            f"Loaded {valid:,} valid observations"
            + (f" from spread snapshot {self.data['spread_snapshot_id']}" if self.data.get("spread_snapshot_id") else "")
            + ". Rolling volatility and momentum use prior observed trades."
        )

    def _fill_stats(self, rows: list[tuple[Any, ...]], group_mode: bool) -> None:
        self.stats.setRowCount(len(rows))
        self.tabs.setTabText(1, "Group statistics" if group_mode else "Bond time-bucket statistics")
        headers = [
            "Period", "Bonds" if group_mode else "Observations", "Mean", "Median", "P25", "P75", "Min", "Max",
            "Vol 63", "Vol 252", "Mom 63", "Mom 252",
        ]
        headers.append("Coverage %" if group_mode else "")
        self.stats.setHorizontalHeaderLabels(headers)
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
            display = [row[0], row[1], *row[2:12]]
            if group_mode:
                display.append(row[12])
            for column, value in enumerate(display):
                if value is None:
                    text = ""
                elif column == 0:
                    text = str(value)
                elif column == 1:
                    text = f"{int(value):,}"
                elif group_mode and column == 12:
                    text = f"{float(value):.1f}%"
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
