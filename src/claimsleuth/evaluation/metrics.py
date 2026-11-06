"""Metric calculations for the agent evaluation. Pure functions over (ground truth, prediction) pairs.

Conventions:
  * a metric that cannot be computed (no observations, no positive class, undefined division) is None, shown as
    N/A. None is never replaced by 0;
  * pairs whose prediction is None (the agent produced no usable output) are excluded from accuracy-style metrics and
    counted separately as failures; reliability is reported on its own, so a failure is never scored as a wrong
    answer and never as a right one.
"""
from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from typing import Any


def ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _scored(pairs: Sequence[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
    return [(t, p) for t, p in pairs if p is not None and t is not None]


def binary_metrics(pairs: Sequence[tuple[bool | None, bool | None]]) -> dict[str, Any]:
    """Accuracy, precision, recall, F1, false-positive rate and the confusion counts for a binary task
    (positive = True). Pairs with a None prediction are excluded and counted in `unscored`."""
    scored = _scored(pairs)
    tp = sum(1 for t, p in scored if t and p)
    fp = sum(1 for t, p in scored if not t and p)
    fn = sum(1 for t, p in scored if t and not p)
    tn = sum(1 for t, p in scored if not t and not p)
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = (None if precision is None or recall is None
          else 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    return {"n": len(scored), "unscored": len(pairs) - len(scored), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "accuracy": ratio(tp + tn, len(scored)), "precision": precision, "recall": recall, "f1": f1,
            "false_positive_rate": ratio(fp, fp + tn)}


def confusion_matrix(pairs: Sequence[tuple[Hashable | None, Hashable | None]], labels: Sequence[Hashable]
                     ) -> dict[str, Any]:
    """matrix[i][j] = cases with true label labels[i] predicted as labels[j]. Unscored pairs are counted apart."""
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    scored = _scored(pairs)
    for t, p in scored:
        if t not in index or p not in index:
            raise ValueError(f"label outside {list(labels)}: true={t!r} predicted={p!r}")
        matrix[index[t]][index[p]] += 1
    return {"labels": list(labels), "matrix": matrix, "n": len(scored), "unscored": len(pairs) - len(scored)}


def multiclass_metrics(pairs: Sequence[tuple[Hashable | None, Hashable | None]], labels: Sequence[Hashable]
                       ) -> dict[str, Any]:
    """Accuracy plus per-class, macro and support-weighted precision / recall / F1 (scikit-learn conventions).

    The averages cover the classes that occur in the ground truth or the predictions of the scored pairs (classes
    that occur in neither are left out, not counted as 0). Within those classes a ratio with an empty denominator
    is 0 (zero_division=0): a class that occurs but is never predicted has recall 0 and precision 0. Weighted
    averages use the true-class support. With no scored pairs everything is None."""
    cm = confusion_matrix(pairs, labels)
    m, n = cm["matrix"], cm["n"]
    per_class = {}
    for i, label in enumerate(labels):
        tp = m[i][i]
        predicted = sum(m[r][i] for r in range(len(labels)))
        support = sum(m[i])
        if not predicted and not support:
            per_class[str(label)] = {"precision": None, "recall": None, "f1": None, "support": 0, "predicted": 0,
                                     "present": False}
            continue
        p, r = ratio(tp, predicted) or 0.0, ratio(tp, support) or 0.0
        f1 = 0.0 if p + r == 0 else 2 * p * r / (p + r)
        per_class[str(label)] = {"precision": p, "recall": r, "f1": f1, "support": support, "predicted": predicted,
                                 "present": True}
    present = [v for v in per_class.values() if v["present"]]

    def macro(key: str) -> dict[str, Any]:
        return {"value": sum(v[key] for v in present) / len(present) if present else None,
                "classes": [k for k, v in per_class.items() if v["present"]]}

    def weighted(key: str) -> float | None:
        total = sum(v["support"] for v in present)
        return None if not total else sum(v[key] * v["support"] for v in present) / total

    correct = sum(m[i][i] for i in range(len(labels)))
    return {"n": n, "unscored": cm["unscored"], "accuracy": ratio(correct, n), "per_class": per_class,
            "macro": {k: macro(k) for k in ("precision", "recall", "f1")},
            "weighted": {k: weighted(k) for k in ("precision", "recall", "f1")},
            "zero_division": 0, "confusion": cm}


def mean(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the same as numpy's default 'linear' method)."""
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


MIN_BOX_OBSERVATIONS = 5


def box_stats(values: Sequence[float | None], min_n: int = MIN_BOX_OBSERVATIONS) -> dict[str, Any]:
    """Tukey box-plot statistics: median, Q1, Q3, whiskers at the most extreme points within 1.5 x IQR, outliers.
    Fewer than `min_n` real observations: no box, a note instead (no observations are invented)."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < min_n:
        return {"n": len(vals), "sufficient": False,
                "note": f"{len(vals)} observation(s); at least {min_n} are needed for a box plot - more runs are "
                        "required."}
    q1, med, q3 = _quantile(vals, .25), _quantile(vals, .5), _quantile(vals, .75)
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = [v for v in vals if lo_fence <= v <= hi_fence]
    return {"n": len(vals), "sufficient": True, "min": vals[0], "max": vals[-1], "q1": q1, "median": med, "q3": q3,
            "iqr": iqr, "whisker_low": min(inside), "whisker_high": max(inside),
            "outliers": [v for v in vals if v < lo_fence or v > hi_fence], "mean": sum(vals) / len(vals)}
