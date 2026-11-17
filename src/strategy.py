"""Editable multi-signal strategy definitions and score transformations."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


STRATEGY_SCHEMA_VERSION = 3
SUPPORTED_COMBINATIONS = {"mean", "weighted_mean", "sum", "rank_mean"}
SUPPORTED_TRANSFORMS = {"winsorize", "zscore", "percentile", "minmax"}


@dataclass
class SignalComponent:
    """One independently configured signal in a strategy."""

    signal_id: str
    predictor_id: str
    signal_space: str
    group_field: str | None = None
    weight: float = 1.0
    parameters: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.signal_id.strip():
            raise ValueError("Every signal needs a non-empty signal_id")
        if self.predictor_id not in {
            "nelson_siegel_residual",
            "historical_partition_percentile",
            "historical_partition_zscore",
        }:
            raise ValueError(f"Unsupported predictor_id: {self.predictor_id}")
        if self.signal_space not in {"yield", "spread", "price"}:
            raise ValueError(f"Unsupported signal_space: {self.signal_space}")
        if not math.isfinite(self.weight):
            raise ValueError(f"Signal {self.signal_id} weight must be finite")


@dataclass
class TransformerNode:
    """One independently configurable transformation node in the graph."""

    node_id: str
    input_node_id: str
    transform: dict[str, Any]
    name: str = ""

    def validate(self) -> None:
        if not self.node_id.strip() or not self.input_node_id.strip():
            raise ValueError("Transformer nodes need node_id and input_node_id")
        if not self.name.strip():
            self.name = self.node_id
        validate_transform(self.transform)


@dataclass
class CombinerNode:
    """A node that combines the outputs of signal branches."""

    node_id: str = "combiner"
    method: str = "weighted_mean"
    input_node_ids: list[str] = field(default_factory=list)
    name: str = "Combiner"

    def validate(self) -> None:
        if not self.node_id.strip():
            raise ValueError("Combiner node needs a node_id")
        if not self.name.strip():
            self.name = self.node_id
        if self.method not in SUPPORTED_COMBINATIONS:
            raise ValueError(f"Unsupported combiner method: {self.method}")


@dataclass
class Strategy:
    """A saved signal recipe; component outputs are combined per date and bond."""

    name: str
    signals: list[SignalComponent]
    strategy_id: str = ""
    transformer_nodes: list[TransformerNode] = field(default_factory=list)
    combiners: list[CombinerNode] = field(default_factory=lambda: [CombinerNode()])
    output_node_id: str = "combiner"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = STRATEGY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != STRATEGY_SCHEMA_VERSION:
            raise ValueError(f"Unsupported strategy schema version: {self.schema_version}")
        if not self.name.strip():
            raise ValueError("Strategy name cannot be empty")
        if not self.signals:
            raise ValueError("A strategy must contain at least one signal")
        ids = [component.signal_id for component in self.signals]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal IDs must be unique within a strategy")
        for component in self.signals:
            component.validate()
        if not self.combiners:
            raise ValueError("A strategy must contain at least one combiner node")
        for combiner in self.combiners:
            combiner.validate()
        signal_node_ids = {f"signal:{signal.signal_id}" for signal in self.signals}
        transformer_ids = [node.node_id for node in self.transformer_nodes]
        combiner_ids = [node.node_id for node in self.combiners]
        all_ids = [*signal_node_ids, *transformer_ids, *combiner_ids]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("Strategy node IDs must be unique")
        display_names = [signal.signal_id.strip().casefold() for signal in self.signals]
        display_names.extend(node.name.strip().casefold() for node in self.transformer_nodes)
        display_names.extend(node.name.strip().casefold() for node in self.combiners)
        if any(not name for name in display_names) or len(display_names) != len(set(display_names)):
            raise ValueError("Signal, transformer, and combiner names must be non-empty and unique")
        for node in self.transformer_nodes:
            node.validate()
            if node.input_node_id not in set(all_ids):
                raise ValueError(f"Transformer {node.node_id} references unknown input {node.input_node_id}")
            if node.input_node_id == node.node_id:
                raise ValueError(f"Transformer {node.node_id} cannot connect to itself")
        valid_combiner_inputs = set(signal_node_ids) | set(transformer_ids) | set(combiner_ids)
        for combiner in self.combiners:
            if len(combiner.input_node_ids) != len(set(combiner.input_node_ids)):
                raise ValueError(f"Inputs to combiner {combiner.name} must be unique")
            if not combiner.input_node_ids:
                raise ValueError(f"Combiner {combiner.name} must have at least one connected input")
            if any(node_id not in valid_combiner_inputs or node_id == combiner.node_id
                   for node_id in combiner.input_node_ids):
                raise ValueError(f"Combiner {combiner.name} has an invalid input connection")
        if self.output_node_id not in set(all_ids):
            raise ValueError(f"Unknown strategy output node: {self.output_node_id}")
        for combiner in self.combiners:
            if combiner.method == "weighted_mean" and not any(s.weight > 0 for s in self.signals):
                raise ValueError("weighted_mean requires at least one positive signal weight")
            if combiner.method in {"weighted_mean", "mean", "rank_mean"} and any(s.weight < 0 for s in self.signals):
                raise ValueError(f"{combiner.method} does not allow negative weights")
        if not self.signals and self.transformer_nodes:
            raise ValueError("A saved strategy cannot have transformers without signal components")

        # Reject cycles before evaluation or serialization.
        dependencies = {node.node_id: [node.input_node_id] for node in self.transformer_nodes}
        dependencies.update({node.node_id: list(node.input_node_ids) for node in self.combiners})
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("Strategy graph cannot contain cycles")
            if node_id in visited:
                return
            visiting.add(node_id)
            for parent_id in dependencies.get(node_id, []):
                visit(parent_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in all_ids:
            visit(node_id)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Strategy":
        schema_version = int(payload.get("schema_version", 1))
        signal_payloads = [dict(item) for item in payload.get("signals", [])]
        transformer_payloads = list(payload.get("transformer_nodes", []))
        combiners: list[CombinerNode]
        if schema_version == 1:
            # Migrate the original embedded transformer lists to explicit nodes.
            for signal in signal_payloads:
                embedded = signal.pop("transformers", [])
                previous = f"signal:{signal['signal_id']}"
                for index, transform in enumerate(embedded, start=1):
                    node_id = f"transform:{signal['signal_id']}:{index}"
                    transformer_payloads.append({
                        "node_id": node_id, "input_node_id": previous, "transform": transform,
                    })
                    previous = node_id
            combiner_node_id = "combiner"
            inputs = []
            for signal in signal_payloads:
                candidates = [node for node in transformer_payloads
                              if node["input_node_id"] == f"signal:{signal['signal_id']}"
                              or node["node_id"].startswith(f"transform:{signal['signal_id']}:")]
                inputs.append(candidates[-1]["node_id"] if candidates else f"signal:{signal['signal_id']}")
            previous = combiner_node_id
            for index, transform in enumerate(payload.get("transformers", []), start=1):
                node_id = f"strategy_transform:{index}"
                transformer_payloads.append({
                    "node_id": node_id, "input_node_id": previous, "transform": transform,
                })
                previous = node_id
            combiner = CombinerNode(
                node_id=combiner_node_id,
                method=str(payload.get("combination_method", "weighted_mean")),
                input_node_ids=inputs,
                name="Combiner 1",
            )
            combiners = [combiner]
            output_node_id = previous
        elif schema_version == 2:
            # Migrate the previous single-combiner graph format.
            old_combiner = dict(payload.get("combiner", {}))
            combiners = [CombinerNode(**old_combiner)]
            output_node_id = str(payload.get("output_node_id", combiners[0].node_id))
        else:
            combiners = [CombinerNode(**item) for item in payload.get("combiners", [])]
            output_node_id = str(payload.get("output_node_id", combiners[-1].node_id if combiners else ""))
        if schema_version < 3:
            for node in transformer_payloads:
                node.setdefault("name", node["node_id"])
        components = [SignalComponent(**item) for item in signal_payloads]
        strategy = cls(
            name=str(payload.get("name", "")),
            signals=components,
            strategy_id=str(payload.get("strategy_id", "")),
            transformer_nodes=[TransformerNode(**item) for item in transformer_payloads],
            combiners=combiners,
            output_node_id=output_node_id,
            metadata=dict(payload.get("metadata", {})),
            schema_version=STRATEGY_SCHEMA_VERSION,
        )
        strategy.validate()
        return strategy

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Strategy":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_transform(transform: Mapping[str, Any]) -> None:
    kind = transform.get("type")
    if kind not in SUPPORTED_TRANSFORMS:
        raise ValueError(f"Unsupported transformer: {kind}")
    if kind == "winsorize":
        lower = float(transform.get("lower", 0.01))
        upper = float(transform.get("upper", 0.99))
        if not 0 <= lower < upper <= 1:
            raise ValueError("winsorize lower/upper must satisfy 0 <= lower < upper <= 1")


def transform_scores(
    values: Mapping[str, float | None],
    transformers: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    """Apply cross-sectional transforms while retaining missing bond scores."""
    output = dict(values)
    for transform in transformers:
        validate_transform(transform)
        keys = [key for key, value in output.items() if value is not None and math.isfinite(float(value))]
        if not keys:
            continue
        array = np.asarray([float(output[key]) for key in keys], dtype=float)
        kind = transform["type"]
        if kind == "winsorize":
            array = np.clip(array, np.quantile(array, float(transform.get("lower", 0.01))),
                            np.quantile(array, float(transform.get("upper", 0.99))))
        elif kind == "zscore":
            scale = float(np.std(array, ddof=1)) if len(array) > 1 else 0.0
            array = (array - float(np.mean(array))) / scale if scale > 0 else np.zeros_like(array)
        elif kind == "minmax":
            width = float(np.max(array) - np.min(array))
            array = (array - float(np.min(array))) / width if width > 0 else np.zeros_like(array)
        elif kind == "percentile":
            order = np.argsort(array, kind="mergesort")
            ranks = np.empty(len(array), dtype=float)
            start = 0
            while start < len(array):
                end = start + 1
                while end < len(array) and array[order[end]] == array[order[start]]:
                    end += 1
                ranks[order[start:end]] = (start + end - 1) / 2
                start = end
            array = ranks / (len(array) - 1) if len(array) > 1 else np.full(1, 0.5)
        output.update({key: float(value) for key, value in zip(keys, array)})
    return output


def evaluate_strategy_nodes(
    strategy: Strategy,
    scores_by_signal: Mapping[str, Mapping[str, float | None]],
) -> dict[str, dict[str, float | None]]:
    """Return the score map for every signal, transformer, and combiner node."""
    strategy.validate()
    components = {f"signal:{item.signal_id}": item for item in strategy.signals}
    transformers = {item.node_id: item for item in strategy.transformer_nodes}
    combiners = {item.node_id: item for item in strategy.combiners}
    component_weights = {f"signal:{item.signal_id}": item.weight for item in strategy.signals}
    cache: dict[str, dict[str, float | None]] = {}

    def combine(node_id: str) -> dict[str, float | None]:
        combiner = combiners[node_id]
        method = combiner.method
        # Evaluate inputs first: transformer and nested combiner weights are
        # propagated during calculation, so looking them up beforehand fails.
        source_maps = [(input_id, calculate(input_id)) for input_id in combiner.input_node_ids]
        weights = {input_id: component_weights[input_id] for input_id in combiner.input_node_ids}
        if method == "rank_mean":
            source_maps = [(node_id, transform_scores(values, [{"type": "percentile"}]))
                           for node_id, values in source_maps]
        bond_ids = set().union(*(set(values) for _, values in source_maps)) if source_maps else set()
        result: dict[str, float | None] = {}
        for bond_id in bond_ids:
            present = [(node_id, values.get(bond_id)) for node_id, values in source_maps]
            present = [(node_id, float(value)) for node_id, value in present
                       if value is not None and math.isfinite(float(value))]
            if not present:
                result[bond_id] = None
                continue
            if method == "sum":
                result[bond_id] = float(sum(weights[node_id] * value for node_id, value in present))
                continue
            weighted = [(1.0 if method == "mean" else weights[node_id], value)
                        for node_id, value in present
                        if method == "mean" or weights[node_id] > 0]
            total_weight = sum(weight for weight, _ in weighted)
            result[bond_id] = (
                float(sum(weight * value for weight, value in weighted) / total_weight)
                if total_weight else None
            )
        return result

    def calculate(node_id: str) -> dict[str, float | None]:
        if node_id in cache:
            return cache[node_id]
        if node_id in components:
            result = dict(scores_by_signal.get(components[node_id].signal_id, {}))
            component_weights[node_id] = components[node_id].weight
        elif node_id in transformers:
            node = transformers[node_id]
            result = transform_scores(calculate(node.input_node_id), [node.transform])
            component_weights[node_id] = component_weights[node.input_node_id]
        elif node_id in combiners:
            result = combine(node_id)
            component_weights[node_id] = 1.0
        else:
            raise ValueError(f"Cannot evaluate unknown strategy node {node_id}")
        cache[node_id] = result
        return result

    for node_id in [*components, *transformers, *combiners]:
        calculate(node_id)
    return cache


def combine_signal_scores(
    strategy: Strategy,
    scores_by_signal: Mapping[str, Mapping[str, float | None]],
) -> dict[str, float | None]:
    """Combine component score maps keyed by signal_id."""
    node_scores = evaluate_strategy_nodes(strategy, scores_by_signal)
    return node_scores[strategy.output_node_id]
