"""Visual node editor for reusable multi-signal strategy JSON files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsTextItem, QGraphicsView, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSplitter, QTextEdit, QVBoxLayout,
    QWidget,
)

from .strategy import Strategy, SignalComponent, SUPPORTED_COMBINATIONS, SUPPORTED_TRANSFORMS


EXAMPLE_STRATEGY: dict[str, Any] = {
    "schema_version": 1,
    "strategy_id": "multi_space_credit_research",
    "name": "Multi-space credit research",
    "combination_method": "weighted_mean",
    "signals": [
        {"signal_id": "ticker_spread_ns", "predictor_id": "nelson_siegel_residual", "signal_space": "spread", "group_field": "equity_ticker", "weight": 1.0, "parameters": {}, "transformers": [{"type": "winsorize", "lower": 0.02, "upper": 0.98}, {"type": "zscore"}]},
        {"signal_id": "rating_yield_ns", "predictor_id": "nelson_siegel_residual", "signal_space": "yield", "group_field": "rating", "weight": 1.0, "parameters": {}, "transformers": [{"type": "zscore"}]},
        {"signal_id": "ticker_price_ns", "predictor_id": "nelson_siegel_residual", "signal_space": "price", "group_field": "equity_ticker", "weight": 0.5, "parameters": {}, "transformers": [{"type": "zscore"}]},
        {"signal_id": "isin_spread_z", "predictor_id": "historical_partition_zscore", "signal_space": "spread", "group_field": "isin", "weight": 1.0, "parameters": {"lookback_days": 1095, "minimum_history_observations": 20}, "transformers": [{"type": "winsorize", "lower": 0.02, "upper": 0.98}, {"type": "zscore"}]},
        {"signal_id": "isin_yield_z", "predictor_id": "historical_partition_zscore", "signal_space": "yield", "group_field": "isin", "weight": 1.0, "parameters": {"lookback_days": 1095, "minimum_history_observations": 20}, "transformers": [{"type": "winsorize", "lower": 0.02, "upper": 0.98}, {"type": "zscore"}]},
    ],
    "transformers": [{"type": "winsorize", "lower": 0.02, "upper": 0.98}, {"type": "zscore"}],
    "metadata": {"description": "Editable starting point; validate group fields against the selected bond list."},
}

PREDICTORS = {
    "Nelson-Siegel residual": "nelson_siegel_residual",
    "Historical percentile": "historical_partition_percentile",
    "Historical z-score": "historical_partition_zscore",
}


class NodeItem(QGraphicsRectItem):
    WIDTH = 190
    HEIGHT = 88

    def __init__(self, node_id: str, title: str, detail: str, color: str) -> None:
        super().__init__(0, 0, self.WIDTH, self.HEIGHT)
        self.node_id = node_id
        self.edges: list[EdgeItem] = []
        self.setBrush(QBrush(QColor(color)))
        self.setPen(QPen(QColor("#94a3b8"), 1.4))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        title_item = QGraphicsTextItem(title, self)
        title_item.setDefaultTextColor(QColor("#f8fafc"))
        title_item.setTextWidth(self.WIDTH - 20)
        title_item.setPos(10, 8)
        detail_item = QGraphicsTextItem(detail, self)
        detail_item.setDefaultTextColor(QColor("#cbd5e1"))
        detail_item.setTextWidth(self.WIDTH - 20)
        detail_item.setPos(10, 35)

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
        self.setZValue(-1)
        source.edges.append(self)
        target.edges.append(self)
        self.update_path()

    def update_path(self) -> None:
        start, end = self.source.anchor(self.target)
        bend = max(35.0, abs(end.x() - start.x()) * 0.45)
        path = QPainterPath(start)
        path.cubicTo(QPointF(start.x() + bend, start.y()), QPointF(end.x() - bend, end.y()), end)
        self.setPath(path)


class StrategyEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Credit Strategy Node Editor")
        self.resize(1360, 850)
        self.strategy = Strategy.from_dict(EXAMPLE_STRATEGY)
        self.node_positions: dict[str, QPointF] = {}
        self.selected_node_id: str | None = None
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel(
            "Build a daily signal graph. Drag nodes to arrange it; select a node to edit its settings. "
            "Signal transforms run before combination; final transforms run after it."
        ))
        toolbar = QHBoxLayout()
        for label, handler in (("Add signal", self._add_signal), ("Remove selected", self._remove_selected),
                               ("Open…", self._open), ("Validate", self._validate), ("Save as…", self._save)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(QColor("#0b1220"))
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.view.setMinimumWidth(700)
        splitter.addWidget(self.view)

        panel = QWidget()
        form = QFormLayout(panel)
        self.strategy_name = QLineEdit(self.strategy.name)
        self.strategy_name.editingFinished.connect(self._update_strategy_name)
        form.addRow("Strategy name", self.strategy_name)
        self.combination = QComboBox()
        self.combination.addItems(sorted(SUPPORTED_COMBINATIONS))
        self.combination.setCurrentText(self.strategy.combination_method)
        self.combination.currentTextChanged.connect(self._update_combination)
        form.addRow("Combination", self.combination)

        self.signal_id = QLineEdit()
        form.addRow("Signal ID", self.signal_id)
        self.predictor = QComboBox()
        for label, value in PREDICTORS.items():
            self.predictor.addItem(label, value)
        form.addRow("Predictor", self.predictor)
        self.signal_space = QComboBox()
        self.signal_space.addItems(["yield", "spread", "price"])
        form.addRow("Signal space", self.signal_space)
        self.group_field = QLineEdit()
        form.addRow("Group field", self.group_field)
        self.weight = QDoubleSpinBox()
        self.weight.setRange(-100, 100)
        self.weight.setDecimals(3)
        self.weight.setSingleStep(0.25)
        form.addRow("Weight", self.weight)
        self.transformers = QTextEdit()
        self.transformers.setPlaceholderText('[{"type": "winsorize", "lower": 0.02, "upper": 0.98}]')
        self.transformers.setMaximumHeight(145)
        form.addRow("Selected node transforms", self.transformers)
        apply_button = QPushButton("Apply node settings")
        apply_button.clicked.connect(self._apply_node_settings)
        form.addRow(apply_button)
        self.node_status = QLabel("Select a signal or transform node to edit it.")
        self.node_status.setWordWrap(True)
        form.addRow(self.node_status)
        self.transform_type = QComboBox()
        self.transform_type.addItems(sorted(SUPPORTED_TRANSFORMS))
        add_transform = QPushButton("Append transform to selected node")
        add_transform.clicked.connect(self._append_transform)
        form.addRow("Add transform", self.transform_type)
        form.addRow(add_transform)
        form.addRow(QLabel(
            "Transform settings are JSON so winsor limits and other parameters stay explicit. "
            "Use [] to clear the selected node's transforms."
        ))
        splitter.addWidget(panel)
        splitter.setSizes([950, 400])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self._draw_graph()

    def _graph_specs(self) -> tuple[list[tuple[str, str, str, str, QPointF]], list[tuple[str, str]]]:
        specs: list[tuple[str, str, str, str, QPointF]] = []
        edges: list[tuple[str, str]] = []
        max_branch_width = 1
        branches: list[list[str]] = []
        for row, component in enumerate(self.strategy.signals):
            ids = [f"signal:{component.signal_id}"]
            ids.extend(f"signal_transform:{component.signal_id}:{index}" for index in range(len(component.transformers)))
            branches.append(ids)
            max_branch_width = max(max_branch_width, len(ids))
            for column, node_id in enumerate(ids):
                if column == 0:
                    title = component.signal_id
                    detail = f"{component.predictor_id} · {component.group_field or 'no group'} · {component.signal_space}"
                    color = "#1d4ed8"
                else:
                    transform = component.transformers[column - 1]
                    title = f"Signal transform {column}"
                    detail = str(transform.get("type", "transform"))
                    color = "#6d28d9"
                default = QPointF(35 + column * 235, 35 + row * 145)
                specs.append((node_id, title, detail, color, self.node_positions.get(node_id, default)))
                if column:
                    edges.append((ids[column - 1], node_id))
        combine_id = "combine"
        combine_x = 35 + max_branch_width * 235
        combine_y = 35 + max(0, len(branches) - 1) * 72
        specs.append((combine_id, "Combine signals", self.strategy.combination_method, "#047857",
                      self.node_positions.get(combine_id, QPointF(combine_x, combine_y))))
        edges.extend((branch[-1], combine_id) for branch in branches)
        previous = combine_id
        final_x = combine_x + 235
        for index, transform in enumerate(self.strategy.transformers):
            node_id = f"final_transform:{index}"
            specs.append((node_id, f"Final transform {index + 1}", str(transform.get("type", "transform")),
                          "#a16207", self.node_positions.get(node_id, QPointF(final_x + index * 235, combine_y))))
            edges.append((previous, node_id))
            previous = node_id
        output_id = "strategy_output"
        specs.append((output_id, "Strategy score", "Combined daily bond score", "#334155",
                      self.node_positions.get(output_id, QPointF(final_x + len(self.strategy.transformers) * 235, combine_y))))
        edges.append((previous, output_id))
        return specs, edges

    def _draw_graph(self) -> None:
        for item in self.scene.items():
            if isinstance(item, NodeItem):
                self.node_positions[item.node_id] = item.pos()
        self.scene.clear()
        nodes: dict[str, NodeItem] = {}
        specs, edges = self._graph_specs()
        for node_id, title, detail, color, position in specs:
            self.node_positions[node_id] = position
            node = NodeItem(node_id, title, detail, color)
            node.setPos(position)
            self.scene.addItem(node)
            nodes[node_id] = node
        for source, target in edges:
            if source in nodes and target in nodes:
                edge = EdgeItem(nodes[source], nodes[target])
                self.scene.addItem(edge)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if not selected or not isinstance(selected[0], NodeItem):
            self.selected_node_id = None
            self.node_status.setText("Select a signal or transform node to edit it.")
            return
        node_id = selected[0].node_id
        self.selected_node_id = node_id
        self._load_node_settings(node_id)

    def _load_node_settings(self, node_id: str) -> None:
        self.signal_id.setEnabled(False)
        self.predictor.setEnabled(False)
        self.signal_space.setEnabled(False)
        self.group_field.setEnabled(False)
        self.weight.setEnabled(False)
        self.transformers.setEnabled(False)
        self.transform_type.setEnabled(False)
        signal_id = None
        transforms: list[dict[str, Any]] = []
        if node_id.startswith("signal:"):
            signal_id = node_id.split(":", 1)[1]
            component = next(item for item in self.strategy.signals if item.signal_id == signal_id)
            self.signal_id.setText(component.signal_id)
            self.predictor.setCurrentIndex(self.predictor.findData(component.predictor_id))
            self.signal_space.setCurrentText(component.signal_space)
            self.group_field.setText(component.group_field or "")
            self.weight.setValue(component.weight)
            transforms = component.transformers
            self.signal_id.setEnabled(True)
            self.predictor.setEnabled(True)
            self.signal_space.setEnabled(True)
            self.group_field.setEnabled(True)
            self.weight.setEnabled(True)
            self.transformers.setEnabled(True)
            self.transform_type.setEnabled(True)
            self.node_status.setText(f"Signal source: {signal_id}")
        elif node_id.startswith("signal_transform:"):
            _, signal_id, _index = node_id.split(":", 2)
            component = next(item for item in self.strategy.signals if item.signal_id == signal_id)
            transforms = component.transformers
            self.transformers.setEnabled(True)
            self.transform_type.setEnabled(True)
            self.node_status.setText(f"Edit the ordered transform list for signal {signal_id}.")
        elif node_id.startswith("final_transform:"):
            transforms = self.strategy.transformers
            self.transformers.setEnabled(True)
            self.transform_type.setEnabled(True)
            self.node_status.setText("Edit the ordered transforms applied after signal combination.")
        elif node_id == "combine":
            self.node_status.setText("Choose how available signal scores are combined. Missing inputs are omitted.")
        else:
            self.node_status.setText("Final combined strategy score.")
        self.transformers.setPlainText(json.dumps(transforms, indent=2))

    def _apply_node_settings(self) -> bool:
        if not self.selected_node_id:
            return False
        try:
            transforms = json.loads(self.transformers.toPlainText() or "[]")
            if not isinstance(transforms, list):
                raise ValueError("Transform list must be a JSON array")
            if self.selected_node_id.startswith("signal:"):
                old_id = self.selected_node_id.split(":", 1)[1]
                component = next(item for item in self.strategy.signals if item.signal_id == old_id)
                new_id = self.signal_id.text().strip()
                if any(item.signal_id == new_id for item in self.strategy.signals if item is not component):
                    raise ValueError(f"Signal ID {new_id!r} is already in use")
                component.signal_id = new_id
                component.predictor_id = str(self.predictor.currentData())
                component.signal_space = self.signal_space.currentText()
                component.group_field = self.group_field.text().strip() or None
                component.weight = self.weight.value()
                component.transformers = transforms
                self.selected_node_id = f"signal:{new_id}"
            elif self.selected_node_id.startswith("signal_transform:"):
                _, signal_id, _index = self.selected_node_id.split(":", 2)
                next(item for item in self.strategy.signals if item.signal_id == signal_id).transformers = transforms
            elif self.selected_node_id.startswith("final_transform:"):
                self.strategy.transformers = transforms
            self.strategy.validate()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Invalid node settings", str(error))
            return False
        self._draw_graph()
        self.node_status.setText("Settings applied to the strategy graph.")
        return True

    def _append_transform(self) -> None:
        if not self.selected_node_id:
            return
        if self.selected_node_id.startswith("signal:"):
            if not self._apply_node_settings():
                return
            signal_id = self.selected_node_id.split(":", 1)[1]
            transforms = next(item for item in self.strategy.signals if item.signal_id == signal_id).transformers
        elif self.selected_node_id.startswith("signal_transform:"):
            _, signal_id, _index = self.selected_node_id.split(":", 2)
            transforms = next(item for item in self.strategy.signals if item.signal_id == signal_id).transformers
        elif self.selected_node_id.startswith("final_transform:"):
            transforms = self.strategy.transformers
        else:
            return
        new_transform: dict[str, Any] = {"type": self.transform_type.currentText()}
        if new_transform["type"] == "winsorize":
            new_transform.update({"lower": 0.02, "upper": 0.98})
        transforms.append(new_transform)
        self._draw_graph()

    def _update_strategy_name(self) -> None:
        self.strategy.name = self.strategy_name.text().strip() or "Untitled strategy"
        self._draw_graph()

    def _update_combination(self, value: str) -> None:
        self.strategy.combination_method = value
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
        self._draw_graph()

    def _remove_selected(self) -> None:
        if not self.selected_node_id:
            return
        if self.selected_node_id.startswith("signal:"):
            signal_id = self.selected_node_id.split(":", 1)[1]
            self.strategy.signals = [item for item in self.strategy.signals if item.signal_id != signal_id]
        elif self.selected_node_id.startswith("signal_transform:"):
            _, signal_id, index = self.selected_node_id.split(":", 2)
            component = next(item for item in self.strategy.signals if item.signal_id == signal_id)
            if int(index) < len(component.transformers):
                component.transformers.pop(int(index))
        elif self.selected_node_id.startswith("final_transform:"):
            index = int(self.selected_node_id.split(":", 1)[1])
            if index < len(self.strategy.transformers):
                self.strategy.transformers.pop(index)
        else:
            return
        if not self.strategy.signals:
            self.strategy.signals.append(SignalComponent(
                signal_id="signal_1", predictor_id="historical_partition_zscore",
                signal_space="spread", group_field="isin",
            ))
        self.selected_node_id = None
        self._draw_graph()

    def _validate(self) -> None:
        try:
            self.strategy.name = self.strategy_name.text().strip()
            self.strategy.combination_method = self.combination.currentText()
            self.strategy.validate()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Strategy invalid", str(error))
            return
        QMessageBox.information(self, "Strategy valid", f"{self.strategy.name}: {len(self.strategy.signals)} signal branches.")

    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open strategy", "", "Strategy JSON (*.json)")
        if not path:
            return
        try:
            strategy = Strategy.load(Path(path))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            QMessageBox.warning(self, "Could not open strategy", str(error))
            return
        self.strategy = strategy
        self.strategy_name.setText(strategy.name)
        self.combination.setCurrentText(strategy.combination_method)
        self.node_positions.clear()
        self.selected_node_id = None
        self._draw_graph()

    def _save(self) -> None:
        self.strategy.name = self.strategy_name.text().strip()
        self.strategy.combination_method = self.combination.currentText()
        try:
            self.strategy.validate()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Strategy invalid", str(error))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save strategy", f"{self.strategy.strategy_id or 'strategy'}.json", "Strategy JSON (*.json)")
        if not path:
            return
        try:
            self.strategy.save(Path(path))
        except OSError as error:
            QMessageBox.warning(self, "Could not save strategy", str(error))
            return
        QMessageBox.information(self, "Strategy saved", f"Saved {self.strategy.name}.")


def main() -> None:
    app = QApplication(sys.argv)
    window = StrategyEditorWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
