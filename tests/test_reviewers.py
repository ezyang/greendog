import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from greendog import reviewers


def _cp(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def _fake_runner(perm="write", reviews=0, pending=0, commits=0, last="2026-09-01T00:00:00Z"):
    def run(args):
        joined = " ".join(args)
        if "/permission" in joined:
            return _cp(perm + "\n")
        if "reviewed-by:" in joined:
            return _cp(json.dumps({"total_count": reviews,
                                   "items": [{"updated_at": last}] if reviews else []}))
        if "review-requested:" in joined:
            return _cp(json.dumps({"total_count": pending, "items": []}))
        if "/commits?author=" in joined:
            return _cp(json.dumps([commits, last if commits else None]))
        raise AssertionError(joined)
    return run


class ClassifyTest(unittest.TestCase):
    def test_no_write_access_is_departed(self):
        self.assertEqual(reviewers.classify({"permission": "read", "reviews_90d": 50}), "departed")

    def test_enough_reviews_is_active(self):
        self.assertEqual(reviewers.classify({"permission": "write", "reviews_90d": 3}), "active")

    def test_commits_alone_are_cold(self):
        self.assertEqual(
            reviewers.classify({"permission": "admin", "reviews_90d": 1, "commits_90d": 40}),
            "cold",
        )

    def test_override_wins(self):
        rec = {"latest": {"permission": "write", "reviews_90d": 0},
               "override": {"status": "active", "note": "reviews internally"}}
        st, why = reviewers.status_of(rec)
        self.assertEqual(st, "active")
        self.assertIn("reviews internally", why)

    def test_overloaded_is_flagged_not_gated(self):
        st, why = reviewers.status_of(
            {"latest": {"permission": "admin", "reviews_90d": 300, "pending": 400}})
        self.assertEqual(st, "active")
        self.assertIn("OVERLOADED", why)


class KBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "reviewers.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_observe_parses_signals(self):
        obs = reviewers.observe("x", _fake_runner(reviews=7, pending=2, commits=5),
                                today=date(2026, 9, 10))
        self.assertEqual(obs["reviews_90d"], 7)
        self.assertEqual(obs["pending"], 2)
        self.assertEqual(obs["commits_90d"], 5)
        self.assertEqual(obs["last_seen"], "2026-09-01")
        self.assertEqual(obs["observed_at"], "2026-09-10")

    def test_eligible_observes_and_persists(self):
        kb = reviewers.ReviewerKB(path=self.path, run=_fake_runner(reviews=10))
        self.assertTrue(kb.eligible("alice"))
        kb.save()
        kb2 = reviewers.ReviewerKB(path=self.path, run=_fake_runner(reviews=0))
        # Fresh record → not re-observed, so the old (active) status holds.
        self.assertTrue(kb2.eligible("alice"))
        self.assertEqual(kb2.consulted[0][1], "active")

    def test_refresh_keeps_history(self):
        kb = reviewers.ReviewerKB(path=self.path, run=_fake_runner(reviews=10))
        kb.refresh("bob", today=date(2026, 1, 1))
        kb.run = _fake_runner(reviews=0)
        kb.refresh("bob", today=date(2026, 3, 1))
        rec = kb.record("bob")
        self.assertEqual(rec["status"], "cold")
        self.assertEqual(len(rec["history"]), 1)
        self.assertEqual(rec["history"][0]["reviews_90d"], 10)

    def test_stale_record_is_reobserved(self):
        kb = reviewers.ReviewerKB(path=self.path, run=_fake_runner(reviews=10))
        kb.refresh("carol", today=date(2026, 1, 1))
        kb.run = _fake_runner(reviews=0)
        self.assertTrue(kb.is_stale("carol", today=date(2026, 3, 1)))
        kb.ensure("carol", today=date(2026, 3, 1))
        self.assertEqual(kb.record("carol")["status"], "cold")

    def test_cold_not_eligible(self):
        kb = reviewers.ReviewerKB(path=self.path, run=_fake_runner(reviews=1, commits=30))
        self.assertFalse(kb.eligible("dave"))


if __name__ == "__main__":
    unittest.main()
