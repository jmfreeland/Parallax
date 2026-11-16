"""Run and cache strategy component scores for fast combination experiments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .signal_evaluation import evaluate_nelson_siegel_signal
from .strategy import Strategy, combine_signal_scores


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
            if (candidate.get("cache_schema_version") == 1
                    and candidate.get("input_signature") == _digest(base_parameters)):
                old_cache = candidate
        except (OSError, json.JSONDecodeError):
            old_cache = {}
    signal_signatures: dict[str, str] = {}
    for index, signal in enumerate(strategy.signals, start=1):
        signal_signature = _digest(asdict(signal))
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
        "cache_schema_version": 1,
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


def recombine_cached_signals(strategy: Strategy, cache_path: Path) -> list[dict[str, Any]]:
    """Apply the current strategy graph to cached component scores by date."""
    strategy.validate()
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    expected_signatures = {
        signal.signal_id: _digest(asdict(signal)) for signal in strategy.signals
    }
    if cache.get("cache_schema_version") != 1:
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
    for as_of_date in sorted(by_date):
        date_scores = by_date[as_of_date]
        combined = combine_signal_scores(strategy, date_scores)
        isins = set().union(*(set(values) for values in date_scores.values()), set(combined))
        for isin in sorted(isins):
            row: dict[str, Any] = {"as_of_date": as_of_date, "isin": isin}
            row.update({
                f"signal:{signal_id}": scores.get(isin)
                for signal_id, scores in date_scores.items()
            })
            row["strategy_score"] = combined.get(isin)
            output.append(row)
    return output
