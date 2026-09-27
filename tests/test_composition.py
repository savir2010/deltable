import pytest

from delta_memory import composition as comp
from delta_memory.composition import AssociativeMemory, StateSpace


def mem_with(*edges, context="default"):
    m = AssociativeMemory(StateSpace(d=512))
    for a, b in edges:
        m.write(a, b, context)
    return m


def test_retrieval_is_not_implication():
    m = mem_with(("build", "container"), ("container", "kubernetes"))
    assert m.recall("build")[0] == "container"          # one hop: B
    assert m.recall("build")[0] != "kubernetes"         # NOT C


def test_composite_operator_is_matrix_product():
    m = mem_with(("build", "container"), ("container", "kubernetes"))
    name, score = m.apply("build", ["default", "default"])  # (S @ S) k_build, no clean-up
    assert name == "kubernetes" and score > 0.9


def test_compose_to_goal_returns_path_and_evidence():
    m = AssociativeMemory(StateSpace())
    m.write("build", "container", episode="deploy-service-x")
    m.write("container", "kubernetes", episode="deploy-k8s")
    path = m.compose("build", goal="kubernetes")
    assert [(h.src, h.dst) for h in path] == [("build", "container"), ("container", "kubernetes")]
    assert [h.episodes for h in path] == [["deploy-service-x"], ["deploy-k8s"]]


def test_delta_write_is_error_correcting_and_noops_restatement():
    m = mem_with(("container", "kubernetes"))
    assert m.write("container", "kubernetes")["status"] == "noop"  # error ~ 0
    r = m.write("container", "nomad")
    assert r["status"] == "written" and r["error"] > 1.0          # ||k_nomad - k_k8s|| ~ sqrt(2)
    assert m.recall("container")[0] == "nomad"


def test_correction_reroutes_composition():
    m = mem_with(("build", "container"), ("container", "kubernetes"))
    m.write("container", "nomad")
    assert m.apply("build", ["default", "default"])[0] == "nomad"


def test_conditional_composition_order_matters():
    m = AssociativeMemory(StateSpace())
    m.write("a", "b", "x")
    m.write("b", "c", "y")
    assert m.apply("a", ["x", "y"])[0] == "c"          # S_y @ S_x
    name, score = m.apply("a", ["y", "x"])              # wrong order: nothing there
    assert name != "c" or score < 0.3
    assert [h.context for h in m.compose("a", goal="c")] == ["x", "y"]


def test_long_chain_with_cleanup():
    states = list("abcdefgh")
    m = mem_with(*zip(states, states[1:]))
    assert [h.dst for h in m.compose("a")] == states[1:]
    assert m.apply("a", ["default"] * 7)[0] == "h"      # S^7 k_a without clean-up


def test_capacity_degrades_past_dimension():
    curve = {r["transitions"]: r for r in comp.capacity_curve(d=128, counts=(16, 512))}
    assert curve[16]["two_hop_acc"] == 1.0
    assert curve[512]["one_hop_acc"] < 0.9               # interference is real; say so


def test_round_trip_through_delta_memory(dm):
    comp.record_transition(dm, "build", "container", episode="deploy-service-x")
    comp.record_transition(dm, "container", "kubernetes", episode="deploy-k8s")
    comp.record_transition(dm, "container", "nomad", episode="migrate-to-nomad")  # a correction
    hist = dm.history("tr_default_container", "next")
    assert [c.kind for c in hist] == ["insert", "correction"]
    assert hist[0].new_value.data == "kubernetes"         # old episode still recoverable

    m = comp.load(dm)                                      # S rebuilt by replaying the log
    path = m.compose("build", goal="nomad")
    assert [h.dst for h in path] == ["container", "nomad"]
    assert path[1].episodes == ["migrate-to-nomad"]
    assert m.compose("build", goal="kubernetes") == []     # superseded, not composed


def test_transition_keys_never_fuzzy_merge(dm):
    comp.record_transition(dm, "build", "container")
    r = comp.record_transition(dm, "builds", "artifact")
    assert r.status == "inserted" and r.slot.entity_id == "tr_default_builds"


def test_context_must_be_alphanumeric():
    with pytest.raises(ValueError):
        comp.norm_context("__")


def test_unassociated_key_does_not_decode_confidently():
    # regression: cosine-normalised crosstalk once decoded build -[eu]-> eu_cluster at 1.0
    m = AssociativeMemory(StateSpace())
    m.write("build", "arm_image", "arm")
    m.write("arm_image", "eu_cluster", "eu")
    name, score = m.recall("build", "eu")
    assert score < 0.2
    assert [h.context for h in m.compose("build", goal="eu_cluster")] == ["arm", "eu"]
    assert m.compose("build", goal="eu_cluster", contexts=["eu"]) == []


def test_namespaces_isolate_runs(dm):
    comp.record_transition(dm, "build", "container", namespace="run1")
    comp.record_transition(dm, "build", "tarball", namespace="run2")
    comp.record_transition(dm, "build", "wheel")                         # default namespace
    assert comp.load(dm, namespace="run1").recall("build")[0] == "container"
    assert comp.load(dm, namespace="run2").recall("build")[0] == "tarball"
    assert comp.load(dm).recall("build")[0] == "wheel"
    assert len(comp.load(dm, namespace="run1").evidence) == 1           # nothing else replayed


def test_episodes_listed_once_per_hop():
    m = AssociativeMemory(StateSpace())
    for _ in range(3):
        m.write("a", "b", episode="ep1")
    assert m.compose("a", goal="b")[0].episodes == ["ep1"]


def test_association_grid_shows_composition_only_at_power_two():
    m = AssociativeMemory(StateSpace())
    m.write("a", "b"); m.write("b", "c")
    g1 = comp.association_grid(m, ["default"], ["a", "b", "c"], power=1)["grid"]
    g2 = comp.association_grid(m, ["default"], ["a", "b", "c"], power=2)["grid"]
    assert g1[0][1] > 0.9 and abs(g1[0][2]) < 0.2    # S: a->b stored, a->c not
    assert g2[0][2] > 0.9                            # S@S: a->c implied
