"""Stage 3: typed error computation. The type decides the comparator.

A single cosine check is wrong for paths/enums (`scripts/deploy.sh` and
`scripts/deploy_v2.sh` embed as "close" but are simply different), so exact-match
types get a binary comparator and only free text falls back to embedding distance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .embed import Embedder, similarity
from .types import EXACT_TYPES, TypedValue, ValueType

# Hand-tuned, not learned. Error at or below tolerance is a restatement (NOOP).
TOLERANCE: dict[ValueType, float] = {
    ValueType.INT: 0.0,
    ValueType.FLOAT: 1e-9,
    ValueType.DATE: 0.0,
    ValueType.PATH: 0.0,
    ValueType.ID: 0.0,
    ValueType.ENUM: 0.0,
    ValueType.STRING: 0.0,
    ValueType.TEXT: 0.03,
}


@dataclass(frozen=True)
class ErrorResult:
    magnitude: float       # >= 0, what the decision stage compares to tolerance
    delta: float | None    # signed difference for numeric types
    comparator: str

    def is_restatement(self, vtype: ValueType) -> bool:
        return self.magnitude <= TOLERANCE[vtype]


def compute_error(new: TypedValue, old: TypedValue, embedder: Embedder) -> ErrorResult:
    if new.type != old.type:
        return ErrorResult(1.0, None, f"type_change:{old.type.value}->{new.type.value}")

    t = new.type
    if t in (ValueType.INT, ValueType.FLOAT):
        d = float(new.data) - float(old.data)
        if t == ValueType.FLOAT:
            # relative for floats so tolerance is scale-free
            scale = max(abs(float(old.data)), 1e-12)
            return ErrorResult(abs(d) / scale, d, "numeric_relative")
        return ErrorResult(abs(d), d, "numeric")
    if t == ValueType.DATE:
        d = (date.fromisoformat(new.data) - date.fromisoformat(old.data)).days
        return ErrorResult(abs(float(d)), float(d), "date_days")
    if t in EXACT_TYPES:
        e = 0.0 if new.data == old.data else 1.0
        return ErrorResult(e, None, "exact")
    # TEXT: the soft fallback. Cannot distinguish "changed" from "refined".
    if new.data == old.data:
        return ErrorResult(0.0, None, "text_identical")
    return ErrorResult(1.0 - similarity(embedder, new.data, old.data), None, "text_cosine")


def describe(entity: str, predicate: str, old: TypedValue, new: TypedValue, err: ErrorResult, source: str) -> str:
    key = f"{entity}.{predicate}"
    if err.comparator.startswith("type_change"):
        return f"{key} changed type ({err.comparator}): {old.data!r} -> {new.data!r} via {source}"
    if err.delta is not None:
        sign = "+" if err.delta > 0 else ""
        unit = " days" if new.type == ValueType.DATE else ""
        d = int(err.delta) if float(err.delta).is_integer() else round(err.delta, 6)
        return f"{key} corrected {old.data!r} -> {new.data!r} ({sign}{d}{unit}) via {source}"
    if err.comparator == "text_cosine":
        return f"{key} text revised (distance {err.magnitude:.3f}) via {source}"
    return f"{key} replaced {old.data!r} -> {new.data!r} via {source}"
