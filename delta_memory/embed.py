"""Embeddings used in exactly two places: key resolution (write time) and text-slot error.

The default embedder is a deterministic character n-gram hasher: zero dependencies,
zero network, stable across runs, which is what you want for a demo and for tuning
thresholds against fixed test cases. Swap in a real model (e.g. whatever GBrain is
configured with) by implementing `Embedder.embed`.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Protocol

Vector = dict[int, float]  # sparse


class Embedder(Protocol):
    def embed(self, text: str) -> Vector: ...


_SPLIT = re.compile(r"[\s_\-./:]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_key(text: str) -> str:
    """`refundPolicy.window_days` -> `refund policy window days`."""
    text = _CAMEL.sub(" ", text)
    return " ".join(t for t in _SPLIT.split(text.lower()) if t)


class NgramEmbedder:
    """Character trigrams + whole-word tokens, hashed into a sparse vector."""

    def __init__(self, n: int = 3, word_weight: float = 1.0):
        self.n = n
        self.word_weight = word_weight

    def embed(self, text: str) -> Vector:
        return _ngram_embed(normalize_key(text), self.n, self.word_weight)


def stem(word: str) -> str:
    """Crude plural folding: policies->policy, scripts->script, days->day. Not Porter."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@lru_cache(maxsize=8192)
def _ngram_embed(norm: str, n: int, word_weight: float) -> Vector:
    feats: Counter[str] = Counter()
    for word in map(stem, norm.split()):
        padded = f"#{word}#"
        for i in range(max(1, len(padded) - n + 1)):
            feats["c:" + padded[i : i + n]] += 1.0
        feats["w:" + word] += word_weight
    vec: Vector = {}
    for f, w in feats.items():
        h = hash_feature(f)
        vec[h] = vec.get(h, 0.0) + w
    return vec


def hash_feature(f: str) -> int:
    # FNV-1a, stable across processes (unlike built-in hash()).
    h = 0x811C9DC5
    for b in f.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def cosine(a: Vector, b: Vector) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def similarity(embedder: Embedder, x: str, y: str) -> float:
    return cosine(embedder.embed(x), embedder.embed(y))
