"""Stage 1: source adapters. Every write enters as a typed Fact.

tool_call_*   : git diffs, config diffs, API responses -> parsed directly. Zero LLM calls.
llm_extract   : free text (Slack, email, policy doc) -> one LLM call -> same Fact shape,
                confidence < 1.0. Falls back to a regex heuristic when no API access.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from .embed import normalize_key, similarity
from .engine import DeltaMemory, WriteResult
from .types import default_confidence, infer_type


@dataclass
class Fact:
    entity_hint: str
    predicate_hint: str
    value: Any
    source: str
    value_type: str | None = None
    confidence: float | None = None
    match_value: Any = None
    cardinality: str = "single"


def apply(dm: DeltaMemory, facts: Iterable[Fact]) -> list[WriteResult]:
    return [dm.write(f.entity_hint, f.predicate_hint, f.value, source=f.source,
                     value_type=f.value_type, cardinality=f.cardinality,
                     confidence=f.confidence, match_value=f.match_value) for f in facts]


# =========================================================== tool_call adapters
_KV_PATTERNS = [
    re.compile(r'^\s*"?(?P<k>[A-Za-z_][\w.\-]*)"?\s*:\s*(?P<v>.+?)\s*,?\s*$'),  # yaml / json
    re.compile(r"^\s*(?:export\s+)?(?P<k>[A-Za-z_][\w.\-]*)\s*=\s*(?P<v>.+?)\s*$"),  # .env / toml / ini
]


def _parse_kv(line: str) -> tuple[str, Any] | None:
    for pat in _KV_PATTERNS:
        m = pat.match(line)
        if m:
            v = m.group("v").strip()
            if v in ("{", "[", ""):
                return None
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                v = v.strip("'\"")
            if isinstance(v, (dict, list)):
                return None
            return m.group("k"), v
    return None


def _stem(path: str) -> str:
    return PurePosixPath(path).stem


def parse_git_diff(diff: str, source: str = "tool_call:git_diff") -> list[Fact]:
    """Renames -> `<stem>.path` facts keyed by old value; config hunks -> `<file stem>.<key>` facts."""
    facts: list[Fact] = []
    file: str | None = None
    rename_from: str | None = None
    removed: dict[str, Any] = {}
    added: dict[str, Any] = {}

    def flush():
        if file:
            for k, v in added.items():
                if k not in removed or removed[k] != v:
                    facts.append(Fact(_stem(file), k, v, source))
        removed.clear()
        added.clear()

    for line in diff.splitlines():
        if line.startswith("diff --git"):
            flush()
            file, rename_from = None, None
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            if m:
                file = m.group(2)
        elif line.startswith("rename from "):
            rename_from = line[len("rename from "):].strip()
        elif line.startswith("rename to ") and rename_from:
            new_path = line[len("rename to "):].strip()
            facts.append(Fact(_stem(new_path), "path", new_path, source, value_type="path",
                              match_value=rename_from))
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                file = p[2:] if p.startswith("b/") else p
        elif line.startswith("---"):
            continue
        elif line.startswith("-"):
            kv = _parse_kv(line[1:])
            if kv:
                removed[kv[0]] = kv[1]
        elif line.startswith("+"):
            kv = _parse_kv(line[1:])
            if kv:
                added[kv[0]] = kv[1]
    flush()
    return facts


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif not isinstance(v, list):
            out[key] = v
    return out


def from_config_diff(entity: str, before: dict, after: dict,
                     source: str = "tool_call:config_diff") -> list[Fact]:
    b, a = _flatten(before), _flatten(after)
    return [Fact(entity, k, v, source) for k, v in a.items() if b.get(k, object()) != v]


def from_api_response(entity: str, payload: dict, source: str = "tool_call:api") -> list[Fact]:
    return [Fact(entity, k, v, source) for k, v in _flatten(payload).items()]


# ========================================================== llm_extract adapter
class ExtractedFact(BaseModel):
    entity: str = Field(description="snake_case entity id; reuse a known one when it refers to the same thing")
    predicate: str = Field(description="snake_case attribute; reuse a known one when it is the same attribute")
    value: str = Field(description="the new value, bare (e.g. '14' not '14 days')")
    value_type: Literal["int", "float", "date", "path", "id", "enum", "string", "text"]


class Extraction(BaseModel):
    facts: list[ExtractedFact]


_SYSTEM = """You turn messages into typed facts for an agent memory store.
Only extract facts the message asserts as current truth (a change, a new value, a correction).
Ignore questions, speculation, and history ("it used to be"). Put units in the predicate, not
the value (window_days = 14). When a known key below refers to the same thing, use it exactly.
Return an empty list if nothing qualifies."""


class LLMExtractor:
    """One LLM call per unstructured message (Anthropic API)."""

    def __init__(self, model: str | None = None):
        import anthropic  # optional dependency: pip install 'delta-memory[llm]'

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("DELTA_MEMORY_MODEL", "claude-opus-5")

    def extract(self, text: str, known_keys: list[str]) -> list[ExtractedFact] | None:
        keys = "\n".join(f"- {k}" for k in known_keys) or "(none yet)"
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            output_config={"effort": "low"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Known keys:\n{keys}\n\nMessage:\n{text}"}],
            output_format=Extraction,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        return resp.parsed_output.facts


_UNITS = r"(days?|hours?|minutes?|weeks?|months?|percent|%|usd|dollars?)"
_HEURISTICS = [
    re.compile(rf"(?P<subj>[\w\s'-]+?)\s+(?:is|are)\s+now\s+(?P<val>[\w./~-]+)\s*(?P<unit>{_UNITS})?", re.I),
    re.compile(rf"(?:changed|updated|set|moved)\s+(?:the\s+)?(?P<subj>[\w\s'-]+?)\s+(?:from\s+\S+\s+(?:\S+\s+)?)?to\s+(?P<val>[\w./~-]+)\s*(?P<unit>{_UNITS})?", re.I),
    re.compile(r"(?P<subj>[\w\s'-]+?)\s+(?:moved|was renamed|lives)\s+(?:to|at)\s+(?P<val>[\w./~-]+)", re.I),
]
_STOP = {"the", "our", "a", "an", "heads", "up", "fyi", "hey", "so", "and", "now"}


class HeuristicExtractor:
    """Offline fallback: regex for "X is now Y" / "changed X to Y", then snap to a known key."""

    def __init__(self, embedder):
        self.embedder = embedder

    def extract(self, text: str, known_keys: list[str]) -> list[ExtractedFact]:
        out = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            for pat in _HEURISTICS:
                m = pat.search(sentence)
                if not m:
                    continue
                subj = " ".join(w for w in normalize_key(m.group("subj")).split() if w not in _STOP)
                unit = (m.groupdict().get("unit") or "").lower().rstrip("s").replace("%", "percent")
                val = m.group("val").rstrip(".,")
                query = f"{subj} {unit}".strip()
                best, score = None, 0.0
                for k in known_keys:
                    s = similarity(self.embedder, query, k)
                    if s > score:
                        best, score = k, s
                if best and score >= 0.45:
                    entity, predicate = best.split(".", 1)
                else:
                    words = subj.split() or ["unknown"]
                    entity = "_".join(words[:-1] or words)
                    predicate = "_".join(filter(None, [words[-1] if len(words) > 1 else "value",
                                                       unit and unit + "s"]))
                out.append(ExtractedFact(entity=entity, predicate=predicate, value=val,
                                         value_type=infer_type(val).value))
                break
        return out


def _known_keys(dm: DeltaMemory) -> list[str]:
    return [f"{e}.{p}" for e in dm.store.list_entities() for p in dm.store.list_predicates(e)]


def extract_facts(dm: DeltaMemory, text: str, extractor=None) -> list[Fact]:
    keys = _known_keys(dm)
    facts, source = None, "llm_extract"
    if extractor is None:
        extractor = default_extractor()
    if extractor is not None and not isinstance(extractor, HeuristicExtractor):
        try:
            facts = extractor.extract(text, keys)
        except Exception:
            facts = None  # no credentials / network: degrade, don't crash the write path
    if facts is None:
        facts, source = HeuristicExtractor(dm.embedder).extract(text, keys), "heuristic_extract"
    conf = default_confidence(source)
    return [Fact(f.entity, f.predicate, f.value, source, f.value_type, conf) for f in facts]


def default_extractor():
    if os.environ.get("DELTA_MEMORY_OFFLINE"):
        return None
    try:
        return LLMExtractor()
    except Exception:
        return None


def ingest_text(dm: DeltaMemory, text: str, extractor=None) -> list[WriteResult]:
    return apply(dm, extract_facts(dm, text, extractor))


def ingest_git_diff(dm: DeltaMemory, diff: str) -> list[WriteResult]:
    return apply(dm, parse_git_diff(diff))
