"""Stage 2: key resolution. Decides whether a write is even in correction territory.

Entity first, then predicate *within* that entity, so `refund_policy.window_days`
never collides with `refund_policy.max_amount` just because they share an entity.
Embedding search runs here, at write time, and never on the read path.
"""
from __future__ import annotations

from dataclasses import dataclass

from .embed import Embedder, normalize_key, similarity
from .store.base import Store
from .types import Slot, TypedValue

# Hand-tuned with scripts/tune_thresholds.py against the NgramEmbedder: positives >= 0.782,
# negatives <= 0.707, so 0.75 sits in the gap. Not learned; re-tune if you swap embedders.
THETA_ENTITY = 0.75
THETA_PREDICATE = 0.75


def slugify(hint: str) -> str:
    return "_".join(normalize_key(hint).split())


@dataclass
class Resolution:
    entity_id: str
    predicate: str
    slot: Slot | None          # None => NEW_SLOT, pure insert, no correction possible
    entity_via: str            # exact | alias | similar | value_match | new
    predicate_via: str
    entity_score: float = 1.0
    predicate_score: float = 1.0

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id, "predicate": self.predicate,
            "entity_via": self.entity_via, "predicate_via": self.predicate_via,
            "entity_score": round(self.entity_score, 3),
            "predicate_score": round(self.predicate_score, 3),
            "new_slot": self.slot is None,
        }


def _best(embedder: Embedder, hint: str, candidates: dict[str, str]) -> tuple[str | None, float]:
    """candidates: surface text -> canonical id. Returns (canonical, score)."""
    best_id, best = None, 0.0
    for text, target in candidates.items():
        s = similarity(embedder, hint, text)
        if s > best:
            best_id, best = target, s
    return best_id, best


def _resolve_one(store: Store, embedder: Embedder, kind: str, scope: str, hint: str,
                 known: list[str], theta: float, learn: bool) -> tuple[str, str, float]:
    slug = slugify(hint)
    if slug in known:
        return slug, "exact", 1.0
    aliases = store.get_aliases(kind, scope)
    if slug in aliases:
        return aliases[slug], "alias", 1.0
    cands = {k: k for k in known} | {a: t for a, t in aliases.items() if t in known}
    target, score = _best(embedder, slug, cands)
    if target is not None and score >= theta:
        if learn:
            store.add_alias(kind, scope, slug, target)
        return target, "similar", score
    return slug, "new", score


def resolve_key(store: Store, embedder: Embedder, entity_hint: str, predicate_hint: str,
                match_value: TypedValue | None = None, learn_aliases: bool = True,
                theta_entity: float = THETA_ENTITY, theta_predicate: float = THETA_PREDICATE) -> Resolution:
    # Structured rename events know the *old value*; that is a better key than any string.
    if match_value is not None:
        hits = store.find_by_value(match_value, predicate=slugify(predicate_hint) or None)
        if len(hits) == 1:
            s = hits[0]
            return Resolution(s.entity_id, s.predicate, s, "value_match", "value_match")

    entity, e_via, e_score = _resolve_one(
        store, embedder, "entity", "", entity_hint, store.list_entities(), theta_entity, learn_aliases)
    if e_via == "new":
        return Resolution(entity, slugify(predicate_hint), None, "new", "new", e_score, 0.0)

    predicate, p_via, p_score = _resolve_one(
        store, embedder, "predicate", entity, predicate_hint, store.list_predicates(entity),
        theta_predicate, learn_aliases)
    slot = None if p_via == "new" else store.get_slot(entity, predicate)
    return Resolution(entity, predicate, slot, e_via, p_via, e_score, p_score)


def key_similarity(embedder: Embedder, a: str, b: str) -> float:
    """Exposed for threshold tuning (scripts/tune_thresholds.py)."""
    return similarity(embedder, slugify(a), slugify(b))
