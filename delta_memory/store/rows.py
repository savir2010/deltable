"""Row <-> object mapping shared by the SQL backends."""
from __future__ import annotations

import json

from ..types import Cardinality, Correction, Pending, Slot, TypedValue


def dump_value(v: TypedValue | None) -> str | None:
    return None if v is None else json.dumps(v.to_json(), sort_keys=True)


def load_value(s) -> TypedValue | None:
    if s is None:
        return None
    return TypedValue.from_json(s if isinstance(s, dict) else json.loads(s))


def slot_from_row(r) -> Slot:
    return Slot(
        entity_id=r["entity_id"],
        predicate=r["predicate"],
        value=load_value(r["value_json"]),
        cardinality=Cardinality(r["cardinality"]),
        version=r["version"],
        updated_at=str(r["updated_at"]),
        source=r["source"],
        confidence=r["confidence"],
    )


def correction_from_row(r) -> Correction:
    return Correction(
        id=r["id"],
        entity_id=r["entity_id"],
        predicate=r["predicate"],
        kind=r["kind"],
        old_value=load_value(r["old_value_json"]),
        new_value=load_value(r["new_value_json"]),
        error_magnitude=r["error_magnitude"],
        delta=r["delta"],
        version_from=r["version_from"],
        version_to=r["version_to"],
        source=r["source"],
        reason=r["reason"],
        timestamp=str(r["ts"]),
    )


def pending_from_row(r) -> Pending:
    return Pending(
        id=r["id"],
        entity_id=r["entity_id"],
        predicate=r["predicate"],
        proposed_value=load_value(r["proposed_value_json"]),
        current_value=load_value(r["current_value_json"]) if "current_value_json" in r.keys() else None,
        read_version=r["read_version"],
        current_version=r["current_version"],
        source=r["source"],
        confidence=r["confidence"],
        reason=r["reason"],
        status=r["status"],
        created_at=str(r["created_at"]),
        resolved_at=None if r["resolved_at"] is None else str(r["resolved_at"]),
    )


def correction_params(c: Correction) -> tuple:
    return (
        c.entity_id, c.predicate, c.kind, dump_value(c.old_value), dump_value(c.new_value),
        c.error_magnitude, c.delta, c.version_from, c.version_to, c.source, c.reason, c.timestamp,
    )
