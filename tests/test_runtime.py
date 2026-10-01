"""Filesystem and record invariants, not model behavior or universal quality tests."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/luobo-soul-ring-hunter/scripts"
sys.path.insert(0, str(SCRIPTS))
import skill_ops as ops
import followup


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="soul-ring-hunter-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.candidate = self.root / "candidate"
        self.target = self.root / "target"
        self.run = self.root / "run"
        self.make_skill(self.candidate, "new")
        self.make_skill(self.target, "old")

    def make_skill(self, path, body):
        path.mkdir()
        (path / "SKILL.md").write_text('---\nname: sample\ndescription: Test fixture\n---\n' + body)

    def prepare(self):
        return ops.prepare(self.candidate, self.target, self.run)

    def proof(self, data, old=True):
        (self.run / "output.txt").write_text("synthetic test evidence; no model inference")
        case = {"status": "passed", "kind": "behavior", "invocation": "unit fixture",
                "runtime": "unit fixture", "evidence": [{"path": "output.txt", "sha256": ops.sha(self.run / "output.txt")}]}
        proof = {"candidate_sha256": data["candidate_sha256"], "new_case": case}
        if old:
            proof["old_case"] = case.copy()
        ops.write_json(self.run / "validation.json", proof)

    def assert_blocked(self, text, fn):
        with self.assertRaisesRegex(ValueError, text):
            fn()

    def test_inventory_deduplicates_exact_mirrors_but_reports_scope(self):
        import shutil
        shutil.copytree(self.target, self.root / "mirror")
        result = ops.inventory([self.root])
        self.assertEqual(len(result["skills"]), 3)
        self.assertEqual(len(result["same_content"]), 1)
        self.assertEqual(result["errors"], [])

    def test_inventory_missing_root_is_not_empty_success(self):
        self.assertEqual(ops.inventory([self.root / "missing"])["errors"][0]["error"], "MISSING_DIRECTORY")

    def test_prepare_does_not_change_target(self):
        before = ops.fingerprint(self.target)
        data = self.prepare()
        self.assertEqual(data["status"], "prepared")
        self.assertEqual(before, ops.fingerprint(self.target))

    def test_nested_and_symlink_paths_rejected(self):
        self.assert_blocked("NESTED_PATHS", lambda: ops.prepare(self.candidate, self.target, self.target / "run"))
        (self.candidate / "link").symlink_to(self.target / "SKILL.md")
        self.assert_blocked("SYMLINK", self.prepare)

    def test_repository_metadata_is_not_silently_removed(self):
        (self.target / ".git").mkdir()
        (self.target / ".git/HEAD").write_text("ref: refs/heads/main\n")
        self.assert_blocked("REPOSITORY_METADATA", self.prepare)
        self.assertTrue((self.target / ".git/HEAD").is_file())

    def test_evidence_symlink_is_rejected_even_inside_run(self):
        data = self.prepare()
        self.proof(data)
        (self.run / "alias.txt").symlink_to(self.run / "output.txt")
        proof = ops.read_json(self.run / "validation.json")
        proof["new_case"]["evidence"][0]["path"] = "alias.txt"
        ops.write_json(self.run / "validation.json", proof)
        self.assert_blocked("EVIDENCE_SYMLINK", lambda: ops.apply(self.run))

    def test_missing_behavior_proof_does_not_apply(self):
        before = ops.digest(self.target)
        self.prepare()
        with self.assertRaises(FileNotFoundError):
            ops.apply(self.run)
        self.assertEqual(before, ops.digest(self.target))

    def test_static_only_proof_rejected(self):
        data = self.prepare()
        self.proof(data)
        proof = ops.read_json(self.run / "validation.json")
        proof["new_case"]["kind"] = "static"
        ops.write_json(self.run / "validation.json", proof)
        self.assert_blocked("NO_BEHAVIOR_PASS", lambda: ops.apply(self.run))

    def test_update_requires_old_behavior_case(self):
        data = self.prepare()
        self.proof(data, old=False)
        self.assert_blocked("NO_BEHAVIOR_PASS: old_case", lambda: ops.apply(self.run))

    def test_evidence_changed_and_outside_run_rejected(self):
        data = self.prepare()
        self.proof(data)
        (self.run / "output.txt").write_text("changed")
        self.assert_blocked("EVIDENCE_CHANGED", lambda: ops.apply(self.run))
        self.proof(data)
        proof = ops.read_json(self.run / "validation.json")
        proof["new_case"]["evidence"][0]["path"] = "../target/SKILL.md"
        ops.write_json(self.run / "validation.json", proof)
        self.assert_blocked("EVIDENCE_OUTSIDE_RUN", lambda: ops.apply(self.run))

    def test_target_drift_preserved(self):
        data = self.prepare()
        self.proof(data)
        (self.target / "extra.txt").write_text("concurrent user change")
        self.assert_blocked("BASELINE_DRIFT", lambda: ops.apply(self.run))
        self.assertEqual((self.target / "extra.txt").read_text(), "concurrent user change")

    def test_candidate_drift_rejected(self):
        data = self.prepare()
        self.proof(data)
        (self.run / "candidate/SKILL.md").write_text("changed after test")
        self.assert_blocked("CANDIDATE_CHANGED", lambda: ops.apply(self.run))

    def test_update_idempotence_and_exact_restore(self):
        before = ops.fingerprint(self.target)
        data = self.prepare()
        self.proof(data)
        self.assertEqual(ops.apply(self.run)["status"], "applied")
        self.assertTrue(ops.apply(self.run)["unchanged"])
        self.assertEqual(ops.digest(self.target), data["candidate_sha256"])
        self.assertEqual(ops.rollback(self.run)["status"], "reverted")
        self.assertEqual(ops.fingerprint(self.target), before)
        self.assertEqual(ops.rollback(self.run)["status"], "reverted")

    def test_rollback_does_not_overwrite_later_edit(self):
        data = self.prepare()
        self.proof(data)
        ops.apply(self.run)
        (self.target / "user.txt").write_text("later edit")
        self.assert_blocked("TARGET_DRIFT", lambda: ops.rollback(self.run))
        self.assertEqual((self.target / "user.txt").read_text(), "later edit")

    def test_new_install_and_remove_only_this_install(self):
        import shutil
        shutil.rmtree(self.target)
        data = self.prepare()
        self.proof(data, old=False)
        ops.apply(self.run)
        self.assertEqual(ops.digest(self.target), data["candidate_sha256"])
        ops.rollback(self.run)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.run / "candidate/SKILL.md").is_file())

    def crash_after_rename(self, count):
        before = ops.fingerprint(self.target)
        data = self.prepare()
        self.proof(data)
        code = '''import os, sys
sys.path.insert(0, sys.argv[1])
import skill_ops as ops
original = os.rename
calls = 0
def interrupted(*args, **kwargs):
    global calls
    result = original(*args, **kwargs)
    calls += 1
    if calls == int(sys.argv[3]):
        os._exit(91)
    return result
os.rename = interrupted
ops.apply(sys.argv[2])
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.run), str(count)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertEqual(ops.read_json(self.run / "transaction.json")["status"], "applying")
        self.assertEqual(ops.rollback(self.run)["status"], "reverted")
        self.assertEqual(ops.fingerprint(self.target), before)

    def test_actual_process_exit_between_directory_moves_is_recoverable(self):
        self.crash_after_rename(1)

    def test_actual_process_exit_after_activation_is_recoverable(self):
        self.crash_after_rename(2)

    def new_record(self):
        self.record = self.root / "usage.json"
        followup.initialize(self.record, self.target, "fixed original criterion", "only this test", "2020-01-01T00:00:00Z")
        self.evidence = self.root / "use.txt"
        self.evidence.write_text("fixture result")

    def test_due_unobserved_is_not_success(self):
        self.new_record()
        self.assertEqual(followup.check(ops.read_json(self.record))["state"], "unobserved")

    def test_replay_is_not_counted_as_real_usage(self):
        self.new_record()
        followup.append_use(self.record, self.evidence, "pass", "replay", "case-1", "unit")
        result = followup.check(ops.read_json(self.record))
        self.assertEqual(result["state"], "unobserved")
        self.assertEqual(result["actual_runs"], 0)

    def test_insufficient_data_and_duplicate_use(self):
        self.new_record()
        followup.append_use(self.record, self.evidence, "pass", "actual", "case-1", "unit")
        self.assertTrue(followup.append_use(self.record, self.evidence, "pass", "actual", "case-1", "unit")["unchanged"])
        result = followup.check(ops.read_json(self.record))
        self.assertEqual((result["state"], result["actual_runs"]), ("insufficient_data", 1))

    def test_three_passes_need_review_not_automatic_success(self):
        self.new_record()
        for i in range(3):
            followup.append_use(self.record, self.evidence, "pass", "actual", f"case-{i}", "unit")
        self.assertEqual(followup.check(ops.read_json(self.record))["state"], "needs_review")

    def test_one_failure_can_be_reviewed_before_due(self):
        self.new_record()
        followup.append_use(self.record, self.evidence, "fail", "actual", "case-1", "unit")
        data = ops.read_json(self.record)
        data["due_at"] = "2099-01-01T00:00:00Z"
        result = followup.check(data)
        self.assertFalse(result["due"])
        self.assertEqual(result["state"], "needs_review")

    def test_version_and_evidence_changes_are_not_old_passes(self):
        self.new_record()
        followup.append_use(self.record, self.evidence, "pass", "actual", "case-1", "unit")
        self.evidence.write_text("changed later")
        self.assertEqual(followup.check(ops.read_json(self.record))["state"], "evidence_changed")
        (self.target / "SKILL.md").write_text("another version")
        self.assertEqual(followup.check(ops.read_json(self.record))["state"], "version_changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
