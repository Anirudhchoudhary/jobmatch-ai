"""Load threshold bounds from evals/thresholds.yaml and check a metrics summary against them."""

import math
from pathlib import Path
from typing import Literal

import yaml

Bound = tuple[Literal["min", "max"], float]


def load_thresholds(path: Path, profile: str) -> dict[str, Bound]:
    """Read `{profile: {metric: {min: x} | {max: x}}}`; unknown profile raises KeyError."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data[profile] or {}
    bounds: dict[str, Bound] = {}
    for metric, spec in raw.items():
        if not isinstance(spec, dict) or len(spec) != 1:
            raise ValueError(f"{metric}: expected exactly one of min/max")
        ((kind, value),) = spec.items()
        if kind == "min":
            bounds[metric] = ("min", float(value))
        elif kind == "max":
            bounds[metric] = ("max", float(value))
        else:
            raise ValueError(f"{metric}: unknown bound {kind!r}")
    return bounds


def check(summary: dict[str, float], bounds: dict[str, Bound]) -> list[str]:
    """One message per failure. A bound on a metric absent from summary is a failure."""
    failures: list[str] = []
    for metric, (kind, limit) in bounds.items():
        if metric not in summary:
            failures.append(f"missing metric {metric}")
            continue
        value = summary[metric]
        if not math.isfinite(value):
            failures.append(f"{metric} is not finite")
            continue
        if kind == "min" and value < limit:
            failures.append(f"{metric} {value:.2f} < min {limit:.2f}")
        elif kind == "max" and value > limit:
            failures.append(f"{metric} {value:.2f} > max {limit:.2f}")
    return failures
