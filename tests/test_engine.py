import threading

import pytest

from delta_memory import DeltaMemory, SqliteStore
from delta_memory.embed import NgramEmbedder
from delta_memory.errors import compute_error
from delta_memory.types import TypedValue, ValueType, infer_type, make_value


# ---------------------------------------------------------------- stage 3
def test_numeric_error_is_signed_delta():
    e = compute_error(make_value(14), make_value(30), NgramEmbedder())
    assert e.magnitude == 16 and e.delta == -16


def test_date_error_in_days():
    e = compute_error(make_value("2026-10-01"), make_value("2026-09-27"), NgramEmbedder())
    assert e.delta == 4


def test_similar_paths_are_fully_different():
    # the whole point of typed dispatch: cosine would call these "close"
    e = compute_error(make_value("scripts/deploy_v2.sh"), make_value("scripts/deploy.sh"), NgramEmbedder())
    assert e.comparator == "exact" and e.magnitude == 1.0


def test_text_uses_embedding_distance():
    a = TypedValue(ValueType.TEXT, "Refunds are accepted within 30 days of purchase")
    b = TypedValue(ValueType.TEXT, "Refunds are accepted within 14 days of purchase")
    e = compute_error(a, b, NgramEmbedder())
    assert e.comparator == "text_cosine" and 0 < e.magnitude < 0.5


def test_type_inference():
    assert infer_type("30") == ValueType.INT
    assert infer_type("2026-01-01") == ValueType.DATE
    assert infer_type("scripts/deploy.sh") == ValueType.PATH
    assert infer_type("prod") == ValueType.STRING
    assert infer_type("customers can return items") == ValueType.TEXT


# ------------------------------------------------------------ stages 2-5
def test_noop_writes_no_log_row(dm):
    dm.write("deploy_script", "path", "scripts/deploy.sh", value_type="path")
    for _ in range(3):
        assert dm.write("deploy_script", "path", "scripts/deploy.sh").status == "noop"
    assert len(dm.history("deploy_script", "path")) == 1


def test_insert_then_correct_then_noop(dm):
    r = dm.write("refund_policy", "window_days", 30, source="manual")
    assert r.status == "inserted" and r.slot.version == 1

    r = dm.write("Refund Policy", "window days", "14", source="tool_call:config_diff")
    assert r.status == "corrected"
    assert r.slot.value.data == 14 and r.slot.version == 2
    assert r.correction.delta == -16

    r = dm.write("refund_policy", "window_days", 14)
    assert r.status == "noop"
    kinds = [c.kind for c in dm.history("refund_policy", "window_days")]
    assert kinds == ["insert", "correction"]  # restatements don't log noise


def test_similar_hint_resolves_and_learns_alias(dm):
    dm.write("refund_policy", "window_days", 30)
    r = dm.write("refund policies", "window_day", 21)
    assert r.status == "corrected"
    assert r.resolution.entity_via in ("exact", "similar")
    # read path never does similarity search, but learned aliases are exact lookups
    assert dm.read("refund policies", "window_day").value.data == 21


def test_different_predicate_same_entity_does_not_collide(dm):
    dm.write("refund_policy", "window_days", 30)
    r = dm.write("refund_policy", "max_amount", 500)
    assert r.status == "inserted"
    assert dm.read("refund_policy", "window_days").value.data == 30


def test_unrelated_entity_is_new_slot(dm):
    dm.write("refund_policy", "window_days", 30)
    r = dm.write("return_policy", "window_days", 60)
    assert r.status == "inserted" and r.resolution.entity_via == "new"


def test_multi_cardinality_appends(dm):
    dm.write("deploy_script", "owners", "alice", cardinality="multi")
    r = dm.write("deploy_script", "owners", "bob")
    assert r.status == "appended" and r.slot.value.data == ["alice", "bob"]
    assert dm.write("deploy_script", "owners", "alice").status == "noop"


def test_low_confidence_cannot_overwrite_structured(dm):
    dm.write("refund_policy", "window_days", 30, source="tool_call:config")
    r = dm.write("refund_policy", "window_days", 7, source="llm_extract")
    assert r.status == "needs_review" and r.pending_id
    assert dm.read("refund_policy", "window_days").value.data == 30


# ---------------------------------------------------------- concurrency
def test_stale_expected_version_goes_to_reconciliation(dm):
    dm.write("refund_policy", "window_days", 30)
    seen = dm.read("refund_policy", "window_days").version  # both agents read v1

    a = dm.write("refund_policy", "window_days", 14, expected_version=seen)
    b = dm.write("refund_policy", "window_days", 21, expected_version=seen)
    assert a.status == "corrected" and a.slot.version == 2
    assert b.status == "conflict" and b.pending_id
    assert dm.read("refund_policy", "window_days").value.data == 14  # not clobbered

    pend = dm.reconcile("refund_policy", "window_days")["pending"]
    assert len(pend) == 1 and pend[0]["proposed_value"]["data"] == 21
    assert pend[0]["current_value"]["data"] == 14  # both candidates on one row

    out = dm.reconcile(accept=b.pending_id)
    assert out["status"] == "accepted" and out["slot"]["version"] == 3
    assert dm.reconcile("refund_policy", "window_days")["pending"] == []
    assert [c.kind for c in dm.history("refund_policy", "window_days")] == \
        ["insert", "correction", "reconcile"]


def test_convergent_stale_write_is_noop(dm):
    dm.write("refund_policy", "window_days", 30)
    dm.write("refund_policy", "window_days", 14, expected_version=1)
    assert dm.write("refund_policy", "window_days", 14, expected_version=1).status == "noop"


def test_real_thread_race_exactly_one_commit(dm):
    dm.write("refund_policy", "window_days", 30)
    barrier = threading.Barrier(8)
    results = []

    def agent(v):
        barrier.wait()
        results.append(dm.write("refund_policy", "window_days", v, expected_version=1).status)

    ts = [threading.Thread(target=agent, args=(10 + i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("corrected") == 1
    assert results.count("conflict") == 7
    assert dm.read("refund_policy", "window_days").version == 2


def test_writes_to_different_slots_never_contend(dm):
    dm.write("refund_policy", "window_days", 30)
    dm.write("deploy_script", "path", "scripts/deploy.sh")
    barrier = threading.Barrier(2)
    out = {}

    def w(k, e, p, v):
        barrier.wait()
        out[k] = dm.write(e, p, v, expected_version=1).status

    t1 = threading.Thread(target=w, args=("a", "refund_policy", "window_days", 14))
    t2 = threading.Thread(target=w, args=("b", "deploy_script", "path", "scripts/release/deploy.sh"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert out == {"a": "corrected", "b": "corrected"}


def test_reject_pending(dm):
    dm.write("refund_policy", "window_days", 30)
    dm.write("refund_policy", "window_days", 14, expected_version=1)
    b = dm.write("refund_policy", "window_days", 21, expected_version=1)
    assert dm.reconcile(reject=b.pending_id)["status"] == "rejected"
    assert dm.reconcile()["pending"] == []
