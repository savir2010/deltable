"""Print key similarities for labelled pairs so THETA_ENTITY / THETA_PREDICATE are picked, not guessed.

    python scripts/tune_thresholds.py
"""
from delta_memory.embed import NgramEmbedder
from delta_memory.resolve import THETA_ENTITY, THETA_PREDICATE, key_similarity

# (hint, existing key, should_match)
ENTITY_PAIRS = [
    ("refund_policy", "refund_policy", True),
    ("Refund Policy", "refund_policy", True),
    ("refundPolicy", "refund_policy", True),
    ("refund-policy", "refund_policy", True),
    ("refund policies", "refund_policy", True),
    ("refunds policy", "refund_policy", True),
    ("deploy_script", "deploy_script", True),
    ("deploy script", "deploy_script", True),
    ("deploy_scripts", "deploy_script", True),
    ("return_policy", "refund_policy", False),
    ("refund_request", "refund_policy", False),
    ("privacy_policy", "refund_policy", False),
    ("deploy_config", "deploy_script", False),
    ("build_script", "deploy_script", False),
    ("deploy", "deploy_script", False),  # ambiguous: prefer a new slot over a false collision
]
PREDICATE_PAIRS = [
    ("window_days", "window_days", True),
    ("window days", "window_days", True),
    ("windowDays", "window_days", True),
    ("window_day", "window_days", True),
    ("refund_window_days", "window_days", True),  # entity name repeated in predicate
    ("max_amount", "window_days", False),
    ("window_hours", "window_days", False),
    ("path", "path", True),
    ("paths", "path", True),
    ("owner", "path", False),
]

def report(name, pairs, theta):
    e = NgramEmbedder()
    pos = [key_similarity(e, a, b) for a, b, m in pairs if m]
    neg = [key_similarity(e, a, b) for a, b, m in pairs if not m]
    print(f"\n{name}")
    for a, b, m in pairs:
        s = key_similarity(e, a, b)
        ok = (s >= theta) == m
        print(f"  {'ok ' if ok else 'BAD'} {s:.3f}  {'match' if m else 'dist '}  {a!r:24} vs {b!r}")
    print(f"  min positive {min(pos):.3f} | max negative {max(neg):.3f} | "
          f"any theta in ({max(neg):.3f}, {min(pos):.3f}] separates them" if max(neg) < min(pos)
          else "  NOT SEPARABLE with this embedder")

report("entities", ENTITY_PAIRS, THETA_ENTITY)
report("predicates", PREDICATE_PAIRS, THETA_PREDICATE)
