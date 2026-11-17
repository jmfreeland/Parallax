"""Visual node editor for reusable multi-signal strategy JSON files."""

from __future__ import annotations

import json
import sys
import csv
import uuid
from datetime import date
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QThread, Signal, QDate
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSplitter, QStackedWidget,
    QSpinBox, QDoubleSpinBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QVBoxLayout, QWidget, QDialog, QDateEdit, QDialogButtonBox, QProgressDialog,
    QTableWidget, QTableWidgetItem,
)

from .strategy import (
    CombinerNode, Strategy, SignalComponent, TransformerNode,
)
from .trace_history import DEFAULT_SNAPSHOT, DATABASE_PATH
from .strategy_run import run_strategy_signals, recombine_cached_signals
from .signal_evaluation import _has_current_cleaning_version


PREDICTORS = {
    "Nelson-Siegel residual": "nelson_siegel_residual",
    "Historical percentile": "historical_partition_percentile",
    "Historical z-score": "historical_partition_zscore",
}
PREDICTOR_LABELS = {value: label for label, value in PREDICTORS.items()}
STRATEGIES_DIR = Path(__file__).resolve().parents[1] / "data" / "strategies"
STRATEGY_RUNS_DIR = STRATEGIES_DIR / "runs"


class StrategyRunWorker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, strategy: Strategy, parameters: dict[str, Any], cache_path: Path) -> None:
        super().__init__()
        self.strategy = strategy
        self.parameters = parameters
        self.cache_path = cache_path

    def run(self) -> None:
        try:
            run_strategy_signals(
                self.strategy, self.parameters, self.cache_path,
                on_progress=self.progress.emit,
            )
            self.succeeded.emit(str(self.cache_path))
        except Exception as error:
            self.failed.emit(str(error))


