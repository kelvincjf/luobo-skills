import copy
import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "skills" / "soul-ring-hunter" / "scripts" / "upstream.py"
spec = importlib.util.spec_from_file_location("upstream", MODULE)
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
NOW = dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc)
OLD, HEAD, OLD_ROOT, NEW_ROOT, BLOB, NEW_BLOB = (c * 40 for c in "123456")


def registry(**changes):
    entry = dict(id="example", repo="owner/repo", ref="main", paths=["SKILL.md"],
                 baseline_commit=OLD, mode="adapted", decision="update", enabled=True,
                 interval_days=7, targets=["local/skill"], source_evidence=["evaluation.json"])
    entry.update(changes)
    return {"schema": 1, "entries": [entry]}


class FakeGitHub:
    def __init__(self, changed=False, missing=False):
        self.calls = []
        self.data = {
            "/repos/owner/repo/commits/main": {"sha": HEAD, "commit": {"tree": {"sha": NEW_ROOT}}},
            "/repos/owner/repo/git/commits/" + OLD: {"tree": {"sha": OLD_ROOT}},
            "/repos/owner/repo/git/trees/" + OLD_ROOT: {"truncated": False, "tree": [self.blob(BLOB)]},
            "/repos/owner/repo/git/trees/" + NEW_ROOT: {"truncated": False, "tree": [] if missing else [self.blob(NEW_BLOB if changed else BLOB), self.blob(NEW_BLOB, "README.md")]},
        }

    @staticmethod
    def blob(value, path="SKILL.md"):
        return {"path": path, "type": "blob", "sha": value, "mode": "100644"}

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        return copy.deepcopy(self.data[endpoint])


class UpstreamTests(unittest.TestCase):
    def run_check(self, registration=None, state=None, api=None, **kwargs):
        report, updated = upstream.check_registry(registration or registry(), state,
                                                  api or FakeGitHub(), now=kwargs.pop("now", NOW), **kwargs)
        return report["results"][0], updated

    def test_commit_changed_only_outside_monitored_path(self):
        result, state = self.run_check()
        self.assertEqual(result["state"], "unchanged")
        self.assertNotEqual(result["before_commit"], result["after_commit"])
        self.assertFalse(result["relevant_changed"])
        self.assertTrue(result["quiet"])
        self.assertEqual(state["entries"]["example"]["baseline_snapshot"]["commit"], OLD)

    def test_real_change_and_repeated_observation_do_not_advance_baseline(self):
        registration = registry()
        original = copy.deepcopy(registration)
        result, state = self.run_check(registration, api=FakeGitHub(changed=True))
        self.assertEqual(result["state"], "update_available")
        self.assertEqual(result["changed_paths"], ["SKILL.md"])
        self.assertFalse(result["changes_applied"])
        self.assertFalse(result["quiet"])
        self.assertTrue(result["new_since_last_check"])
        notification_key = result["notification_key"]
        result, next_state = self.run_check(registration, state, FakeGitHub(changed=True), force=True)
        self.assertTrue(result["quiet"])
        self.assertFalse(result["new_since_last_check"])
        self.assertEqual(result["notification_key"], notification_key)
        self.assertEqual(next_state["entries"]["example"]["baseline_snapshot"]["commit"], OLD)
        self.assertEqual(registration, original)

    def test_unknown_baseline_remains_unknown(self):
        result, state = self.run_check(registry(baseline_commit=None))
        self.assertEqual(result["state"], "baseline_unknown")
        self.assertIsNone(result["relevant_changed"])
        self.assertNotIn("baseline_snapshot", state["entries"]["example"])
        self.assertIsNone(result["compare_url"])

    def test_deleted_monitored_file_needs_review(self):
        result, _ = self.run_check(api=FakeGitHub(missing=True))
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["missing_paths"], ["SKILL.md"])
        self.assertTrue(result["relevant_changed"])

    def test_error_preserves_successful_observation_and_retry_is_due(self):
        _, state = self.run_check()
        before = copy.deepcopy(state)
        def denied(_):
            raise upstream.UpstreamError("GitHub HTTP 429")
        result, failed = self.run_check(state=state, api=denied, now=NOW + dt.timedelta(days=8))
        self.assertEqual(result["state"], "error")
        self.assertIsNone(result["relevant_changed"])
        self.assertEqual(failed["entries"]["example"]["last_observed"], before["entries"]["example"]["last_observed"])
        self.assertEqual(state, before)
        api = FakeGitHub()
        result, _ = self.run_check(state=failed, api=api, now=NOW + dt.timedelta(days=8, seconds=10))
        self.assertEqual(result["state"], "unchanged")
        self.assertTrue(api.calls)

    def test_not_due_has_no_network_and_no_fresh_claim(self):
        _, state = self.run_check()
        api = FakeGitHub()
        result, _ = self.run_check(state=state, api=api, now=NOW + dt.timedelta(days=1))
        self.assertEqual(result["state"], "not_due")
        self.assertIsNone(result["relevant_changed"])
        self.assertFalse(api.calls)

    def test_disabled_defer_and_reject_skip_network(self):
        for change in ({"enabled": False}, {"decision": "defer"}, {"decision": "reject"}):
            with self.subTest(change=change):
                api = FakeGitHub()
                result, _ = self.run_check(registry(**change), api=api)
                self.assertEqual(result["state"], "skipped")
                self.assertFalse(api.calls)

    def test_config_change_forces_fresh_baseline_even_before_due(self):
        _, state = self.run_check()
        api = FakeGitHub(changed=True)
        result, _ = self.run_check(registry(paths=["README.md"]), state, api)
        self.assertEqual(result["changed_paths"], ["README.md"])
        self.assertTrue(any("/git/commits/" in call for call in api.calls))

    def test_truncated_tree_cannot_be_unchanged(self):
        api = FakeGitHub()
        api.data["/repos/owner/repo/git/trees/" + NEW_ROOT]["truncated"] = True
        result, state = self.run_check(api=api)
        self.assertEqual(result["state"], "error")
        self.assertNotIn("last_observed", state["entries"]["example"])

    def test_directory_is_compared_by_tree_sha(self):
        api = FakeGitHub()
        for root, value in ((OLD_ROOT, BLOB), (NEW_ROOT, NEW_BLOB)):
            api.data["/repos/owner/repo/git/trees/" + root]["tree"] = [{"path": "skills", "type": "tree", "sha": value, "mode": "040000"}]
        result, _ = self.run_check(registry(paths=["skills"]), api=api)
        self.assertEqual(result["changed_paths"], ["skills"])
        self.assertEqual(len(api.calls), 4)

    def test_reference_is_explicitly_observation_only(self):
        result, _ = self.run_check(registry(decision="reference"), api=FakeGitHub(changed=True))
        self.assertTrue(result["reference_only"])
        self.assertFalse(result["changes_applied"])

    def test_invalid_path_and_too_frequent_interval_fail_before_network(self):
        for change in ({"paths": ["../private"]}, {"paths": [[]]}, {"interval_days": 1}, {"repo": "owner/.."}, {"repo": []}):
            api = FakeGitHub()
            with self.assertRaises(ValueError):
                self.run_check(registry(**change), api=api)
            self.assertFalse(api.calls)

    def test_atomic_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            data = {"schema": 1, "entries": {}}
            upstream.atomic_json(path, data)
            self.assertEqual(upstream.read_json(path), data)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
