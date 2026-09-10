import json
import subprocess
import unittest
from unittest.mock import patch

from greendog import triage


def _node(number: int) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "author": {"login": "author"},
        "labels": {"nodes": [{"name": "open source"}]},
        "reviewRequests": {
            "nodes": [{"requestedReviewer": {"login": "reviewer"}}]
        },
        "reviews": {"nodes": [{"author": {"login": "reviewer"}}]},
        "comments": {
            "nodes": [{"author": {"login": "reviewer"}, "body": "Looks good"}]
        },
        "files": {"nodes": [{"path": "torch/example.py"}]},
    }


def _response(nodes: list[dict], *, has_next: bool, cursor: str | None) -> str:
    return json.dumps({
        "data": {
            "search": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    })


class FetchPrsTest(unittest.TestCase):
    @patch("greendog.triage.subprocess.run")
    def test_fetches_and_flattens_multiple_small_pages(self, run) -> None:
        run.side_effect = [
            subprocess.CompletedProcess(
                [], 0, _response([_node(2)], has_next=True, cursor="next"), ""
            ),
            subprocess.CompletedProcess(
                [], 0, _response([_node(1)], has_next=False, cursor=None), ""
            ),
        ]

        prs = triage.fetch_prs()

        self.assertEqual([pr["number"] for pr in prs], [2, 1])
        self.assertEqual(prs[0]["reviewRequests"], [{"login": "reviewer"}])
        self.assertEqual(prs[0]["files"], [{"path": "torch/example.py"}])
        self.assertEqual(run.call_count, 2)
        first_payload = json.loads(run.call_args_list[0].kwargs["input"])
        second_payload = json.loads(run.call_args_list[1].kwargs["input"])
        self.assertEqual(first_payload["variables"]["pageSize"], triage.PR_PAGE_SIZE)
        self.assertIsNone(first_payload["variables"]["endCursor"])
        self.assertEqual(second_payload["variables"]["endCursor"], "next")
        run.assert_any_call(
            ["gh", "api", "graphql", "--input", "-"],
            input=run.call_args_list[0].kwargs["input"],
            capture_output=True,
            text=True,
            check=True,
        )

    @patch("greendog.triage.subprocess.run")
    def test_rejects_missing_cursor_for_another_page(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 0, _response([], has_next=True, cursor=None), ""
        )

        with self.assertRaisesRegex(RuntimeError, "without an end cursor"):
            triage.fetch_prs()


if __name__ == "__main__":
    unittest.main()


class ExplicitAuthorRequestTest(unittest.TestCase):
    """Criterion 3 refinement: an author's late, solitary reviewer pick counts."""

    def _pr(self, created="2026-09-01T10:29:15Z"):
        return {
            "number": 1, "title": "t", "createdAt": created,
            "author": {"login": "author"},
            "labels": [{"name": "open source"}],
            "reviewRequests": [{"login": "mlazos"}],
            "reviews": [], "comments": [],
            "files": [{"path": "torch/_dynamo/x.py"}],
        }

    def _classify(self, events):
        return triage.adjudicate(
            [self._pr()],
            can_merge=lambda u, files: u == "mlazos",
            request_actors=lambda n: events,
        )[0]

    def test_late_solo_author_pick_counts(self) -> None:
        r = self._classify({"mlazos": {("author", "2026-09-02T12:59:43Z")}})
        self.assertEqual(r["verdict"], "mark_triaged")
        self.assertEqual(r["reasons"]["mlazos"], "author-picked-reviewer")

    def test_open_time_request_is_codeowner_noise(self) -> None:
        r = self._classify({"mlazos": {("author", "2026-09-01T10:29:16Z")}})
        self.assertEqual(r["verdict"], "needs_triage")

    def test_late_batch_is_codeowner_noise(self) -> None:
        r = self._classify({
            "mlazos": {("author", "2026-09-02T12:59:43Z")},
            "someone": {("author", "2026-09-02T12:59:44Z")},
        })
        self.assertEqual(r["verdict"], "needs_triage")

    def test_non_author_request_still_manual(self) -> None:
        r = self._classify({"mlazos": {("triager", "2026-09-01T10:29:16Z")}})
        self.assertEqual(r["reasons"]["mlazos"], "manual-reviewer")
