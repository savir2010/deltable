"""Optional mirror of committed slots into GBrain as markdown pages.

The typed tables are the source of truth for slots. This hook additionally writes one
page per entity into a GBrain brain directory so GBrain's normal retrieval (search,
chat, other agents that only speak GBrain) sees the corrected values and the audit
trail. Point DELTA_MEMORY_GBRAIN_DIR at the brain repo; GBrain's own sync/import picks
the pages up. This is the one integration seam that assumes GBrain's markdown-page
ingestion; adjust the path/frontmatter to match your GBrain setup.
"""
from __future__ import annotations

import json
from pathlib import Path

from .store.base import Store
from .types import Correction, Slot


class MarkdownMirror:
    def __init__(self, store: Store, root: str | Path, subdir: str = "delta-memory", history: int = 10):
        self.store = store
        self.dir = Path(root) / subdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.history = history

    def __call__(self, slot: Slot, log: Correction) -> None:
        self.render(slot.entity_id)

    def render(self, entity_id: str) -> Path:
        slots = [self.store.get_slot(entity_id, p) for p in self.store.list_predicates(entity_id)]
        lines = [
            "---",
            f"title: {entity_id}",
            "type: delta-memory-entity",
            f"slots: {json.dumps({s.predicate: s.value.data for s in slots if s})}",
            "---",
            f"# {entity_id}",
            "",
            "| predicate | value | type | version | source | confidence | updated |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in slots:
            if s:
                lines.append(f"| {s.predicate} | `{s.value.data}` | {s.value.type.value} | v{s.version} "
                             f"| {s.source} | {s.confidence:.2f} | {s.updated_at} |")
        lines += ["", "## Recent corrections", ""]
        logs = sorted((c for s in slots if s for c in self.store.history(entity_id, s.predicate)),
                      key=lambda c: c.id or 0)[-self.history:]
        for c in reversed(logs):
            lines.append(f"- {c.timestamp} v{c.version_from}->v{c.version_to} {c.reason}")
        path = self.dir / f"{entity_id}.md"
        path.write_text("\n".join(lines) + "\n")
        return path
