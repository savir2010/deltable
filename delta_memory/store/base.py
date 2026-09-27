"""Storage contract. Delta Memory is stateless logic; the store is GBrain's Postgres.

Every mutating method is atomic and touches exactly one slot row plus one log row
(the rank-one property). `cas_update` is the only way a slot's value changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import Correction, Pending, Slot, TypedValue


class Store(ABC):
    # --- reads -------------------------------------------------------------
    @abstractmethod
    def get_slot(self, entity_id: str, predicate: str) -> Slot | None: ...

    @abstractmethod
    def list_entities(self) -> list[str]: ...

    @abstractmethod
    def list_predicates(self, entity_id: str) -> list[str]: ...

    @abstractmethod
    def find_by_value(self, value: TypedValue, predicate: str | None = None) -> list[Slot]:
        """Slots whose current value equals `value` (used for rename events)."""

    @abstractmethod
    def get_aliases(self, kind: str, scope: str) -> dict[str, str]:
        """alias -> canonical id. kind is 'entity' (scope '') or 'predicate' (scope=entity_id)."""

    @abstractmethod
    def history(self, entity_id: str, predicate: str) -> list[Correction]: ...

    @abstractmethod
    def list_pending(self, entity_id: str | None = None, predicate: str | None = None,
                     status: str = "open") -> list[Pending]: ...

    @abstractmethod
    def get_pending(self, pending_id: int) -> Pending | None: ...

    # --- writes ------------------------------------------------------------
    @abstractmethod
    def add_alias(self, kind: str, scope: str, alias: str, target: str) -> None: ...

    @abstractmethod
    def insert_slot(self, slot: Slot, log: Correction) -> bool:
        """Create a new slot at version 1. False if it already exists (lost a create race)."""

    @abstractmethod
    def cas_update(self, entity_id: str, predicate: str, expected_version: int,
                   new_value: TypedValue, source: str, confidence: float,
                   log: Correction) -> Slot | None:
        """UPDATE ... WHERE version = expected_version. None on version conflict."""

    @abstractmethod
    def add_pending(self, pending: Pending) -> int: ...

    @abstractmethod
    def set_pending_status(self, pending_id: int, status: str) -> None: ...

    def recent_corrections(self, limit: int = 50) -> list[Correction]:
        """Newest-first across all slots (for the live UI)."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass
