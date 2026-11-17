"""Run and cache strategy component scores for fast combination experiments."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .signal_evaluation import evaluate_nelson_siegel_signal
from .strategy import Strategy, evaluate_strategy_nodes


STRATEGY_INPUT_CACHE_VERSION = 2
STRATEGY_RUNS_DIR = Path(__file__).resolve().parents[1] / "data" / "strategies" / "runs"


def stable_strategy_id(strategy_path: Path, declared_id: str = "") -> str:
    """Return the saved ID, or a stable path-based ID for older JSON files."""
    return declared_id or uuid.uuid5(uuid.NAMESPACE_URL, str(Path(strategy_path).resolve())).hex


def strategy_cache_path(strategy_id: str) -> Path:
    return STRATEGY_RUNS_DIR / f"{strategy_id}.inputs.json"


def strategy_results_path(strategy_id: str) -> Path:
    return STRATEGY_RUNS_DIR / f"{strategy_id}.results.json"


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _digest(payload: Any) -> str:
    encoded = json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _signal_calculation_signature(signal: Any) -> str:
    definition = asdict(signal)
    if signal.predictor_id in {"historical_partition_percentile", "historical_partition_zscore"}:
        # The ISIN partition minimum-bond handling changed from five to one.
        definition["calculation_version"] = 2
    return _digest(definition)


def run_strategy_signals(
    strategy: Strategy,
    evaluation_parameters: Mapping[str, Any],
    cache_path: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Evaluate each signal once and persist its bond/date scores.

    ``evaluation_parameters`` are shared TRACE inputs and dates accepted by
    :func:`evaluate_nelson_siegel_signal`. Signal-specific predictor, space,
    partition, and history settings come from the strategy. Combiner and
    transformer settings are excluded from the cache signature, so they can
    be edited and recombined without rerunning signals.
    """
    strategy.validate()
    base_parameters = dict(evaluation_parameters)
    if "start_date" not in base_parameters or "end_date" not in base_parameters:
        raise ValueError("Strategy runs require start_date and end_date")
    score_rows: dict[str, list[dict[str, Any]]] = {}
    cache_path = Path(cache_path)
    old_cache: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            candidate = json.loads(cache_path.read_text(encoding="utf-8"))
            if (candidate.get("cache_schema_version") in {1, STRATEGY_INPUT_CACHE_VERSION}
                    and candidate.get("input_signature") == _digest(base_parameters)):
                old_cache = candidate
        except (OSError, json.JSONDecodeError):
            old_cache = {}
    signal_signatures: dict[str, str] = {}
    for index, signal in enumerate(strategy.signals, start=1):
        signal_signature = _signal_calculation_signature(signal)
        signal_signatures[signal.signal_id] = signal_signature
        cached_rows = old_cache.get("signals", {}).get(signal.signal_id)
        if (cached_rows is not None
                and old_cache.get("signal_signatures", {}).get(signal.signal_id) == signal_signature):
            score_rows[signal.signal_id] = cached_rows
            if on_progress is not None:
                on_progress(index, len(strategy.signals))
            continue
        parameters = dict(base_parameters)
        parameters.update({
            "predictor_id": signal.predictor_id,
            "signal_space": signal.signal_space,
            "partition_field": signal.group_field or "sector",
            "percentile_lookback_days": int(signal.parameters.get("lookback_days", 1095)),
            "minimum_history_observations": int(
                signal.parameters.get("minimum_history_observations", 20)
            ),
        })
        if signal.predictor_id == "nelson_siegel_residual":
            parameters["minimum_bonds"] = max(5, int(parameters.get("minimum_bonds", 5)))
        else:
            parameters["minimum_bonds"] = int(signal.parameters.get(
                "minimum_bonds",
                1 if signal.group_field == "isin" else int(parameters.get("minimum_bonds", 5)),
            ))
        def report_signal_progress(current: int, total: int) -> None:
            if on_progress is not None:
                progress = (index - 1) * 100 + int(100 * current / max(total, 1))
                on_progress(progress, len(strategy.signals) * 100)

        parameters["on_progress"] = report_signal_progress
        result = evaluate_nelson_siegel_signal(**parameters)
        rows = []
        for row in result["bond_results"]:
            score = row.get("signal_value_pct", row.get("signal_value"))
            rows.append({
                "as_of_date": str(row["as_of_date"]),
                "isin": str(row["isin"]),
                "score": float(score) if score is not None else None,
            })
        score_rows[signal.signal_id] = rows
        if on_progress is not None:
            on_progress(index * 100, len(strategy.signals) * 100)

    signal_definitions = [asdict(signal) for signal in strategy.signals]
    cache = {
        "cache_schema_version": STRATEGY_INPUT_CACHE_VERSION,
        "signal_signature": _digest(signal_definitions),
        "signal_signatures": signal_signatures,
        "input_signature": _digest(base_parameters),
        "signal_definitions": signal_definitions,
        "evaluation_parameters": _json_value(base_parameters),
        "signals": score_rows,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return cache


def recombine_cached_signals(
    strategy: Strategy,
    cache_path: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Apply the current strategy graph to cached component scores by date."""
    strategy.validate()
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    expected_signatures = {
        signal.signal_id: _signal_calculation_signature(signal) for signal in strategy.signals
    }
    if cache.get("cache_schema_version") != STRATEGY_INPUT_CACHE_VERSION:
        raise ValueError("Unsupported strategy signal cache version")
    cached_signatures = cache.get("signal_signatures", {})
    cached_signal_ids = set(cache.get("signals", {}))
    expected_signal_ids = {signal.signal_id for signal in strategy.signals}
    if cached_signal_ids != expected_signal_ids:
        raise ValueError("Cached signal list does not match the current strategy")
    mismatched = [signal_id for signal_id, signature in expected_signatures.items()
                  if cached_signatures.get(signal_id) != signature]
    if mismatched:
        raise ValueError("Cached inputs are missing or stale for signals: " + ", ".join(mismatched))

    by_date: dict[str, dict[str, dict[str, float | None]]] = {}
    for signal_id, rows in cache["signals"].items():
        for row in rows:
            by_date.setdefault(row["as_of_date"], {}).setdefault(signal_id, {})[
                row["isin"]
            ] = row["score"]
    output: list[dict[str, Any]] = []
    ordered_dates = sorted(by_date)
    for index, as_of_date in enumerate(ordered_dates, start=1):
        date_scores = by_date[as_of_date]
        node_scores = evaluate_strategy_nodes(strategy, date_scores)
        combined = node_scores[strategy.output_node_id]
        isins = set().union(*(set(values) for values in node_scores.values()), set(combined))
        for isin in sorted(isins):
            row: dict[str, Any] = {"as_of_date": as_of_date, "isin": isin}
            row.update({
                f"signal:{signal_id}": scores.get(isin)
                for signal_id, scores in date_scores.items()
            })
            row.update({
                f"node:{node_id}": scores.get(isin)
                for node_id, scores in node_scores.items()
            })
            row["strategy_score"] = combined.get(isin)
            output.append(row)
        if on_progress is not None:
            on_progress(index, len(ordered_dates))
    return output


def cached_input_status(strategy: Strategy, cache_path: Path) -> tuple[str, int]:
    """Describe whether a strategy has reusable, current signal inputs."""
    cache_path = Path(cache_path)
    if not cache_path.is_file():
        return "No cached inputs", 0
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Cache unreadable", 0
    signal_ids = {signal.signal_id for signal in strategy.signals}
    if cache.get("cache_schema_version") != STRATEGY_INPUT_CACHE_VERSION:
        return "Needs recalculation", 0
    if set(cache.get("signals", {})) != signal_ids:
        return "Needs recalculation", 0
    signatures = cache.get("signal_signatures", {})
    if any(signatures.get(signal.signal_id) != _signal_calculation_signature(signal)
           for signal in strategy.signals):
        return "Needs recalculation", 0
    rows = sum(len(values) for values in cache.get("signals", {}).values())
    return ("Ready" if rows else "No signal rows"), rows


def saved_strategy_info(strategy_path: Path) -> dict[str, Any]:
    strategy_path = Path(strategy_path)
    strategy = Strategy.load(strategy_path)
    strategy.strategy_id = stable_strategy_id(strategy_path, strategy.strategy_id)
    cache_path = strategy_cache_path(strategy.strategy_id)
    status, row_count = cached_input_status(strategy, cache_path)
    return {
        "name": strategy.name,
        "strategy_id": strategy.strategy_id,
        "cache_status": status,
        "cached_rows": row_count,
        "signal_count": len(strategy.signals),
        "cache_path": str(cache_path),
        "result_path": str(strategy_results_path(strategy.strategy_id)),
    }


def run_saved_strategy(strategy_path: Path, *, on_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Recombine the selected saved strategy's cache and persist the run result."""
    strategy_path = Path(strategy_path)
    strategy = Strategy.load(strategy_path)
    strategy.strategy_id = stable_strategy_id(strategy_path, strategy.strategy_id)
    cache_path = strategy_cache_path(strategy.strategy_id)
    rows = recombine_cached_signals(strategy, cache_path, on_progress=on_progress)
    result_path = strategy_results_path(strategy.strategy_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({
        "strategy_id": strategy.strategy_id,
        "strategy_name": strategy.name,
        "strategy": strategy.to_dict(),
        "rows": rows,
    }, indent=2) + "\n", encoding="utf-8")
    return {"strategy_name": strategy.name, "result_path": str(result_path), "row_count": len(rows)}
