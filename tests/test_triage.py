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


class RequestedReviewerTest(unittest.TestCase):
    """Criterion 3: any merge-capable requested reviewer is on the hook."""

    def _pr(self, reviewers):
        return {
            "number": 1, "title": "t",
            "author": {"login": "author"},
            "labels": [{"name": "open source"}],
            "reviewRequests": [{"login": r} for r in reviewers],
            "reviews": [], "comments": [],
            "files": [{"path": "torch/optim/x.py"}],
        }

    def test_codeowner_assigned_merger_counts(self) -> None:
        r = triage.adjudicate(
            [self._pr(["janeyx99", "albanD"])],
            can_merge=lambda u, files: u in {"janeyx99", "albanD"},
        )[0]
        self.assertEqual(r["verdict"], "mark_triaged")
        self.assertEqual(r["on_the_hook"], ["albanD", "janeyx99"])
        self.assertEqual(r["reasons"]["janeyx99"], "requested-reviewer")
        self.assertEqual(r["add_reviewers"], [])

    def test_non_merger_reviewer_does_not_count(self) -> None:
        r = triage.adjudicate(
            [self._pr(["sylvesterkaczmarek"])],
            can_merge=lambda u, files: False,
        )[0]
        self.assertEqual(r["verdict"], "needs_triage")

    def test_author_as_reviewer_ignored(self) -> None:
        # Author can't merge (else it's maintainer_authored); a self-request
        # must not count as engagement.
        pr = self._pr(["author"])
        pr["reviews"] = [{"author": {"login": "author"}}]
        r = triage.adjudicate(
            [pr], can_merge=lambda u, files: u != "author"
        )[0]
        self.assertEqual(r["verdict"], "needs_triage")
