"""Failure-triggered correction (the Memorable hook).

A procedure that used to pass fails -> treat it as an implicit error signal, but never
trust the failure itself as truth: re-probe reality first, and only commit if the
probe disagrees with memory. A flake re-probes to the same value and commits nothing.

`infer_dependency` is a registry, hardcoded per procedure step. Generalising it (e.g.
tracing which slots a step read) is future work; for the demo, register the mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .engine import DeltaMemory

Probe = Callable[[Any], Any]  # current (possibly stale) value -> ground truth, or None if unknown


@dataclass
class Dependency:
    entity_id: str
    predicate: str
    probe: Probe


class DependencyRegistry:
    def __init__(self):
        self._deps: dict[tuple[str, str], Dependency] = {}

    def register(self, procedure_id: str, step: str, entity_id: str, predicate: str, probe: Probe):
        self._deps[(procedure_id, step)] = Dependency(entity_id, predicate, probe)

    def infer_dependency(self, procedure_id: str, step: str) -> Dependency | None:
        return self._deps.get((procedure_id, step))


def on_procedure_failure(dm: DeltaMemory, registry: DependencyRegistry, procedure_id: str,
                         failed_step: str, detail: str = "") -> dict:
    dep = registry.infer_dependency(procedure_id, failed_step)
    if dep is None:
        return {"status": "unmapped", "message": f"no dependency registered for {procedure_id}/{failed_step}"}
    slot = dm.read(dep.entity_id, dep.predicate)
    if slot is None:
        return {"status": "unknown_slot", "message": f"{dep.entity_id}.{dep.predicate} not in memory"}

    truth = dep.probe(slot.value.data)
    if truth is None:
        return {"status": "probe_inconclusive", "slot": slot.to_dict(),
                "message": "re-probe found no ground truth; memory left unchanged"}
    if truth == slot.value.data:
        return {"status": "flake", "slot": slot.to_dict(),
                "message": "re-probe agrees with memory; failure not caused by this slot"}

    # Commit against the version we probed from, so a concurrent fix isn't clobbered.
    r = dm.write(slot.entity_id, slot.predicate, truth, source=f"failure_triggered:{procedure_id}",
                 value_type=slot.value.type.value, expected_version=slot.version)
    out = r.to_dict()
    out["trigger"] = {"procedure": procedure_id, "step": failed_step, "detail": detail}
    return out


# ------------------------------------------------------------ reusable probes
def find_file_probe(root: str | Path, filename: str | None = None) -> Probe:
    """Ground truth for a `path` slot: does it exist? If not, where did the file go?"""
    root = Path(root)

    def probe(assumed: str) -> str | None:
        if (root / assumed).exists():
            return assumed
        name = filename or Path(assumed).name
        hits = sorted(p for p in root.rglob(name) if p.is_file())
        if len(hits) == 1:
            return hits[0].relative_to(root).as_posix()
        return None  # zero or ambiguous: don't guess

    return probe
