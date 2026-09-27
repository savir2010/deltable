"""Composable transitions: a delta-rule associative memory over procedure states.

What this is, precisely
-----------------------
A *linear associative memory* updated with the delta rule

    S <- S + beta * (v - S k) k^T

the same write discipline DeltaNet uses (query the current association S k, compute the
error against the target v, write only the correction). It is NOT a trained DeltaNet:
there is no network, no learned projection, no gradient descent. States are fixed
near-orthogonal random vectors; the only "learning" is the closed-form delta update.

Why composition works here (and why it usually doesn't)
-------------------------------------------------------
Storing A->B and B->C as k_A -> v_B and k_B -> v_C does not make S k_A return C:
single-hop recall gives B, and that is all. Retrieval is not implication.

Composition becomes possible because values are drawn from the SAME space as keys
(v_B = k_B). Then S maps states to states, so applying it twice is meaningful:

    S (S k_A) ~= S k_B ~= k_C        i.e.  T_AC = T_BC o T_AB = S @ S

That constraint is imposed by design, not learned. A model that has to *learn* the
algebra of composition (the research question) is out of scope; this module shows the
mechanism the idea relies on and its limits (interference grows with the number of
stored transitions relative to the dimension d; see `capacity_curve`).

Conditional relations ("A -x-> B, B -y-> C") use one matrix per context, and compose
as S_y @ S_x. Composing in the wrong context fails, which is the point.

Episodic evidence stays recoverable
-----------------------------------
Transitions are stored as ordinary Delta Memory slots (entity `tr_<context>_<state>`,
predicate `next`), so each change is a typed, versioned correction with its source
episode. S is rebuilt by replaying that log in order through the delta rule: the
compressed composable state and the episodes that produced it come from one record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .embed import hash_feature
from .engine import DeltaMemory
from .resolve import slugify

PREFIX = "tr"
PREDICATE = "next"


# ------------------------------------------------------------------ state space
class StateSpace:
    """Name -> fixed unit vector in R^d. Deterministic across processes."""

    def __init__(self, d: int = 512, seed: int = 7):
        self.d, self.seed = d, seed
        self._vecs: dict[str, np.ndarray] = {}

    def vec(self, name: str) -> np.ndarray:
        if name not in self._vecs:
            rng = np.random.default_rng((hash_feature(name) << 8) ^ self.seed)
            v = rng.standard_normal(self.d)
            self._vecs[name] = v / np.linalg.norm(v)
        return self._vecs[name]

    def decode(self, x: np.ndarray) -> tuple[str | None, float, float]:
        """Nearest known state by PROJECTION <v_state, x>: (name, score, margin).

        Not cosine: for a key with no stored association, S k is small crosstalk, and
        normalising it would make noise look like a confident match. A fitted association
        projects at ~1.0; an unrelated key at ~1/sqrt(d)."""
        if np.linalg.norm(x) < 1e-9 or not self._vecs:
            return None, 0.0, 0.0
        names = list(self._vecs)
        sims = np.stack([self._vecs[k] for k in names]) @ x
        order = np.argsort(sims)[::-1]
        second = float(sims[order[1]]) if len(order) > 1 else 0.0
        return names[order[0]], float(sims[order[0]]), float(sims[order[0]]) - second


# ---------------------------------------------------------- associative memory
@dataclass
class Hop:
    src: str
    dst: str
    context: str
    score: float
    episodes: list[str]


@dataclass
class AssociativeMemory:
    space: StateSpace = field(default_factory=StateSpace)
    tol: float = 1e-6
    S: dict[str, np.ndarray] = field(default_factory=dict)          # context -> d x d
    evidence: dict[tuple[str, str], list[dict]] = field(default_factory=dict)  # (ctx, src) -> log

    def _S(self, ctx: str) -> np.ndarray:
        if ctx not in self.S:
            self.S[ctx] = np.zeros((self.space.d, self.space.d))
        return self.S[ctx]

    def write(self, src: str, dst: str, context: str = "default", beta: float = 1.0,
              episode: str = "unknown") -> dict:
        """Delta rule: S += beta (v - S k) k^T. Returns the error that was corrected."""
        k, v = self.space.vec(src), self.space.vec(dst)
        S = self._S(context)
        err = v - S @ k
        mag = float(np.linalg.norm(err))
        self.evidence.setdefault((context, src), []).append(
            {"dst": dst, "episode": episode, "error": round(mag, 4), "beta": beta})
        if mag <= self.tol:
            return {"status": "noop", "error": mag}
        S += beta * np.outer(err, k)  # k is unit-norm, so beta=1 fits this key exactly
        return {"status": "written", "error": mag}

    def recall(self, src: str, context: str = "default") -> tuple[str | None, float]:
        """ONE hop. This is all plain associative retrieval gives you."""
        if context not in self.S:
            return None, 0.0
        name, score, _ = self.space.decode(self.S[context] @ self.space.vec(src))
        return name, score

    def operator(self, contexts: list[str]) -> np.ndarray:
        """Composite transform for a sequence of contexts: S_cn @ ... @ S_c1."""
        T = np.eye(self.space.d)
        for c in contexts:
            T = self._S(c) @ T
        return T

    def apply(self, src: str, contexts: list[str]) -> tuple[str | None, float]:
        """Decode T k_src with NO clean-up between hops: tests T_AC = S_BC S_AB directly."""
        name, score, _ = self.space.decode(self.operator(contexts) @ self.space.vec(src))
        return name, score

    def _hop(self, src: str, ctx: str, theta: float) -> Hop | None:
        name, score = self.recall(src, ctx)
        if name is None or score < theta or name == src:
            return None
        eps = list(dict.fromkeys(e["episode"] for e in self.evidence.get((ctx, src), []) if e["dst"] == name))
        return Hop(src, name, ctx, score, eps)

    def compose(self, start: str, goal: str | None = None, contexts: list[str] | None = None,
                max_hops: int = 8, theta: float = 0.5) -> list[Hop]:
        """Multi-hop composition with clean-up (snap to the nearest state each hop, so
        noise doesn't accumulate). With `goal`, breadth-first over contexts."""
        ctxs = contexts or sorted(self.S)
        if goal is None:  # follow the chain in a single context
            path, cur, seen = [], start, {start}
            for _ in range(max_hops):
                h = self._hop(cur, ctxs[0], theta)
                if h is None or h.dst in seen:
                    break
                path.append(h); seen.add(h.dst); cur = h.dst
            return path
        frontier, seen = [(start, [])], {start}
        for _ in range(max_hops):
            nxt = []
            for cur, path in frontier:
                for c in ctxs:
                    h = self._hop(cur, c, theta)
                    if h is None or h.dst in seen:
                        continue
                    if h.dst == goal:
                        return path + [h]
                    seen.add(h.dst); nxt.append((h.dst, path + [h]))
            frontier = nxt
        return []


# --------------------------------------------------- storage in Delta Memory
def norm_state(name: str) -> str:
    return slugify(name)


def norm_context(ctx: str) -> str:
    """Contexts are one alphanumeric token so tr_<ctx>_<state> decodes unambiguously."""
    c = re.sub(r"[^a-z0-9]", "", ctx.lower())
    if not c:
        raise ValueError(f"context {ctx!r} has no alphanumeric characters")
    return c


def prefix(namespace: str = "") -> str:
    """Slot-id prefix. A namespace isolates a set of transitions (e.g. one demo run):
    its matrices are rebuilt only from its own log, so nothing else interferes."""
    ns = re.sub(r"[^a-z0-9]", "", namespace.lower())
    return f"{PREFIX}{ns}_"


def _entity(context: str, state: str, namespace: str = "") -> str:
    return f"{prefix(namespace)}{norm_context(context)}_{norm_state(state)}"


def record_transition(dm: DeltaMemory, src: str, dst: str, context: str = "default",
                      episode: str = "manual", expected_version: int | None = None,
                      confidence: float | None = None, namespace: str = ""):
    """Store A->B as slot tr<ns>_<ctx>_<A>.next = B. A changed B is a typed correction."""
    return dm.write(_entity(context, src, namespace), PREDICATE, norm_state(dst), value_type="id",
                    source=f"episode:{episode}", expected_version=expected_version,
                    confidence=confidence, exact_key=True)


def _transition_slots(dm: DeltaMemory, namespace: str = ""):
    pre = prefix(namespace)
    for e in dm.store.list_entities():
        if not e.startswith(pre):
            continue
        for p in dm.store.list_predicates(e):
            if p == PREDICATE:
                yield e


def load(dm: DeltaMemory, space: StateSpace | None = None, namespace: str = "") -> AssociativeMemory:
    """Rebuild S by replaying every transition correction, in commit order, through the
    delta rule. Stateless: any process rebuilds the same S from the same log."""
    mem = AssociativeMemory(space or StateSpace())
    events = []
    pre = prefix(namespace)
    for entity in _transition_slots(dm, namespace):
        for c in dm.store.history(entity, PREDICATE):
            events.append((c.id or 0, entity, c))
    for _, entity, c in sorted(events, key=lambda t: t[0]):
        ctx, _, src = entity[len(pre):].partition("_")
        episode = c.source.split(":", 1)[1] if c.source.startswith("episode:") else c.source
        mem.write(src, str(c.new_value.data), ctx, beta=1.0, episode=episode)
    return mem


def capacity_curve(d: int = 512, counts=(16, 64, 128, 256, 384, 512, 768), seed: int = 1) -> list[dict]:
    """Honest limit: one-hop and two-hop accuracy as random chains fill a d-dim memory."""
    out = []
    for n in counts:
        mem = AssociativeMemory(StateSpace(d=d, seed=seed))
        states = [f"s{i}" for i in range(n + 1)]
        for a, b in zip(states, states[1:]):
            mem.write(a, b)
        one = sum(mem.recall(a)[0] == b for a, b in zip(states, states[1:])) / n
        two = sum(mem.apply(a, ["default", "default"])[0] == c
                  for a, c in zip(states, states[2:])) / max(1, n - 1)
        out.append({"transitions": n, "d": d, "one_hop_acc": round(one, 3), "two_hop_acc": round(two, 3)})
    return out


def association_grid(mem: AssociativeMemory, contexts: list[str], states: list[str] | None = None,
                     power: int = 1) -> dict:
    """<v_j, T k_i> for every pair of known states, T = (S_ctx...)^power. power=1 shows what
    is stored; power=2 shows what composition implies (A->C appears only here)."""
    names = states or list(mem.space._vecs)
    T = np.linalg.matrix_power(mem.operator(contexts), power) if power > 1 else mem.operator(contexts)
    K = np.stack([mem.space.vec(n) for n in names])     # rows: states as keys
    grid = K @ T.T @ K.T                                 # grid[i, j] = <v_j, T k_i>
    return {"states": names, "grid": np.round(grid, 3).tolist(), "power": power, "contexts": contexts}
