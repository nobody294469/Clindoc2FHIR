"""Quality and reliability gate checking engine for M-BENCHMARK."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from clindoc.eval.schema import BenchmarkReport, GateCheckResult


def _extract_nested_value(data: Dict[str, Any], path: str) -> Optional[float]:
    """Traverse a dotted path in a nested dictionary/model dict to extract a numeric float."""
    if path.startswith("field_metrics."):
        parts = path.split(".")
        attr = parts[-1]
        field_type = ".".join(parts[1:-1])
        fms = data.get("field_metrics", {})
        fm = fms.get(field_type)
        if fm is None:
            return None
        val = fm.get(attr) if isinstance(fm, dict) else getattr(fm, attr, None)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    current: Any = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
    if current is None:
        return None
    try:
        return float(current)
    except (ValueError, TypeError):
        return None


def evaluate_gates(
    report: BenchmarkReport,
    gate_config_path: Optional[Path] = None,
    gate_config_dict: Optional[Dict[str, Any]] = None,
) -> Tuple[List[GateCheckResult], bool]:
    """Evaluate benchmark results against configured gate thresholds.

    Args:
        report: BenchmarkReport containing evaluated datasets.
        gate_config_path: Path to JSON configuration file (e.g. configs/eval_gates.json).
        gate_config_dict: In-memory gate config dictionary (optional override).

    Returns:
        Tuple of (list of GateCheckResult, overall_passed boolean).
    """
    if gate_config_dict is None:
        if gate_config_path is None:
            default_path = Path("configs/eval_gates.json")
            if default_path.exists():
                gate_config_path = default_path
            else:
                return [], True
        with open(gate_config_path, "r", encoding="utf-8") as f:
            gate_config_dict = json.load(f)

    gates = gate_config_dict.get("gates", [])
    results: List[GateCheckResult] = []
    overall_passed = True

    report_dict = report.model_dump()
    datasets_dict = report_dict.get("datasets", {})

    for gate in gates:
        metric_name = gate.get("metric_name", "Unknown Gate")
        target_threshold = float(gate.get("target_threshold", 0.0))
        dataset_name = gate.get("dataset_name", "")
        path = gate.get("path", "")
        comparison = gate.get("comparison", "gte")

        dataset_data = datasets_dict.get(dataset_name)
        if dataset_data is None:
            # Dataset not included in this report (e.g. subset run)
            continue

        measured = _extract_nested_value(dataset_data, path)
        if measured is None:
            # Metric was not evaluated or unavailable
            passed = False
            measured_val = 0.0
        else:
            measured_val = measured
            if comparison == "gte":
                passed = measured_val >= (target_threshold - 1e-6)
            elif comparison == "lte":
                passed = measured_val <= (target_threshold + 1e-6)
            elif comparison == "gt":
                passed = measured_val > target_threshold
            elif comparison == "lt":
                passed = measured_val < target_threshold
            else:
                passed = abs(measured_val - target_threshold) < 1e-4

        if not passed:
            overall_passed = False

        results.append(
            GateCheckResult(
                metric_name=metric_name,
                target_threshold=target_threshold,
                measured_value=round(measured_val, 4),
                passed=passed,
                dataset_name=dataset_name,
            )
        )

    return results, overall_passed
