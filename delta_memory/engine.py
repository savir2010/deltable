"""The write path (stages 2-5), the read path, history and reconciliation.

Stateless: every call reads what it needs from the store and commits with a
per-slot compare-and-swap. Two agents writing different slots never contend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .embed import Embedder, NgramEmbedder
from .errors import ErrorResult, compute_error, describe
from .resolve import Resolution, resolve_key, slugify
from .store.base import Store
from .types import (Cardinality, Correction, Pending, Slot, TypedValue, ValueType, coerce,
                    default_confidence, make_value)

CommitHook = Callable[[Slot, Correction], None]


@dataclass
class WriteResult:
    status: str  # inserted | corrected | appended | noop | conflict | needs_review
    resolution: Resolution
    slot: Slot | None = None
    correction: Correction | None = None
    pending_id: int | None = None
    error: ErrorResult | None = None
    message: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"status": self.status, "message": self.message,
                             "resolution": self.resolution.to_dict()}
        if self.slot:
            d["slot"] = self.slot.to_dict()
        if self.correction:
            d["correction"] = self.correction.to_dict()
        if self.pending_id is not None:
            d["pending_id"] = self.pending_id
        if self.error:
            d["error"] = {"magnitude": self.error.magnitude, "delta": self.error.delta,
                          "comparator": self.error.comparator}
        return d


@dataclass
class DeltaMemory:
    store: Store
    embedder: Embedder = field(default_factory=NgramEmbedder)
    hooks: list[CommitHook] = field(default_factory=list)

    # ------------------------------------------------------------------ write
    def write(self, entity_hint: str, predicate_hint: str, value: Any, source: str = "manual",
              value_type: str | None = None, cardinality: str = "single",
              confidence: float | None = None, expected_version: int | None = None,
              match_value: Any = None, exact_key: bool = False, _retries: int = 3) -> WriteResult:
        conf = default_confidence(source) if confidence is None else float(confidence)
        mv = make_value(match_value, value_type) if match_value is not None else None

        # Stage 2: key resolution
        # exact_key: machine-generated ids (e.g. transition slots) must never fuzzy-match.
        theta = {"theta_entity": 1.01, "theta_predicate": 1.01, "learn_aliases": False} if exact_key else {}
        res = resolve_key(self.store, self.embedder, entity_hint, predicate_hint, match_value=mv, **theta)
        slot = res.slot
        new = self._typed(value, value_type, slot)

        if slot is None:
            return self._insert(res, new, Cardinality(cardinality), source, conf, _retries,
                                (entity_hint, predicate_hint, value, value_type, cardinality,
                                 confidence, expected_version, match_value, exact_key))

        if slot.cardinality == Cardinality.MULTI:
            return self._append(res, slot, new, source, conf, _retries)

        # Stage 3: typed error against the version the caller actually saw
        read_version = slot.version if expected_version is None else int(expected_version)
        err = compute_error(new, slot.value, self.embedder)

        # Stage 4: decision
        if err.is_restatement(new.type):
            return WriteResult("noop", res, slot, error=err,
                               message=f"restatement of {slot.entity_id}.{slot.predicate} v{slot.version}")
        if read_version != slot.version:
            return self._conflict(res, slot, new, read_version, source, conf, err,
                                  f"stale read: caller saw v{read_version}, slot is at v{slot.version}")
        if conf < slot.confidence:
            return self._needs_review(res, slot, new, source, conf, err)

        # Stage 5: commit (CAS)
        log = Correction(slot.entity_id, slot.predicate, "correction", slot.value, new,
                         err.magnitude, err.delta, read_version, read_version + 1, source,
                         describe(slot.entity_id, slot.predicate, slot.value, new, err, source))
        committed = self.store.cas_update(slot.entity_id, slot.predicate, read_version, new,
                                          source, conf, log)
        if committed is None:
            current = self.store.get_slot(slot.entity_id, slot.predicate)
            if current and current.value == new:
                return WriteResult("noop", res, current, error=err,
                                   message="lost the race to an identical write (convergent)")
            return self._conflict(res, current or slot, new, read_version, source, conf, err,
                                  "version moved between read and commit")
        self._fire(committed, log)
        return WriteResult("corrected", res, committed, log, error=err, message=log.reason)

    def _typed(self, value: Any, value_type: str | None, slot: Slot | None) -> TypedValue:
        if value_type or slot is None:
            return make_value(value, value_type)
        # Existing slot: its type drives interpretation of the incoming value.
        try:
            raw = value["data"] if isinstance(value, dict) and "data" in value else value
            return TypedValue(slot.value.type, coerce(raw, slot.value.type))
        except (ValueError, TypeError):
            return make_value(value)  # genuine type change; compute_error flags it

    def _insert(self, res, new, cardinality, source, conf, retries, args) -> WriteResult:
        data = [new.data] if cardinality == Cardinality.MULTI else new.data
        value = TypedValue(new.type, data)
        slot = Slot(res.entity_id, res.predicate, value, cardinality, 1, source=source, confidence=conf)
        log = Correction(res.entity_id, res.predicate, "insert", None, value, 0.0, None, 0, 1,
                         source, f"{res.entity_id}.{res.predicate} created = {data!r} via {source}")
        if self.store.insert_slot(slot, log):
            self._fire(slot, log)
            return WriteResult("inserted", res, slot, log, message=log.reason)
        if retries <= 0:
            raise RuntimeError("insert race did not settle")
        # Someone created it first; replay as a write against the existing slot.
        eh, ph, v, vt, card, c, ev, mv, ek = args
        return self.write(eh, ph, v, source, vt, card, c, ev, mv, ek, _retries=retries - 1)

    def _append(self, res, slot, new, source, conf, retries) -> WriteResult:
        # Set semantics: appends commute, so a lost CAS is safely retried, never queued.
        for _ in range(retries + 1):
            if new.data in slot.value.data:
                return WriteResult("noop", res, slot, message="already a member of multi slot")
            merged = TypedValue(slot.value.type, [*slot.value.data, new.data])
            log = Correction(slot.entity_id, slot.predicate, "append", slot.value, merged, 0.0, None,
                             slot.version, slot.version + 1, source,
                             f"{slot.entity_id}.{slot.predicate} += {new.data!r} via {source}")
            committed = self.store.cas_update(slot.entity_id, slot.predicate, slot.version, merged,
                                              source, max(conf, slot.confidence), log)
            if committed:
                self._fire(committed, log)
                return WriteResult("appended", res, committed, log, message=log.reason)
            slot = self.store.get_slot(slot.entity_id, slot.predicate)
        raise RuntimeError("append contention did not settle")

    def _conflict(self, res, slot, new, read_version, source, conf, err, why) -> WriteResult:
        p = Pending(slot.entity_id, slot.predicate, new, read_version, slot.version, source, conf,
                    f"version_conflict: {why}; current={slot.value.data!r}, proposed={new.data!r}",
                    current_value=slot.value)
        pid = self.store.add_pending(p)
        return WriteResult("conflict", res, slot, pending_id=pid, error=err, message=p.reason)

    def _needs_review(self, res, slot, new, source, conf, err) -> WriteResult:
        p = Pending(slot.entity_id, slot.predicate, new, slot.version, slot.version, source, conf,
                    f"outranked: {source} (conf {conf:.2f}) would overwrite {slot.source} "
                    f"(conf {slot.confidence:.2f}); current={slot.value.data!r}, proposed={new.data!r}",
                    current_value=slot.value)
        pid = self.store.add_pending(p)
        return WriteResult("needs_review", res, slot, pending_id=pid, error=err, message=p.reason)

    def _fire(self, slot: Slot, log: Correction) -> None:
        for h in self.hooks:
            try:
                h(slot, log)
            except Exception:  # a mirror failing must never undo a committed correction
                pass

    # ------------------------------------------------------------------- read
    def read(self, entity_id: str, predicate: str) -> Slot | None:
        """Direct lookup. No similarity search on the read path, ever."""
        slot = self.store.get_slot(entity_id, predicate)
        if slot:
            return slot
        e = slugify(entity_id)
        e = self.store.get_aliases("entity", "").get(e, e)
        p = slugify(predicate)
        p = self.store.get_aliases("predicate", e).get(p, p)
        return self.store.get_slot(e, p)

    def history(self, entity_id: str, predicate: str) -> list[Correction]:
        slot = self.read(entity_id, predicate)
        if slot:
            entity_id, predicate = slot.entity_id, slot.predicate
        return self.store.history(entity_id, predicate)

    # -------------------------------------------------------------- reconcile
    def reconcile(self, entity_id: str | None = None, predicate: str | None = None,
                  accept: int | None = None, reject: int | None = None) -> dict:
        if accept is not None:
            return self._accept(accept)
        if reject is not None:
            p = self.store.get_pending(reject)
            if not p or p.status != "open":
                return {"status": "error", "message": f"no open pending #{reject}"}
            self.store.set_pending_status(reject, "rejected")
            return {"status": "rejected", "pending_id": reject}
        if entity_id and predicate:
            slot = self.read(entity_id, predicate)
            if slot:
                entity_id, predicate = slot.entity_id, slot.predicate
        pend = self.store.list_pending(entity_id, predicate)
        out = []
        for p in pend:
            cur = self.store.get_slot(p.entity_id, p.predicate)
            out.append({**p.to_dict(), "current": cur.to_dict() if cur else None})
        return {"status": "ok", "pending": out}

    def _accept(self, pending_id: int) -> dict:
        p = self.store.get_pending(pending_id)
        if not p or p.status != "open":
            return {"status": "error", "message": f"no open pending #{pending_id}"}
        cur = self.store.get_slot(p.entity_id, p.predicate)
        if cur is None:
            return {"status": "error", "message": "slot no longer exists"}
        if cur.value == p.proposed_value:
            self.store.set_pending_status(pending_id, "superseded")
            return {"status": "noop", "message": "current value already equals proposal",
                    "slot": cur.to_dict()}
        err = compute_error(p.proposed_value, cur.value, self.embedder)
        src = f"reconcile:{p.source}"
        log = Correction(cur.entity_id, cur.predicate, "reconcile", cur.value, p.proposed_value,
                         err.magnitude, err.delta, cur.version, cur.version + 1, src,
                         describe(cur.entity_id, cur.predicate, cur.value, p.proposed_value, err, src)
                         + f" (accepted pending #{pending_id})")
        committed = self.store.cas_update(cur.entity_id, cur.predicate, cur.version,
                                          p.proposed_value, src, p.confidence, log)
        if committed is None:
            return {"status": "conflict", "message": "slot moved again during reconcile; retry"}
        self.store.set_pending_status(pending_id, "accepted")
        self._fire(committed, log)
        return {"status": "accepted", "slot": committed.to_dict(), "correction": log.to_dict()}
