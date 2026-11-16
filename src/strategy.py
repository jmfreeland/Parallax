"""Editable multi-signal strategy definitions and score transformations."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


STRATEGY_SCHEMA_VERSION = 1
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
    transformers: list[dict[str, Any]] = field(default_factory=list)

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
        for transform in self.transformers:
            validate_transform(transform)


@dataclass
class Strategy:
    """A saved signal recipe; component outputs are combined per date and bond."""

    name: str
    signals: list[SignalComponent]
    strategy_id: str = ""
    combination_method: str = "weighted_mean"
    transformers: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = STRATEGY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != STRATEGY_SCHEMA_VERSION:
            raise ValueError(f"Unsupported strategy schema version: {self.schema_version}")
        if not self.name.strip():
            raise ValueError("Strategy name cannot be empty")
        if not self.signals:
            raise ValueError("A strategy must contain at least one signal")
        if self.combination_method not in SUPPORTED_COMBINATIONS:
            raise ValueError(f"Unsupported combination_method: {self.combination_method}")
        ids = [component.signal_id for component in self.signals]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal IDs must be unique within a strategy")
        for component in self.signals:
            component.validate()
        for transform in self.transformers:
            validate_transform(transform)
        if self.combination_method == "weighted_mean" and not any(s.weight > 0 for s in self.signals):
            raise ValueError("weighted_mean requires at least one positive signal weight")
        if self.combination_method in {"weighted_mean", "mean", "rank_mean"} and any(s.weight < 0 for s in self.signals):
            raise ValueError(f"{self.combination_method} does not allow negative weights")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Strategy":
        components = [SignalComponent(**item) for item in payload.get("signals", [])]
        strategy = cls(
            name=str(payload.get("name", "")),
            signals=components,
            strategy_id=str(payload.get("strategy_id", "")),
            combination_method=str(payload.get("combination_method", "weighted_mean")),
            transformers=list(payload.get("transformers", [])),
            metadata=dict(payload.get("metadata", {})),
            schema_version=int(payload.get("schema_version", STRATEGY_SCHEMA_VERSION)),
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


def combine_signal_scores(
    strategy: Strategy,
    scores_by_signal: Mapping[str, Mapping[str, float | None]],
) -> dict[str, float | None]:
    """Combine component score maps keyed by signal_id, renormalizing available inputs."""
    strategy.validate()
    transformed: dict[str, dict[str, float | None]] = {}
    for component in strategy.signals:
        transformed[component.signal_id] = transform_scores(
            scores_by_signal.get(component.signal_id, {}), component.transformers
        )
        if strategy.combination_method == "rank_mean":
            transformed[component.signal_id] = transform_scores(
                transformed[component.signal_id], [{"type": "percentile"}]
            )
    bond_ids = set().union(*(set(values) for values in transformed.values()))
    combined: dict[str, float | None] = {}
    for bond_id in bond_ids:
        present = [
            (component, transformed[component.signal_id].get(bond_id))
            for component in strategy.signals
        ]
        present = [(component, float(value)) for component, value in present
                   if value is not None and math.isfinite(float(value))]
        if not present:
            combined[bond_id] = None
            continue
        if strategy.combination_method == "sum":
            combined[bond_id] = float(sum(component.weight * value for component, value in present))
        else:
            weighted = [(component.weight, value) for component, value in present
                        if strategy.combination_method in {"weighted_mean", "rank_mean"} and component.weight > 0]
            if strategy.combination_method == "mean":
                weighted = [(1.0, value) for _, value in present]
            total_weight = sum(weight for weight, _ in weighted)
            combined[bond_id] = (
                float(sum(weight * value for weight, value in weighted) / total_weight)
                if total_weight else None
            )
    return transform_scores(combined, strategy.transformers)