class StrategyInputsDialog(QDialog):
    def __init__(self, parent: QWidget, bond_list: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calculate strategy inputs")
        layout = QFormLayout(self)
        self.start_date = QDateEdit(calendarPopup=True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate().addDays(-90))
        self.end_date = QDateEdit(calendarPopup=True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate.currentDate().addDays(-1))
        layout.addRow("Signal start date", self.start_date)
        layout.addRow("Signal end date", self.end_date)
        self.bond_list = QLineEdit(bond_list)
        layout.addRow("Bond list", self.bond_list)
        self.data_source = QComboBox()
        for label, value in (("Cleaned Parquet", "cleaned_parquet"),
                             ("Preprocessed Parquet", "preprocessed_parquet"),
                             ("Raw DuckDB history", "raw_duckdb")):
            self.data_source.addItem(label, value)
        layout.addRow("TRACE source", self.data_source)
        self.data_path = QLineEdit()
        self._previous_source: str | None = None
        self.data_browse = QPushButton("Browse…")
        data_row = QHBoxLayout()
        data_row.addWidget(self.data_path, 1)
        data_row.addWidget(self.data_browse)
        layout.addRow("Parquet dataset", data_row)
        self.data_source.currentIndexChanged.connect(self._source_changed)
        self.data_browse.clicked.connect(self._browse_data)
        self._source_changed()
        self.note = QLabel(
            "Matching cached signals are reused. A different date range or TRACE source "
            "recalculates all inputs; added or edited signals recalculate only those signals."
        )
        self.note.setWordWrap(True)
        layout.addRow(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _source_changed(self, *_args: Any) -> None:
        source = str(self.data_source.currentData())
        changed = self._previous_source != source
        if changed:
            self.data_path.clear()
            self._previous_source = source
        is_raw = source == "raw_duckdb"
        self.data_path.setEnabled(not is_raw)
        self.data_browse.setEnabled(not is_raw)
        if not is_raw and not self.data_path.text().strip():
            prefix = "clean_id=" if source == "cleaned_parquet" else "preprocess_id="
            root = DATABASE_PATH.parent / "preprocessed"
            candidates = list(root.rglob("observations.parquet")) if root.exists() else []
            candidates = [
                item for item in candidates
                if item.parent.name.startswith(prefix)
                and (source != "cleaned_parquet" or _has_current_cleaning_version(item))
            ]
            if candidates:
                self.data_path.setText(str(max(candidates, key=lambda item: item.stat().st_mtime)))

    def _browse_data(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select TRACE observations", self.data_path.text(), "Parquet (*.parquet)"
        )
        if path:
            self.data_path.setText(path)

    def evaluation_parameters(self) -> dict[str, Any]:
        if self.end_date.date() < self.start_date.date():
            raise ValueError("Signal end date must be on or after the start date")
        source = str(self.data_source.currentData())
        dataset = Path(self.data_path.text()).expanduser() if source != "raw_duckdb" else None
        if source != "raw_duckdb" and (dataset is None or not dataset.is_file()):
            raise ValueError("Select an existing TRACE observations.parquet dataset")
        bond_list = Path(self.bond_list.text()).expanduser()
        if not bond_list.is_file():
            raise ValueError("Select an existing reference bond-list CSV")
        return {
            "start_date": date.fromisoformat(self.start_date.date().toString("yyyy-MM-dd")),
            "end_date": date.fromisoformat(self.end_date.date().toString("yyyy-MM-dd")),
            "horizon_days": 21,
            "bond_list_path": bond_list,
            "database_path": DATABASE_PATH,
            "data_source": source,
            "data_path": dataset,
        }


def _transform_label(transform: dict[str, Any]) -> str:
    kind = transform.get("type", "transform")
    if kind == "winsorize":
        lower = 100 * float(transform.get("lower", 0.01))
        upper = 100 * float(transform.get("upper", 0.99))
        return f"Winsorize · {lower:g}–{upper:g}%"
    return {
        "zscore": "Z-score normalization",
        "percentile": "Percentile rank · 0–1",
        "minmax": "Min–max scaling · 0–1",
    }.get(kind, str(kind).replace("_", " ").title())


def _transform_description(transform: dict[str, Any]) -> str:
    return {
        "winsorize": "Caps cross-sectional tails.",
        "zscore": "Centers and scales the cross-section.",
        "percentile": "Maps ranks onto a 0–1 scale.",
        "minmax": "Rescales the cross-section to 0–1.",
    }.get(str(transform.get("type")), "Transforms the incoming score.")


class HeatmapItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(left, (float, int)) and isinstance(right, (float, int)):
            return left < right
        return super().__lt__(other)


class StrategyInspectorDialog(QDialog):
    METADATA_ALIASES = {
        "Ticker": ("equity_ticker", "ticker", "issuer_ticker", "bloomberg_ticker"),
        "Coupon": ("coupon", "coupon_pct", "coupon_rate", "coupon_rate_pct", "coupon_percent"),
        "Maturity": ("maturity_date", "maturity"),
        "Sector": ("sector", "industry_sector", "gics_sector", "industry"),
        "Issuer": ("issuer", "issuer_name", "company_name"),
        "Rating": ("rating", "composite_rating", "credit_rating"),
    }

    def __init__(self, strategy: Strategy, rows: list[dict[str, Any]],
                 bond_details: dict[str, dict[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Strategy inspector · {strategy.name}")
        self.resize(1450, 820)
        self.strategy = strategy
        self.rows_by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            self.rows_by_date.setdefault(str(row["as_of_date"]), []).append(row)
        layout = QVBoxLayout(self)
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("Signal date"))
        self.date_choice = QComboBox()
        self.date_choice.addItems(sorted(self.rows_by_date, reverse=True))
        self.date_choice.currentTextChanged.connect(self._show_date)
        date_row.addWidget(self.date_choice)
        self.legend = QLabel("Heatmap by date: low values  ←  red · neutral · green  →  high values")
        date_row.addWidget(self.legend)
        layout.addLayout(date_row)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort by"))
        self.sort_by = QComboBox()
        self.sort_by.currentIndexChanged.connect(self._apply_sort)
        sort_row.addWidget(self.sort_by)
        sort_row.addWidget(QLabel("Order"))
        self.sort_order = QComboBox()
        self.sort_order.addItem("Descending", Qt.SortOrder.DescendingOrder.value)
        self.sort_order.addItem("Ascending", Qt.SortOrder.AscendingOrder.value)
        self.sort_order.currentIndexChanged.connect(self._apply_sort)
        sort_row.addWidget(self.sort_order)
        sort_row.addStretch(1)
        layout.addLayout(sort_row)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._sync_sort_controls)
        layout.addWidget(self.table, 1)
        self.bond_details = bond_details

        node_names = {f"signal:{signal.signal_id}": signal.signal_id for signal in strategy.signals}
        node_names.update({node.node_id: node.name for node in strategy.transformer_nodes})
        node_names.update({node.node_id: node.name for node in strategy.combiners})
        metadata_columns: list[tuple[str, str]] = [("ISIN", "isin")]
        for label, candidates in self.METADATA_ALIASES.items():
            selected = next((field for field in candidates
                             if any(field in bond for bond in bond_details.values())), None)
            if selected:
                metadata_columns.append((label, f"metadata:{selected}"))
        node_parents = {node.node_id: [node.input_node_id] for node in strategy.transformer_nodes}
        node_parents.update({node.node_id: list(node.input_node_ids) for node in strategy.combiners})

        def score_key(node_id: str) -> str:
            return f"signal:{node_id.split(':', 1)[1]}" if node_id.startswith("signal:") else f"node:{node_id}"

        output_name = node_names.get(strategy.output_node_id, strategy.output_node_id)
        score_columns: list[tuple[str, str]] = [
            (f"Strategy score · {output_name}", score_key(strategy.output_node_id))
        ]
        seen_score_keys = {score_columns[0][1]}
        pending = [(parent_id, output_name) for parent_id in node_parents.get(strategy.output_node_id, [])]
        while pending:
            node_id, child_name = pending.pop(0)
            key = score_key(node_id)
            if key in seen_score_keys:
                continue
            seen_score_keys.add(key)
            score_columns.append((
                f"Input · {child_name} ← {node_names.get(node_id, node_id)}", key
            ))
            pending.extend((parent_id, node_names.get(node_id, node_id))
                           for parent_id in node_parents.get(node_id, []))
        all_node_ids = [*(f"signal:{signal.signal_id}" for signal in strategy.signals),
                        *(node.node_id for node in strategy.transformer_nodes),
                        *(node.node_id for node in strategy.combiners)]
        for node_id in all_node_ids:
            key = score_key(node_id)
            if key not in seen_score_keys:
                score_columns.append((f"Unconnected · {node_names.get(node_id, node_id)}", key))
                seen_score_keys.add(key)
        self.metadata_columns = metadata_columns
        self.score_columns = score_columns
        self._show_date(self.date_choice.currentText())

    @staticmethod
    def _heat_color(value: float, low: float, high: float) -> QColor:
        if high <= low:
            fraction = 0.5
        else:
            fraction = (value - low) / (high - low)
        red = QColor("#ef6a68")
        neutral = QColor("#f1f5f9")
        green = QColor("#4ade80")
        start, end, local = (red, neutral, fraction * 2) if fraction <= 0.5 else (neutral, green, (fraction - 0.5) * 2)
        return QColor(*(round(start.getRgb()[index] * (1 - local) + end.getRgb()[index] * local)
                        for index in range(3)))

    def _show_date(self, as_of_date: str) -> None:
        rows = self.rows_by_date.get(as_of_date, [])
        columns = [*self.score_columns, *self.metadata_columns]
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([label for label, _key in columns])
        self.sort_by.blockSignals(True)
        self.sort_by.clear()
        for column, (label, _key) in enumerate(columns):
            self.sort_by.addItem(label, column)
        self.sort_by.setCurrentIndex(0)
        self.sort_by.blockSignals(False)
        self.table.setRowCount(len(rows))
        score_values: dict[str, list[float]] = {}
        for _label, key in self.score_columns:
            values = [float(row[key]) for row in rows
                      if row.get(key) is not None and math.isfinite(float(row[key]))]
            score_values[key] = values
        bounds = {key: (min(values), max(values)) for key, values in score_values.items() if values}
        for row_index, row in enumerate(rows):
            details = self.bond_details.get(str(row.get("isin", "")), {})
            for column_index, (_label, key) in enumerate(columns):
                if key.startswith("metadata:"):
                    value: Any = details.get(key.split(":", 1)[1], "")
                elif key == "isin":
                    value = row.get("isin", "")
                else:
                    value = row.get(key)
                item = HeatmapItem("" if value is None else f"{value:.5f}" if isinstance(value, (int, float)) else str(value))
                item.setData(Qt.ItemDataRole.UserRole, value)
                if key in bounds and value is not None and math.isfinite(float(value)):
                    low, high = bounds[key]
                    item.setBackground(QBrush(self._heat_color(float(value), low, high)))
                    item.setForeground(QBrush(QColor("#111827")))
                    item.setToolTip(f"{float(value):.6f} · date range column min {low:.6f}, max {high:.6f}")
                self.table.setItem(row_index, column_index, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)
        self._apply_sort()

    def _apply_sort(self, *_args: Any) -> None:
        if not self.table.isSortingEnabled() or self.sort_by.currentData() is None:
            return
        direction = Qt.SortOrder(int(self.sort_order.currentData()))
        self.table.sortItems(int(self.sort_by.currentData()), direction)

    def _sync_sort_controls(self, section: int, order: Qt.SortOrder) -> None:
        self.sort_by.blockSignals(True)
        index = self.sort_by.findData(section)
        if index >= 0:
            self.sort_by.setCurrentIndex(index)
        self.sort_by.blockSignals(False)
        self.sort_order.blockSignals(True)
        self.sort_order.setCurrentIndex(
            self.sort_order.findData(order.value)
        )
        self.sort_order.blockSignals(False)


class NodeItem(QGraphicsRectItem):
    WIDTH = 230
    HEIGHT = 142

    def __init__(self, node_id: str, title: str, detail: str, color: str) -> None:
        super().__init__(0, 0, self.WIDTH, self.HEIGHT)
        self.node_id = node_id
        self.title = title
        self.detail = detail
        self.edges: list[EdgeItem] = []
        self.accent = QColor(color)
        self.setBrush(QBrush(QColor("#111c2e")))
        self.setPen(QPen(QColor("#334155"), 1.2))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0, 0, self.WIDTH, self.HEIGHT)
        painter.setBrush(QBrush(QColor("#111c2e")))
        border = QColor("#60a5fa") if self.isSelected() else QColor("#334155")
        painter.setPen(QPen(border, 2.0 if self.isSelected() else 1.2))
        painter.drawRoundedRect(body, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self.accent))
        painter.drawRoundedRect(QRectF(0, 0, 7, self.HEIGHT), 4, 4)
        painter.setPen(QPen(QColor("#263449"), 1))
        painter.drawLine(QPointF(14, 32), QPointF(self.WIDTH - 14, 32))
        painter.setPen(QColor("#f8fafc"))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        title = painter.fontMetrics().elidedText(
            self.title, Qt.TextElideMode.ElideRight, self.WIDTH - 32
        )
        painter.drawText(QRectF(16, 5, self.WIDTH - 32, 24),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        painter.setPen(QColor("#cbd5e1"))
        painter.setFont(QFont("Segoe UI", 9))
        detail_flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        painter.drawText(QRectF(16, 39, self.WIDTH - 32, self.HEIGHT - 46), detail_flags, self.detail)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.update_path()
        return result

    def anchor(self, other: "NodeItem") -> tuple[QPointF, QPointF]:
        source_right = other.sceneBoundingRect().center().x() >= self.sceneBoundingRect().center().x()
        source = self.sceneBoundingRect().center()
        target = other.sceneBoundingRect().center()
        return (
            QPointF(self.sceneBoundingRect().right() if source_right else self.sceneBoundingRect().left(), source.y()),
            QPointF(other.sceneBoundingRect().left() if source_right else other.sceneBoundingRect().right(), target.y()),
        )


class EdgeItem(QGraphicsPathItem):
    def __init__(self, source: NodeItem, target: NodeItem) -> None:
        super().__init__()
        self.source = source
        self.target = target
        self.setPen(QPen(QColor("#64748b"), 2.0))
        self.setBrush(QBrush(QColor("#94a3b8")))
        self.setZValue(-1)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        source.edges.append(self)
        target.edges.append(self)
        self.update_path()

    def update_path(self) -> None:
        start, end = self.source.anchor(self.target)
        bend = max(35.0, abs(end.x() - start.x()) * 0.45)
        path = QPainterPath(start)
        direction = 1.0 if end.x() >= start.x() else -1.0
        control_one = QPointF(start.x() + direction * bend, start.y())
        control_two = QPointF(end.x() - direction * bend, end.y())
        path.cubicTo(control_one, control_two, end)
        self.setPath(path)
        base_x = end.x() - direction * 11
        self.arrow = QPolygonF([
            end,
            QPointF(base_x, end.y() - 5),
            QPointF(base_x, end.y() + 5),
        ])

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        super().paint(painter, option, widget)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#94a3b8")))
        painter.drawPolygon(self.arrow)

    def boundingRect(self) -> QRectF:
        return super().boundingRect().adjusted(-12, -12, 12, 12)


class NodeGraphView(QGraphicsView):
    """Scrollable node canvas with cursor-centered wheel zoom."""

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumWidth(700)
        self._pan_anchor: QPoint | None = None
        self.pan_all = False

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if self.pan_all or item is None or isinstance(item, EdgeItem):
                self._pan_anchor = event.position().toPoint()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._pan_anchor is not None:
            current = event.position().toPoint()
            anchor_scene = self.mapToScene(self._pan_anchor)
            current_scene = self.mapToScene(current)
            center_scene = self.mapToScene(self.viewport().rect().center())
            self.centerOn(center_scene + anchor_scene - current_scene)
            self._pan_anchor = current
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pan_anchor is not None:
            self._pan_anchor = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom_by(factor)
        event.accept()

    def zoom_by(self, factor: float) -> None:
        current_zoom = self.transform().m11()
        if 0.35 <= current_zoom * factor <= 2.8:
            self.scale(factor, factor)

    def fit_graph(self) -> None:
        rect = self.scene().itemsBoundingRect().adjusted(-35, -35, 35, 35)
        if rect.isValid() and not rect.isEmpty():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)


class StrategyEditorWindow(QMainWindow):
    def __init__(self, startup_strategy: Path | None = None, startup_action: str = "edit") -> None:
        super().__init__()
        self.setWindowTitle("Credit Strategy Node Editor")
        self.resize(1360, 850)
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        self.strategy = Strategy(
            name="Untitled strategy", signals=[], combiners=[CombinerNode()],
            strategy_id=uuid.uuid4().hex,
        )
        self.node_positions: dict[str, QPointF] = {}
        self.pan_mode = False
        self.bond_list_fields: list[str] = []
        self.selected_node_id: str | None = None
        self.run_worker: StrategyRunWorker | None = None
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel(
            "Start with signal nodes, insert transformer nodes on any connection, and configure the combiner node. "
            "Each transformer processes the output of its input node."
        ))
        graph_toolbar = QHBoxLayout()
        for label, handler in (("Add signal", self._add_signal), ("Add transformer", self._append_transform),
                               ("Add combiner", self._add_combiner),
                               ("Remove selected", self._remove_selected), ("New graph", self._new_graph),
                               ("Open…", self._open), ("Validate", self._validate), ("Save as…", self._save)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            graph_toolbar.addWidget(button)
        graph_toolbar.addStretch(1)
        layout.addLayout(graph_toolbar)

        run_toolbar = QHBoxLayout()
        for label, handler in (("Calculate inputs…", self._calculate_inputs),
                               ("Run strategy", self._run_strategy),
                               ("Inspect strategy", self._inspect_strategy),
                               ("Export latest scores…", self._export_latest_scores)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            if label == "Export latest scores…":
                button.setToolTip(
                    "Export per-signal and combined scores for the latest cached date, ranked highest score first."
                )
            elif label == "Calculate inputs…":
                button.setToolTip(
                    "Choose the date range and TRACE dataset, then calculate and cache the strategy's signal inputs."
                )
            run_toolbar.addWidget(button)
            if label == "Calculate inputs…":
                self.run_inputs_button = button
            elif label == "Run strategy":
                self.run_strategy_button = button
        run_toolbar.addStretch(1)
        zoom_out = QPushButton("−")
        zoom_out.setToolTip("Zoom out")
        zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / 1.2))
        run_toolbar.addWidget(zoom_out)
        zoom_in = QPushButton("+")
        zoom_in.setToolTip("Zoom in")
        zoom_in.clicked.connect(lambda: self.view.zoom_by(1.2))
        run_toolbar.addWidget(zoom_in)
        fit_graph = QPushButton("Fit graph")
        fit_graph.clicked.connect(lambda: self.view.fit_graph())
        run_toolbar.addWidget(fit_graph)
        self.pan_button = QPushButton("Pan canvas")
        self.pan_button.setCheckable(True)
        self.pan_button.setToolTip(
            "Blank-space drags always pan. Turn this on to pan when dragging over a node; "
            "turn it off to move nodes directly."
        )
        self.pan_button.toggled.connect(self._set_pan_mode)
        run_toolbar.addWidget(self.pan_button)
        layout.addLayout(run_toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(QColor("#0b1220"))
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view = NodeGraphView(self.scene)
        splitter.addWidget(self.view)
        block_legend = QLabel(
            "BLOCK TYPES:  SIGNAL (blue)   →   TRANSFORMER (purple)   →   COMBINER (green)   →   OUTPUT (gray)"
        )
        block_legend.setStyleSheet("color: #cbd5e1; padding: 5px;")

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(block_legend)
        global_box = QGroupBox("Global strategy settings")
        global_form = QFormLayout(global_box)
        self.strategy_name = QLineEdit(self.strategy.name)
        self.strategy_name.editingFinished.connect(self._update_strategy_name)
        global_form.addRow("Strategy name", self.strategy_name)
        self.bond_list_path = QLineEdit(str(DEFAULT_SNAPSHOT))
        self.bond_list_path.setToolTip("CSV schema used to validate signal grouping fields.")
        bond_list_row = QHBoxLayout()
        bond_list_row.addWidget(self.bond_list_path, 1)
        browse_bond_list = QPushButton("Browse…")
        browse_bond_list.clicked.connect(self._browse_bond_list)
        bond_list_row.addWidget(browse_bond_list)
        global_form.addRow("Reference bond list", bond_list_row)
        self.output_node = QComboBox()
        self.output_node.currentIndexChanged.connect(self._update_output_node)
        global_form.addRow("Strategy output", self.output_node)
        global_form.addRow(QLabel("These settings describe the saved strategy as a whole."))
        panel_layout.addWidget(global_box)

        node_box = QGroupBox("Selected node settings")
        node_layout = QVBoxLayout(node_box)
        self.node_status = QLabel("Select a node to see and edit its settings.")
        self.node_status.setWordWrap(True)
        node_layout.addWidget(self.node_status)
        self.node_pages = QStackedWidget()
        empty_page = QLabel("Node-specific controls appear here once you select a block on the graph.")
        empty_page.setWordWrap(True)
        self.node_pages.addWidget(empty_page)

        # Signal component controls are shown only when a signal node is selected.
        signal_page = QWidget()
        signal_form = QFormLayout(signal_page)
        self.signal_id = QLineEdit()
        self.predictor = QComboBox()
        for label, value in PREDICTORS.items():
            self.predictor.addItem(label, value)
        self.predictor.currentIndexChanged.connect(self._signal_predictor_changed)
        self.signal_space = QComboBox()
        self.signal_space.addItems(["yield", "spread", "price"])
        self.group_field = QComboBox()
        self.group_field.setToolTip("Choose a column present in the reference bond-list CSV.")
        self.weight = QDoubleSpinBox()
        self.weight.setRange(-100, 100)
        self.weight.setDecimals(3)
        self.weight.setSingleStep(0.25)
        self.lookback_days = QSpinBox()
        self.lookback_days.setRange(30, 3650)
        self.lookback_days.setValue(1095)
        self.minimum_history = QSpinBox()
        self.minimum_history.setRange(5, 500)
        self.minimum_history.setValue(20)
        signal_form.addRow("Signal ID", self.signal_id)
        signal_form.addRow("Predictor", self.predictor)
        signal_form.addRow("Signal space", self.signal_space)
        signal_form.addRow("Group field", self.group_field)
        signal_form.addRow("Weight", self.weight)
        signal_form.addRow("History lookback (days)", self.lookback_days)
        signal_form.addRow("Minimum history points", self.minimum_history)
        self.node_pages.addWidget(signal_page)
        self.bond_list_path.textChanged.connect(self._refresh_bond_list_fields)
        self._refresh_bond_list_fields()

        # A transformer owns its method and parameters as an independent node.
        transformer_page = QWidget()
        transformer_form = QFormLayout(transformer_page)
        self.transform_name = QLineEdit()
        transformer_form.addRow("Node name", self.transform_name)
        self.transform_type = QComboBox()
        for label, value in (("Winsorize", "winsorize"), ("Z-score", "zscore"),
                             ("Percentile rank", "percentile"), ("Min-max", "minmax")):
            self.transform_type.addItem(label, value)
        self.transform_type.currentIndexChanged.connect(self._transform_type_changed)
        transformer_form.addRow("Transform", self.transform_type)
        self.winsor_lower = QDoubleSpinBox()
        self.winsor_lower.setRange(0.0, 0.99)
        self.winsor_lower.setDecimals(3)
        self.winsor_lower.setSingleStep(0.01)
        self.winsor_lower.setValue(0.02)
        transformer_form.addRow("Lower quantile (0–1)", self.winsor_lower)
        self.winsor_upper = QDoubleSpinBox()
        self.winsor_upper.setRange(0.01, 1.0)
        self.winsor_upper.setDecimals(3)
        self.winsor_upper.setSingleStep(0.01)
        self.winsor_upper.setValue(0.98)
        transformer_form.addRow("Upper quantile (0–1)", self.winsor_upper)
        transformer_form.addRow(QLabel("This transformer processes the value coming from its input connection."))
        self._transform_type_changed()
        self.node_pages.addWidget(transformer_page)

        # The method is a property of the combiner node, not global strategy state.
        combiner_page = QWidget()
        combiner_form = QFormLayout(combiner_page)
        self.combiner_name = QLineEdit()
        combiner_form.addRow("Node name", self.combiner_name)
        self.combination = QComboBox()
        for label, value in (("Weighted mean", "weighted_mean"), ("Equal-weight mean", "mean"),
                             ("Weighted sum", "sum"), ("Rank mean", "rank_mean")):
            self.combination.addItem(label, value)
        self.combination.setCurrentIndex(self.combination.findData(self.strategy.combiners[0].method))
        combiner_form.addRow("Method", self.combination)
        self.combiner_inputs = QListWidget()
        self.combiner_inputs.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.combiner_inputs.setMaximumHeight(180)
        combiner_form.addRow("Connected inputs", self.combiner_inputs)
        combiner_form.addRow(QLabel("Check each node that should feed this combiner. Connections update when you apply settings."))
        self.node_pages.addWidget(combiner_page)
        self.node_pages.setCurrentIndex(0)
        node_layout.addWidget(self.node_pages)
        apply_button = QPushButton("Apply node settings")
        apply_button.clicked.connect(self._apply_node_settings)
        self.apply_button = apply_button
        node_layout.addWidget(apply_button)
        panel_layout.addWidget(node_box, 1)
        splitter.addWidget(panel)
        splitter.setSizes([950, 400])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self._draw_graph()
        QTimer.singleShot(0, self.view.fit_graph)
        if startup_strategy is not None:
            QTimer.singleShot(
                0, lambda: self._load_strategy_file(startup_strategy, startup_action)
            )

    def _refresh_bond_list_fields(self, *_args: Any, selected_value: str | None = None) -> None:
        if not hasattr(self, "group_field"):
            return
        if selected_value is None and self.group_field.count():
            selected_value = self.group_field.currentData()
        try:
            with Path(self.bond_list_path.text()).expanduser().open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                self.bond_list_fields = list(csv.DictReader(handle).fieldnames or [])
        except OSError:
            self.bond_list_fields = []
        self.group_field.blockSignals(True)
        self.group_field.clear()
        self.group_field.addItem("Select a bond-list field…", None)
        for field_name in self.bond_list_fields:
            self.group_field.addItem(field_name, field_name)
        if selected_value and selected_value not in self.bond_list_fields:
            self.group_field.addItem(f"{selected_value} (not in reference list)", selected_value)
        self.group_field.setCurrentIndex(
            self.group_field.findData(selected_value) if selected_value else 0
        )
        self.group_field.blockSignals(False)

    def _browse_bond_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select reference bond list", self.bond_list_path.text(), "CSV files (*.csv)"
        )
        if path:
            self.bond_list_path.setText(path)

    def _graph_specs(self) -> tuple[list[tuple[str, str, str, str, QPointF]], list[tuple[str, str]]]:
        specs: list[tuple[str, str, str, str, QPointF]] = []
        edges: list[tuple[str, str]] = []
        signals = {f"signal:{node.signal_id}": node for node in self.strategy.signals}
        transformers = {node.node_id: node for node in self.strategy.transformer_nodes}
        combiners = {node.node_id: node for node in self.strategy.combiners}
        dependencies: dict[str, list[str]] = {
            node.node_id: [node.input_node_id] for node in self.strategy.transformer_nodes
        }
        dependencies.update({node.node_id: list(node.input_node_ids) for node in self.strategy.combiners})
        all_node_ids = [*signals, *transformers, *combiners]
        levels: dict[str, int] = {}

        def get_level(node_id: str, active: set[str] | None = None) -> int:
            if node_id in levels:
                return levels[node_id]
            if node_id in signals:
                levels[node_id] = 0
                return 0
            active = active or set()
            if node_id in active:
                return 0
            parents = dependencies.get(node_id, [])
            parent_levels = [get_level(parent, active | {node_id}) for parent in parents if parent in all_node_ids]
            levels[node_id] = 1 + max(parent_levels, default=-1)
            return levels[node_id]

        for node_id in all_node_ids:
            get_level(node_id)
        by_level: dict[int, list[str]] = {}
        for node_id in all_node_ids:
            by_level.setdefault(levels[node_id], []).append(node_id)
        combination_labels = {
            "weighted_mean": "Weighted mean",
            "mean": "Equal-weight mean",
            "sum": "Weighted sum",
            "rank_mean": "Mean of cross-sectional ranks",
        }
        combination_details = {
            "weighted_mean": "Missing scores dropped.\nAvailable weights are renormalized.",
            "mean": "Equal mean of available inputs.\nMissing scores are dropped.",
            "sum": "Weighted sum of available inputs.\nMissing scores contribute nothing.",
            "rank_mean": "Ranks each input cross-sectionally.\nThen averages available ranks.",
        }
        names = {node_id: node_id.removeprefix("signal:") for node_id in signals}
        names.update({node_id: node.name for node_id, node in transformers.items()})
        names.update({node_id: node.name for node_id, node in combiners.items()})
        for level, node_ids in by_level.items():
            for row, node_id in enumerate(node_ids):
                if node_id in signals:
                    component = signals[node_id]
                    predictor = PREDICTOR_LABELS.get(component.predictor_id, component.predictor_id)
                    group = component.group_field or "no group"
                    weight = component.weight
                    lookback = component.parameters.get("lookback_days")
                    if component.predictor_id == "nelson_siegel_residual":
                        description = f"Bond level vs. {group} curve.\nPositive score means cheap."
                    elif component.predictor_id == "historical_partition_percentile":
                        description = "Group median vs. own history.\nHigher percentile means cheaper."
                    else:
                        description = "Group median vs. trailing mean.\nHigher z-score means cheaper."
                    detail = f"{predictor} · {component.signal_space.title()}\nGroup: {group} · Weight: {weight:g}"
                    if lookback is not None:
                        detail += f"\nLookback: {lookback} days"
                    detail += f"\n{description}"
                    title, color = f"SIGNAL  /  {names[node_id]}", "#1d4ed8"
                elif node_id in transformers:
                    node = transformers[node_id]
                    detail = (
                        f"Input: {names.get(node.input_node_id, node.input_node_id)}\n"
                        f"{_transform_label(node.transform)}\n{_transform_description(node.transform)}"
                    )
                    title, color = f"TRANSFORMER  /  {node.name}", "#6d28d9"
                else:
                    node = combiners[node_id]
                    input_names = ", ".join(names.get(item, item) for item in node.input_node_ids)
                    detail = (
                        f"{combination_labels.get(node.method, node.method)} · {len(node.input_node_ids)} inputs\n"
                        f"{combination_details.get(node.method, '')}\nInputs: {input_names or 'none'}"
                    )
                    title, color = f"COMBINER  /  {node.name}", "#047857"
                default = QPointF(35 + level * 275, 35 + row * 175)
                specs.append((node_id, title, detail, color, self.node_positions.get(node_id, default)))
                edges.extend((parent, node_id) for parent in dependencies.get(node_id, []))
        output_level = max(levels.values(), default=0) + 1
        output_y = self.node_positions.get(self.strategy.output_node_id, QPointF(0, 35)).y()
        output_id = "strategy_output"
        specs.append((output_id, "OUTPUT",
                      f"Final score from: {names.get(self.strategy.output_node_id, self.strategy.output_node_id)}\nRanks bonds for portfolio selection.",
                      "#334155", self.node_positions.get(output_id, QPointF(35 + output_level * 275, output_y))))
        edges.append((self.strategy.output_node_id, output_id))
        return specs, edges

    def _draw_graph(self) -> None:
        selected_id = self.selected_node_id
        view_transform = self.view.transform()
        view_center = self.view.mapToScene(self.view.viewport().rect().center())
        for item in self.scene.items():
            if isinstance(item, NodeItem):
                self.node_positions[item.node_id] = item.pos()
        self.scene.clear()
        nodes: dict[str, NodeItem] = {}
        specs, edges = self._graph_specs()
        for node_id, title, detail, color, position in specs:
            self.node_positions[node_id] = position
            node = NodeItem(node_id, title, detail, color)
            node.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not self.pan_mode)
            node.setPos(position)
            self.scene.addItem(node)
            nodes[node_id] = node
        for source, target in edges:
            if source in nodes and target in nodes:
                edge = EdgeItem(nodes[source], nodes[target])
                self.scene.addItem(edge)
        if selected_id in nodes:
            nodes[selected_id].setSelected(True)
        else:
            self.selected_node_id = None
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        self.view.setTransform(view_transform)
        self.view.centerOn(view_center)
        self._refresh_output_choices()

    def _set_pan_mode(self, enabled: bool) -> None:
        self.pan_mode = enabled
        self.view.pan_all = enabled
        for item in self.scene.items():
            if isinstance(item, NodeItem):
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not enabled)
        self.pan_button.setText("Pan over nodes" if enabled else "Pan canvas")

    def _refresh_output_choices(self) -> None:
        if not hasattr(self, "output_node"):
            return
        node_names = {f"signal:{node.signal_id}": node.signal_id for node in self.strategy.signals}
        node_names.update({node.node_id: node.name for node in self.strategy.transformer_nodes})
        node_names.update({node.node_id: node.name for node in self.strategy.combiners})
        self.output_node.blockSignals(True)
        self.output_node.clear()
        for node_id, name in node_names.items():
            self.output_node.addItem(name, node_id)
        index = self.output_node.findData(self.strategy.output_node_id)
        if index < 0 and self.strategy.combiners:
            self.strategy.output_node_id = self.strategy.combiners[-1].node_id
            index = self.output_node.findData(self.strategy.output_node_id)
        self.output_node.setCurrentIndex(index)
        self.output_node.blockSignals(False)

    def _update_output_node(self, *_args: Any) -> None:
        node_id = self.output_node.currentData()
        if node_id:
            self.strategy.output_node_id = str(node_id)
            self._draw_graph()

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if not selected or not isinstance(selected[0], NodeItem):
            self.selected_node_id = None
            self.node_status.setText("Select a signal, transformer, or combiner node to edit it.")
            return
        node_id = selected[0].node_id
        self.selected_node_id = node_id
        self._load_node_settings(node_id)

    def _load_node_settings(self, node_id: str) -> None:
        self.apply_button.setEnabled(False)
        if node_id.startswith("signal:"):
            signal_id = node_id.split(":", 1)[1]
            component = next(item for item in self.strategy.signals if item.signal_id == signal_id)
            self.signal_id.setText(component.signal_id)
            self.predictor.setCurrentIndex(self.predictor.findData(component.predictor_id))
            self.signal_space.setCurrentText(component.signal_space)
            self._refresh_bond_list_fields(selected_value=component.group_field)
            if component.group_field is None:
                self.group_field.setCurrentIndex(0)
            self.weight.setValue(component.weight)
            is_partition = component.predictor_id in {
                "historical_partition_percentile", "historical_partition_zscore",
            }
            self.lookback_days.setVisible(is_partition)
            self.minimum_history.setVisible(is_partition)
            self.lookback_days.setValue(int(component.parameters.get("lookback_days", 1095)))
            self.minimum_history.setValue(int(component.parameters.get("minimum_history_observations", 20)))
            self.node_pages.setCurrentIndex(1)
            self.apply_button.setEnabled(True)
            self.node_status.setText(f"Signal source: {signal_id}. Add a separate transformer node to modify its scores.")
        elif node_id in {node.node_id for node in self.strategy.transformer_nodes}:
            node = next(item for item in self.strategy.transformer_nodes if item.node_id == node_id)
            self.transform_name.setText(node.name)
            self.transform_type.setCurrentIndex(self.transform_type.findData(str(node.transform.get("type", "zscore"))))
            self.winsor_lower.setValue(float(node.transform.get("lower", 0.02)))
            self.winsor_upper.setValue(float(node.transform.get("upper", 0.98)))
            self.node_pages.setCurrentIndex(2)
            self.apply_button.setEnabled(True)
            self.node_status.setText(f"Transformer node {node.name}; input: {node.input_node_id}.")
        elif node_id in {node.node_id for node in self.strategy.combiners}:
            node = next(item for item in self.strategy.combiners if item.node_id == node_id)
            self.combiner_name.setText(node.name)
            self.combination.setCurrentIndex(self.combination.findData(node.method))
            self.combiner_inputs.clear()
            input_names = {f"signal:{item.signal_id}": item.signal_id for item in self.strategy.signals}
            input_names.update({item.node_id: item.name for item in self.strategy.transformer_nodes})
            input_names.update({item.node_id: item.name for item in self.strategy.combiners})
            for input_id, name in input_names.items():
                if input_id == node_id:
                    continue
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, input_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if input_id in node.input_node_ids
                                   else Qt.CheckState.Unchecked)
                self.combiner_inputs.addItem(item)
            self.node_pages.setCurrentIndex(3)
            self.apply_button.setEnabled(True)
            self.node_status.setText(f"Combiner node {node.name}. Select one or more incoming graph nodes.")
        else:
            self.node_pages.setCurrentIndex(0)
            self.node_status.setText("Final combined strategy score.")

    def _transform_type_changed(self, *_args: Any) -> None:
        is_winsorize = hasattr(self, "transform_type") and self.transform_type.currentData() == "winsorize"
        if hasattr(self, "winsor_lower"):
            self.winsor_lower.setVisible(is_winsorize)
            self.winsor_upper.setVisible(is_winsorize)

    def _signal_predictor_changed(self, *_args: Any) -> None:
        is_partition = self.predictor.currentData() in {
            "historical_partition_percentile", "historical_partition_zscore",
        }
        self.lookback_days.setVisible(is_partition)
        self.minimum_history.setVisible(is_partition)

    def _apply_node_settings(self) -> bool:
        if not self.selected_node_id:
            return False
        try:
            if self.selected_node_id.startswith("signal:"):
                old_id = self.selected_node_id.split(":", 1)[1]
                component = next(item for item in self.strategy.signals if item.signal_id == old_id)
                new_id = self.signal_id.text().strip()
                if any(item.signal_id == new_id for item in self.strategy.signals if item is not component):
                    raise ValueError(f"Signal ID {new_id!r} is already in use")
                component.signal_id = new_id
                component.predictor_id = str(self.predictor.currentData())
                component.signal_space = self.signal_space.currentText()
                group_field = self.group_field.currentData()
                if group_field not in self.bond_list_fields:
                    raise ValueError("Choose a grouping field present in the reference bond list.")
                component.group_field = str(group_field)
                component.weight = self.weight.value()
                if component.predictor_id in {
                    "historical_partition_percentile", "historical_partition_zscore",
                }:
                    component.parameters.update({
                        "lookback_days": self.lookback_days.value(),
                        "minimum_history_observations": self.minimum_history.value(),
                    })
                if old_id != new_id:
                    old_node_id, new_node_id = f"signal:{old_id}", f"signal:{new_id}"
                    if old_node_id in self.node_positions:
                        self.node_positions[new_node_id] = self.node_positions.pop(old_node_id)
                    for node in self.strategy.transformer_nodes:
                        if node.input_node_id == old_node_id:
                            node.input_node_id = new_node_id
                    for combiner in self.strategy.combiners:
                        combiner.input_node_ids = [
                            new_node_id if item == old_node_id else item
                            for item in combiner.input_node_ids
                        ]
                    if self.strategy.output_node_id == old_node_id:
                        self.strategy.output_node_id = new_node_id
                self.selected_node_id = f"signal:{new_id}"
            elif self.selected_node_id in {node.node_id for node in self.strategy.transformer_nodes}:
                node = next(item for item in self.strategy.transformer_nodes if item.node_id == self.selected_node_id)
                node.name = self.transform_name.text().strip()
                kind = self.transform_type.currentData()
                node.transform = {"type": kind}
                if kind == "winsorize":
                    node.transform.update({"lower": self.winsor_lower.value(), "upper": self.winsor_upper.value()})
            elif self.selected_node_id in {node.node_id for node in self.strategy.combiners}:
                node = next(item for item in self.strategy.combiners if item.node_id == self.selected_node_id)
                node.name = self.combiner_name.text().strip()
                node.method = self.combination.currentData()
                node.input_node_ids = [
                    self.combiner_inputs.item(index).data(Qt.ItemDataRole.UserRole)
                    for index in range(self.combiner_inputs.count())
                    if self.combiner_inputs.item(index).checkState() == Qt.CheckState.Checked
                ]
            self.strategy.validate()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Invalid node settings", str(error))
            return False
        self._draw_graph()
        self.node_status.setText("Settings applied to the strategy graph.")
        return True

    def _append_transform(self) -> None:
        if not self.selected_node_id:
            self.node_status.setText("Select a signal, transformer, or combiner, then add a transformer node.")
            return
        if self.selected_node_id.startswith("signal:") or self.selected_node_id in {
            node.node_id for node in self.strategy.transformer_nodes
        }:
            if not self._apply_node_settings():
                return
        elif self.selected_node_id in {node.node_id for node in self.strategy.combiners}:
            if not self._apply_node_settings():
                return
        else:
            self.node_status.setText("Transformers can be inserted on signal branches or after the combiner.")
            return
        input_node_id = self.selected_node_id
        # New nodes use a simple default; select the transformer after insertion
        # to change its method or parameters.
        transform = {"type": "zscore"}
        index = 1
        existing = {node.node_id for node in self.strategy.transformer_nodes}
        while f"transform_{index}" in existing:
            index += 1
        new_id = f"transform_{index}"
        inserted = TransformerNode(new_id, input_node_id, transform, name=f"Transformer {index}")
        # Insert into the selected connection, preserving any downstream chain.
        for node in self.strategy.transformer_nodes:
            if node.input_node_id == input_node_id:
                node.input_node_id = new_id
        for combiner in self.strategy.combiners:
            combiner.input_node_ids = [
                new_id if item == input_node_id else item
                for item in combiner.input_node_ids
            ]
        if self.strategy.output_node_id == input_node_id:
            self.strategy.output_node_id = new_id
        self.strategy.transformer_nodes.append(inserted)
        self._draw_graph()
        self._select_node(new_id)

    def _select_node(self, node_id: str) -> None:
        self.selected_node_id = node_id
        self.scene.clearSelection()
        for item in self.scene.items():
            if isinstance(item, NodeItem) and item.node_id == node_id:
                item.setSelected(True)
                self.view.centerOn(item)
                break

    def _add_combiner(self) -> None:
        index = 1
        existing_ids = {node.node_id for node in self.strategy.combiners}
        while f"combiner_{index}" in existing_ids:
            index += 1
        new_id = f"combiner_{index}"
        # Leave wiring explicit: the user chooses the inputs on the new node.
        # If this is still a blank graph, replace its placeholder combiner.
        if not self.strategy.signals and len(self.strategy.combiners) == 1:
            placeholder = self.strategy.combiners[0]
            if not placeholder.input_node_ids:
                self.strategy.combiners.clear()
                self.strategy.output_node_id = new_id
        new_node = CombinerNode(node_id=new_id, name=f"Combiner {index}")
        self.strategy.combiners.append(new_node)
        self._draw_graph()
        self._select_node(new_id)

    def _new_graph(self) -> None:
        self.strategy = Strategy(name="Untitled strategy", signals=[], combiners=[CombinerNode()],
                                 strategy_id=uuid.uuid4().hex)
        self.strategy_name.setText(self.strategy.name)
        self.combination.setCurrentIndex(self.combination.findData(self.strategy.combiners[0].method))
        self.node_positions.clear()
        self.selected_node_id = None
        self._draw_graph()

    def _update_strategy_name(self) -> None:
        self.strategy.name = self.strategy_name.text().strip() or "Untitled strategy"
        self._draw_graph()

    def _add_signal(self) -> None:
        index = 1
        existing = {item.signal_id for item in self.strategy.signals}
        while f"signal_{index}" in existing:
            index += 1
        self.strategy.signals.append(SignalComponent(
            signal_id=f"signal_{index}", predictor_id="historical_partition_zscore",
            signal_space="spread", group_field="isin", weight=1.0,
            parameters={"lookback_days": 1095, "minimum_history_observations": 20},
        ))
        target_combiner = next(
            (node for node in self.strategy.combiners if node.node_id == self.selected_node_id),
            next((node for node in self.strategy.combiners if node.node_id == self.strategy.output_node_id),
                 self.strategy.combiners[-1]),
        )
        target_combiner.input_node_ids.append(f"signal:signal_{index}")
        self._draw_graph()
        self._select_node(f"signal:signal_{index}")

    def _remove_selected(self) -> None:
        if not self.selected_node_id:
            return
        if self.selected_node_id.startswith("signal:"):
            removed_nodes = {self.selected_node_id}
            changed = True
            while changed:
                changed = False
                for node in self.strategy.transformer_nodes:
                    if node.input_node_id in removed_nodes and node.node_id not in removed_nodes:
                        removed_nodes.add(node.node_id)
                        changed = True
            signal_id = self.selected_node_id.split(":", 1)[1]
            self.strategy.signals = [item for item in self.strategy.signals if item.signal_id != signal_id]
            self.strategy.transformer_nodes = [node for node in self.strategy.transformer_nodes
                                                if node.node_id not in removed_nodes]
            for combiner in self.strategy.combiners:
                combiner.input_node_ids = [node_id for node_id in combiner.input_node_ids
                                           if node_id not in removed_nodes]
        elif self.selected_node_id in {node.node_id for node in self.strategy.transformer_nodes}:
            node = next(item for item in self.strategy.transformer_nodes if item.node_id == self.selected_node_id)
            input_node_id = node.input_node_id
            self.strategy.transformer_nodes = [item for item in self.strategy.transformer_nodes
                                                if item.node_id != self.selected_node_id]
            for item in self.strategy.transformer_nodes:
                if item.input_node_id == self.selected_node_id:
                    item.input_node_id = input_node_id
            for combiner in self.strategy.combiners:
                combiner.input_node_ids = [
                    input_node_id if item == self.selected_node_id else item
                    for item in combiner.input_node_ids
                ]
            if self.strategy.output_node_id == self.selected_node_id:
                self.strategy.output_node_id = input_node_id
        elif self.selected_node_id in {node.node_id for node in self.strategy.combiners}:
            if len(self.strategy.combiners) == 1:
                self.node_status.setText("A strategy needs at least one combiner node.")
                return
            node = next(item for item in self.strategy.combiners if item.node_id == self.selected_node_id)
            inputs = list(node.input_node_ids)
            if not inputs:
                self.node_status.setText("Connect at least one input before removing this combiner.")
                return
            downstream_transformers = [item for item in self.strategy.transformer_nodes
                                       if item.input_node_id == node.node_id]
            if len(inputs) > 1 and downstream_transformers:
                self.node_status.setText("Remove downstream transformers first; they accept one input connection.")
                return
            for transformer in downstream_transformers:
                transformer.input_node_id = inputs[0]
            for combiner in self.strategy.combiners:
                if combiner.node_id == node.node_id:
                    continue
                rewired: list[str] = []
                for input_id in combiner.input_node_ids:
                    rewired.extend(inputs if input_id == node.node_id else [input_id])
                combiner.input_node_ids = list(dict.fromkeys(rewired))
            if self.strategy.output_node_id == node.node_id:
                self.strategy.output_node_id = inputs[-1]
            self.strategy.combiners = [item for item in self.strategy.combiners
                                        if item.node_id != node.node_id]
        else:
            return
        self.selected_node_id = None
        self._draw_graph()

    def _validate(self) -> None:
        try:
            self.strategy.name = self.strategy_name.text().strip()
            self.strategy.validate()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Strategy invalid", str(error))
            return
        QMessageBox.information(self, "Strategy valid", f"{self.strategy.name}: {len(self.strategy.signals)} signal branches.")

    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open strategy", str(STRATEGIES_DIR), "Strategy JSON (*.json)")
        if not path:
            return
        self._load_strategy_file(Path(path))

    def _load_strategy_file(self, path: Path, action: str = "edit") -> None:
        try:
            strategy = Strategy.load(path)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not open strategy", str(error))
            return
        self.strategy = strategy
        if not self.strategy.strategy_id:
            self.strategy.strategy_id = uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve())).hex
        self.strategy_name.setText(strategy.name)
        self.combination.blockSignals(True)
        self.combination.setCurrentIndex(self.combination.findData(strategy.combiners[0].method))
        self.combination.blockSignals(False)
        saved_positions = strategy.metadata.get("editor_node_positions", {})
        self.node_positions = {
            str(node_id): QPointF(float(position[0]), float(position[1]))
            for node_id, position in saved_positions.items()
            if isinstance(position, (list, tuple)) and len(position) == 2
        }
        self.selected_node_id = None
        self._draw_graph()
        if action == "calculate":
            QTimer.singleShot(0, self._calculate_inputs)
        elif action == "run":
            QTimer.singleShot(0, self._run_strategy)

    def _save(self) -> None:
        # Include any edits still present in the node controls before writing.
        if self.apply_button.isEnabled() and not self._apply_node_settings():
            return
        self.strategy.name = self.strategy_name.text().strip()
        for item in self.scene.items():
            if isinstance(item, NodeItem):
                self.node_positions[item.node_id] = item.pos()
        self.strategy.metadata["editor_node_positions"] = {
            node_id: [round(position.x(), 2), round(position.y(), 2)]
            for node_id, position in self.node_positions.items()
        }
        try:
            self.strategy.validate()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Strategy invalid", str(error))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save strategy", str(STRATEGIES_DIR / f"{self.strategy.strategy_id or 'strategy'}.json"),
            "Strategy JSON (*.json)",
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".json":
            path = f"{path}.json"
        try:
            self.strategy.save(Path(path))
        except OSError as error:
            QMessageBox.warning(self, "Could not save strategy", str(error))
            return
        QMessageBox.information(self, "Strategy saved", f"Saved {self.strategy.name}.")

    def _strategy_cache_path(self) -> Path:
        return STRATEGY_RUNS_DIR / f"{self.strategy.strategy_id or 'unsaved'}.inputs.json"

    def _calculate_inputs(self) -> None:
        if self.run_worker is not None and self.run_worker.isRunning():
            return
        if self.apply_button.isEnabled() and not self._apply_node_settings():
            return
        self.strategy.name = self.strategy_name.text().strip()
        try:
            self.strategy.validate()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Strategy invalid", str(error))
            return
        dialog = StrategyInputsDialog(self, self.bond_list_path.text())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            parameters = dialog.evaluation_parameters()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid input settings", str(error))
            return
        self._run_progress = QProgressDialog("Calculating signal inputs…", "", 0,
                                             len(self.strategy.signals) * 100, self)
        self._run_progress.setWindowTitle("Strategy inputs")
        self._run_progress.setCancelButton(None)
        self._run_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._run_progress.setMinimumDuration(0)
        self._run_progress.setValue(0)
        self.run_inputs_button.setEnabled(False)
        self.run_strategy_button.setEnabled(False)
        self.run_worker = StrategyRunWorker(self.strategy, parameters, self._strategy_cache_path())
        self.run_worker.progress.connect(self._run_progress.setValue)
        self.run_worker.succeeded.connect(self._inputs_calculated)
        self.run_worker.failed.connect(self._inputs_failed)
        self._run_progress.show()
        self.run_worker.start()

    def _inputs_calculated(self, cache_path: str) -> None:
        self._run_progress.close()
        self.run_inputs_button.setEnabled(True)
        self.run_strategy_button.setEnabled(True)
        QMessageBox.information(
            self, "Inputs calculated", f"Cached signal outputs for {len(self.strategy.signals)} signals.\n{cache_path}"
        )

    def _inputs_failed(self, message: str) -> None:
        self._run_progress.close()
        self.run_inputs_button.setEnabled(True)
        self.run_strategy_button.setEnabled(True)
        QMessageBox.critical(self, "Input calculation failed", message)

    def _run_strategy(self) -> None:
        if self.apply_button.isEnabled() and not self._apply_node_settings():
            return
        self.strategy.name = self.strategy_name.text().strip()
        try:
            self.strategy.validate()
            rows = recombine_cached_signals(self.strategy, self._strategy_cache_path())
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self, "Strategy inputs unavailable",
                f"{error}\n\nUse Calculate inputs to create or refresh the signal cache.",
            )
            return
        result_path = STRATEGY_RUNS_DIR / f"{self.strategy.strategy_id}.results.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({
            "strategy_id": self.strategy.strategy_id,
            "strategy_name": self.strategy.name,
            "strategy": self.strategy.to_dict(),
            "rows": rows,
        }, indent=2) + "\n", encoding="utf-8")
        QMessageBox.information(
            self, "Strategy run complete",
            f"Combined {len(rows):,} dated bond scores.\nSaved results to {result_path}",
        )

    def _inspect_strategy(self) -> None:
        if self.apply_button.isEnabled() and not self._apply_node_settings():
            return
        self.strategy.name = self.strategy_name.text().strip()
        try:
            self.strategy.validate()
            rows = recombine_cached_signals(self.strategy, self._strategy_cache_path())
            cache = json.loads(self._strategy_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self, "Strategy inputs unavailable",
                f"{error}\n\nUse Calculate inputs to create or refresh the signal cache.",
            )
            return
        bond_list_path = cache.get("evaluation_parameters", {}).get("bond_list_path")
        details: dict[str, dict[str, str]] = {}
        if bond_list_path and Path(bond_list_path).is_file():
            with Path(bond_list_path).open("r", encoding="utf-8-sig", newline="") as handle:
                for bond in csv.DictReader(handle):
                    if bond.get("isin"):
                        details[str(bond["isin"])] = bond
        inspector = StrategyInspectorDialog(self.strategy, rows, details, self)
        inspector.exec()

    def _export_latest_scores(self) -> None:
        if self.apply_button.isEnabled() and not self._apply_node_settings():
            return
        self.strategy.name = self.strategy_name.text().strip()
        try:
            self.strategy.validate()
            rows = recombine_cached_signals(self.strategy, self._strategy_cache_path())
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self, "Strategy inputs unavailable",
                f"{error}\n\nUse Calculate inputs to create or refresh the signal cache.",
            )
            return
        if not rows:
            QMessageBox.information(self, "No signal scores", "The cache contains no dated bond scores.")
            return
        latest_date = max(str(row["as_of_date"]) for row in rows)
        latest_rows = [row for row in rows if str(row["as_of_date"]) == latest_date]
        latest_rows.sort(
            key=lambda row: (
                row.get("strategy_score") is None,
                -(float(row["strategy_score"]) if row.get("strategy_score") is not None else 0.0),
                str(row["isin"]),
            )
        )
        cache = json.loads(self._strategy_cache_path().read_text(encoding="utf-8"))
        evaluation_parameters = cache.get("evaluation_parameters", {})
        bond_list_path = evaluation_parameters.get("bond_list_path")
        bond_details: dict[str, dict[str, str]] = {}
        if bond_list_path and Path(bond_list_path).is_file():
            with Path(bond_list_path).open("r", encoding="utf-8-sig", newline="") as handle:
                for bond in csv.DictReader(handle):
                    isin = bond.get("isin")
                    if isin:
                        bond_details[str(isin)] = bond
        metadata_aliases = {
            "ticker": ("equity_ticker", "ticker", "issuer_ticker", "bloomberg_ticker"),
            "coupon": ("coupon", "coupon_pct", "coupon_rate", "coupon_rate_pct", "coupon_percent"),
            "maturity_date": ("maturity_date", "maturity"),
            "sector": ("sector", "industry_sector", "gics_sector", "industry"),
            "issuer": ("issuer", "issuer_name", "company_name"),
            "rating": ("rating", "composite_rating", "credit_rating"),
        }
        metadata_sources = {
            output_field: next((candidate for candidate in candidates
                                if any(candidate in bond for bond in bond_details.values())), None)
            for output_field, candidates in metadata_aliases.items()
        }
        details = [field for field, source in metadata_sources.items() if source is not None]
        signal_fields = [f"signal:{signal.signal_id}" for signal in self.strategy.signals]
        node_names = {f"signal:{signal.signal_id}": signal.signal_id for signal in self.strategy.signals}
        node_names.update({node.node_id: node.name for node in self.strategy.transformer_nodes})
        node_names.update({node.node_id: node.name for node in self.strategy.combiners})
        pre_combiner_fields = {
            f"pre_combiner:{combiner.name}:{node_names.get(input_id, input_id)}": f"node:{input_id}"
            for combiner in self.strategy.combiners
            for input_id in combiner.input_node_ids
        }
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Export latest strategy scores",
            str(STRATEGY_RUNS_DIR / f"{self.strategy.strategy_id}_latest_scores_{latest_date}.csv"),
            "CSV files (*.csv)",
        )
        if not output_path:
            return
        fieldnames = [
            "as_of_date", "rank", "isin", *details, "strategy_score",
            *signal_fields, *pre_combiner_fields,
        ]
        with Path(output_path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            rank = 0
            for row in latest_rows:
                score = row.get("strategy_score")
                if score is not None and math.isfinite(float(score)):
                    rank += 1
                detail = bond_details.get(str(row["isin"]), {})
                normalized_details = {
                    field: detail.get(source, "")
                    for field, source in metadata_sources.items() if source is not None
                }
                writer.writerow({
                    **row,
                    "rank": rank if score is not None else "",
                    "strategy_score": score,
                    **normalized_details,
                    **{field: row.get(node_field) for field, node_field in pre_combiner_fields.items()},
                })
        QMessageBox.information(
            self, "Latest scores exported",
            f"Exported {len(latest_rows):,} bond scores for {latest_date}, ordered by strategy score.\n{output_path}",
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build and run signal strategies")
    parser.add_argument("--strategy", type=Path, help="Open a saved strategy file on launch")
    parser.add_argument("--action", choices=("edit", "calculate", "run"), default="edit")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    window = StrategyEditorWindow(args.strategy, args.action)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
