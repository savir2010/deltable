"""Core data model: typed values, slots, corrections, pending reconciliations."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class ValueType(str, Enum):
    INT = "int"
    FLOAT = "float"
    DATE = "date"
    PATH = "path"
    ID = "id"
    ENUM = "enum"
    STRING = "string"  # short token, compared exactly
    TEXT = "text"      # free prose, compared by embedding distance (the soft fallback)


NUMERIC_TYPES = {ValueType.INT, ValueType.FLOAT, ValueType.DATE}
EXACT_TYPES = {ValueType.PATH, ValueType.ID, ValueType.ENUM, ValueType.STRING}


class Cardinality(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


# Confidence by source kind. Structured sources are ground truth; extraction is not.
SOURCE_CONFIDENCE = {
    "tool_call": 1.0,
    "failure_triggered": 1.0,  # only committed after a re-probe against reality
    "manual": 1.0,
    "llm_extract": 0.8,
    "heuristic_extract": 0.6,
}


def default_confidence(source: str) -> float:
    return SOURCE_CONFIDENCE.get(source.split(":", 1)[0], 0.7)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class TypedValue:
    type: ValueType
    data: Any  # JSON-serialisable; dates stored as ISO strings, multi slots as lists

    def to_json(self) -> dict:
        return {"type": self.type.value, "data": self.data}

    @staticmethod
    def from_json(obj: dict) -> "TypedValue":
        return TypedValue(ValueType(obj["type"]), obj["data"])


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PATH_RE = re.compile(r"^(~|\.{1,2})?/?[\w.\-]+(/[\w.\-]+)+/?$|^/[\w.\-]+$")


def infer_type(raw: Any) -> ValueType:
    if isinstance(raw, bool):
        return ValueType.ENUM
    if isinstance(raw, int):
        return ValueType.INT
    if isinstance(raw, float):
        return ValueType.FLOAT
    if isinstance(raw, (date, datetime)):
        return ValueType.DATE
    s = str(raw).strip()
    if _INT_RE.match(s):
        return ValueType.INT
    if _FLOAT_RE.match(s):
        return ValueType.FLOAT
    if _DATE_RE.match(s):
        return ValueType.DATE
    if " " not in s and _PATH_RE.match(s):
        return ValueType.PATH
    if " " in s or len(s) > 64:
        return ValueType.TEXT
    return ValueType.STRING


def coerce(raw: Any, vtype: ValueType) -> Any:
    """Coerce a raw value into the canonical JSON representation for `vtype`.

    Raises ValueError if the value cannot represent that type.
    """
    if vtype == ValueType.INT:
        if isinstance(raw, bool):
            raise ValueError("bool is not int")
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"{raw!r} is not an integer")
        return int(str(raw).strip()) if not isinstance(raw, (int, float)) else int(raw)
    if vtype == ValueType.FLOAT:
        return float(raw)
    if vtype == ValueType.DATE:
        if isinstance(raw, datetime):
            return raw.date().isoformat()
        if isinstance(raw, date):
            return raw.isoformat()
        return date.fromisoformat(str(raw).strip()[:10]).isoformat()
    if vtype == ValueType.PATH:
        s = str(raw).strip()
        return s.rstrip("/") if len(s) > 1 else s
    if vtype == ValueType.ENUM:
        return str(raw).strip().lower() if not isinstance(raw, bool) else str(raw).lower()
    return str(raw).strip()


def make_value(raw: Any, vtype: ValueType | str | None = None) -> TypedValue:
    if isinstance(raw, TypedValue):
        return raw
    if isinstance(raw, dict) and set(raw) == {"type", "data"}:
        return TypedValue(ValueType(raw["type"]), coerce(raw["data"], ValueType(raw["type"])))
    t = ValueType(vtype) if vtype else infer_type(raw)
    return TypedValue(t, coerce(raw, t))


@dataclass
class Slot:
    entity_id: str
    predicate: str
    value: TypedValue
    cardinality: Cardinality = Cardinality.SINGLE
    version: int = 1
    updated_at: str = field(default_factory=now_iso)
    source: str = "manual"
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "predicate": self.predicate,
            "value": self.value.to_json(),
            "cardinality": self.cardinality.value,
            "version": self.version,
            "updated_at": self.updated_at,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class Correction:
    entity_id: str
    predicate: str
    kind: str  # insert | correction | append | reconcile
    old_value: TypedValue | None
    new_value: TypedValue
    error_magnitude: float
    delta: float | None  # signed numeric delta where meaningful
    version_from: int
    version_to: int
    source: str
    reason: str
    timestamp: str = field(default_factory=now_iso)
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["old_value"] = self.old_value.to_json() if self.old_value else None
        d["new_value"] = self.new_value.to_json()
        return d


@dataclass
class Pending:
    entity_id: str
    predicate: str
    proposed_value: TypedValue
    read_version: int
    current_version: int
    source: str
    confidence: float
    reason: str
    current_value: TypedValue | None = None  # the value that won, so one row holds both candidates
    status: str = "open"  # open | accepted | rejected | superseded
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["proposed_value"] = self.proposed_value.to_json()
        d["current_value"] = self.current_value.to_json() if self.current_value else None
        return d
