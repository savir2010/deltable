from pathlib import Path

import pytest

from delta_memory import DeltaMemory, SqliteStore
from delta_memory.adapters import (ExtractedFact, extract_facts, from_api_response,
                                   from_config_diff, ingest_git_diff, ingest_text, parse_git_diff)
from delta_memory.failure import DependencyRegistry, find_file_probe, on_procedure_failure
from delta_memory.gbrain import MarkdownMirror

CONFIG_DIFF = """\
diff --git a/config/refund_policy.yaml b/config/refund_policy.yaml
index 1111111..2222222 100644
--- a/config/refund_policy.yaml
+++ b/config/refund_policy.yaml
@@ -1,3 +1,3 @@
 currency: usd
-window_days: 30
+window_days: 14
 max_amount: 500
"""

RENAME_DIFF = """\
diff --git a/scripts/deploy.sh b/scripts/release/deploy.sh
similarity index 100%
rename from scripts/deploy.sh
rename to scripts/release/deploy.sh
"""


def test_parse_config_diff_only_changed_keys():
    facts = parse_git_diff(CONFIG_DIFF)
    assert [(f.entity_hint, f.predicate_hint, f.value) for f in facts] == [("refund_policy", "window_days", 14)]


def test_git_diff_correction_no_llm(dm):
    dm.write("refund_policy", "window_days", 30)
    [r] = ingest_git_diff(dm, CONFIG_DIFF)
    assert r.status == "corrected" and r.slot.source == "tool_call:git_diff" and r.slot.confidence == 1.0


def test_rename_resolves_by_old_value(dm):
    dm.write("deploy_script", "path", "scripts/deploy.sh", value_type="path")
    [r] = ingest_git_diff(dm, RENAME_DIFF)
    # hint stem is "deploy", which alone would NOT resolve to deploy_script; the old value does
    assert r.resolution.entity_via == "value_match"
    assert r.status == "corrected" and r.slot.value.data == "scripts/release/deploy.sh"


def test_config_and_api_adapters():
    assert [(f.predicate_hint, f.value) for f in
            from_config_diff("svc", {"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 3}})] == [("b.c", 3)]
    assert len(from_api_response("svc", {"x": 1, "y": {"z": "q"}})) == 2


def test_heuristic_extract_snaps_to_known_key(dm):
    dm.write("refund_policy", "window_days", 30, source="manual", confidence=0.7)
    facts = extract_facts(dm, "Heads up: the refund window is now 14 days.", extractor=_Offline())
    assert [(f.entity_hint, f.predicate_hint, f.value, f.source) for f in facts] == \
        [("refund_policy", "window_days", "14", "heuristic_extract")]


def test_llm_extract_path_with_fake_extractor(dm):
    dm.write("refund_policy", "window_days", 30, confidence=0.5)

    class Fake:
        def extract(self, text, keys):
            assert "refund_policy.window_days" in keys
            return [ExtractedFact(entity="refund_policy", predicate="window_days", value="21", value_type="int")]

    [r] = ingest_text(dm, "we're moving to a 3 week refund window", extractor=Fake())
    assert r.status == "corrected" and r.slot.source == "llm_extract" and r.slot.confidence == 0.8


class _Offline:
    def extract(self, text, keys):
        raise RuntimeError("no network")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "deploy.sh").write_text("echo deploy\n")
    return root


def test_failure_triggered_correction(dm, tmp_path):
    root = _repo(tmp_path)
    dm.write("deploy_script", "path", "scripts/deploy.sh", value_type="path")
    reg = DependencyRegistry()
    reg.register("deploy", "run_script", "deploy_script", "path", find_file_probe(root))

    # flake: file still where memory says -> nothing committed
    assert on_procedure_failure(dm, reg, "deploy", "run_script")["status"] == "flake"

    (root / "scripts" / "release").mkdir()
    (root / "scripts" / "deploy.sh").rename(root / "scripts" / "release" / "deploy.sh")
    out = on_procedure_failure(dm, reg, "deploy", "run_script", "No such file")
    assert out["status"] == "corrected"
    assert dm.read("deploy_script", "path").value.data == "scripts/release/deploy.sh"
    assert dm.read("deploy_script", "path").source == "failure_triggered:deploy"


def test_failure_probe_inconclusive_when_file_gone(dm, tmp_path):
    root = _repo(tmp_path)
    dm.write("deploy_script", "path", "scripts/deploy.sh", value_type="path")
    (root / "scripts" / "deploy.sh").unlink()
    reg = DependencyRegistry()
    reg.register("deploy", "run_script", "deploy_script", "path", find_file_probe(root))
    assert on_procedure_failure(dm, reg, "deploy", "run_script")["status"] == "probe_inconclusive"
    assert dm.read("deploy_script", "path").version == 1


def test_markdown_mirror(dm, tmp_path):
    dm.hooks.append(MarkdownMirror(dm.store, tmp_path / "brain"))
    dm.write("refund_policy", "window_days", 30)
    dm.write("refund_policy", "window_days", 14)
    page = (tmp_path / "brain" / "delta-memory" / "refund_policy.md").read_text()
    assert "`14`" in page and "30 -> 14" in page
