"""OSS PR triage: is a maintainer already engaged?

The pytorch/pytorch "needs triage" queue is open, non-draft PRs labeled
`open source` but not `triaged` and not yet approved.  This module answers
step 1 of triage: if someone with merge rights is already engaged, we can
bulk-mark the PR `triaged` (a human is on the hook).

A PR hits the bar (→ mark_triaged) when a person who can ACTUALLY MERGE
this PR (per merge_rules.yaml — see mergerules.py), who is not the PR
author or a bot:

  1. left a substantive review or comment (design discussion, questions,
     requesting changes) — NOT a mechanical bot command; OR
  2. is a requested reviewer AND left any comment at all (even a bot
     command like "@claude review this please") — commenting while
     assigned is evidence they've accepted the review; OR
  3. was MANUALLY assigned as a reviewer by someone other than the author
     (a real triage action), even if they haven't commented yet.

Codeowner / author auto-assignment of a reviewer does NOT by itself count
(criterion 3 requires a non-author assigner; a silent codeowner reviewer
who never comments is not evidence of acceptance).

"Can actually merge it" is what `@pytorchbot merge` enforces: the person
must be in the `approved_by` of a merge_rules.yaml rule whose `patterns`
cover ALL of the PR's changed files (a global `*` rule for Core/Metamates,
or a path-scoped rule for the files this PR touches).  This is stricter
and more accurate than repo write access or `authorAssociation`, which
over-count people who can't merge the PR in question.  See CLAUDE.md
"OSS PR triage modality" for the full rubric.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable

from .mergerules import make_can_merge_resolver

REPO = "pytorch/pytorch"

# The canonical "needs triage" search (the is:pr/repo/state parts are supplied
# by `gh pr list` flags, so they're omitted here).
SEARCH = (
    'base:main -label:triaged draft:false label:"open source" '
    "NOT WIP NOT TESTING in:title -review:approved sort:updated-desc"
)

# A PR carrying this label is already claimed for landing (Edward's mergedog
# automation, or jansel's).  Whoever claimed it should already be a reviewer,
# so it will normally be caught by engagement anyway — but skip it explicitly
# so we never fight over an owned PR.
CLAIMED_LABELS = {"mergedog"}

# Comment bodies that are mechanical bot triggers, NOT substantive engagement.
#   - jansel's automation posts "@claude review these changes" — per direct
#     agreement this is NOT him signing up to review/land.
#   - "@pytorchbot fix-lint" / other bot commands are drive-bys.
# (Criterion 2 still counts these when the commenter is also a reviewer.)
BOT_COMMAND_PREFIXES = ("@claude", "@pytorchbot", "@pytorch-bot", "@pytorchmergebot")

# Keep each GraphQL response modest.  Asking `gh pr list` for 200 PRs together
# with every PR's files, reviews, comments, and review requests intermittently
# trips GitHub's 502 response limit.
PR_PAGE_SIZE = 25
MAX_PRS = 200

PR_LIST_QUERY = """
query($searchQuery: String!, $pageSize: Int!, $endCursor: String) {
  search(
    query: $searchQuery
    type: ISSUE
    first: $pageSize
    after: $endCursor
  ) {
    nodes {
      ... on PullRequest {
        number
        title
        author { login }
        labels(first: 100) { nodes { name } }
        reviewRequests(first: 100) {
          nodes {
            requestedReviewer {
              ... on User { login }
              ... on Team { slug }
            }
          }
        }
        reviews(first: 100) { nodes { author { login } } }
        comments(first: 100) { nodes { author { login } body } }
        files(first: 100) { nodes { path } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _is_bot(login: str) -> bool:
    login = (login or "").lower()
    return login.endswith("bot") or login.endswith("[bot]") or login in {
        "claude",
        "facebook-github-bot",
        "codecov",
        # Mechanical/app actors that leave review-like artifacts but are not
        # human engagement: the CLA signing check and GitHub Copilot's
        # automated PR reviewer.
        "linux-foundation-easycla",
        "copilot-pull-request-reviewer",
    }


def _is_bot_command(body: str) -> bool:
    b = (body or "").strip().lower()
    return b.startswith(BOT_COMMAND_PREFIXES)


# ---------------------------------------------------------------------------
# gh-backed data access (impure; injected into adjudicate for testability)
# ---------------------------------------------------------------------------

def _flatten_pr(node: dict[str, Any]) -> dict[str, Any]:
    """Convert GraphQL connections to the shape returned by `gh pr list`."""
    reviewer_requests = node.get("reviewRequests", {}).get("nodes") or []
    return {
        "number": node["number"],
        "title": node["title"],
        "author": node.get("author"),
        "labels": node.get("labels", {}).get("nodes") or [],
        "reviewRequests": [
            request["requestedReviewer"]
            for request in reviewer_requests
            if request.get("requestedReviewer")
        ],
        "reviews": node.get("reviews", {}).get("nodes") or [],
        "comments": node.get("comments", {}).get("nodes") or [],
        "files": node.get("files", {}).get("nodes") or [],
    }


def fetch_prs() -> list[dict]:
    """Fetch the needs-triage queue in small GraphQL pages."""
    search_query = f"is:pr repo:{REPO} is:open {SEARCH}"
    prs: list[dict] = []
    cursor: str | None = None

    while len(prs) < MAX_PRS:
        variables = {
            "searchQuery": search_query,
            "pageSize": min(PR_PAGE_SIZE, MAX_PRS - len(prs)),
            "endCursor": cursor,
        }
        payload = json.dumps({"query": PR_LIST_QUERY, "variables": variables})
        out = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        search = json.loads(out)["data"]["search"]
        prs.extend(
            _flatten_pr(node)
            for node in search["nodes"]
            if node and "number" in node
        )

        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError("GitHub returned another page without an end cursor")

    return prs[:MAX_PRS]


def make_request_actors_resolver() -> Callable[[int], dict[str, set[str]]]:
    """Return `request_actors(pr_number) -> {reviewer: {actors who requested}}`.

    Reads the issue timeline; used only to tell manual reviewer assignment
    (actor != author) from codeowner/author auto-assignment.  Cached per PR.
    """
    cache: dict[int, dict[str, set[str]]] = {}

    def request_actors(pr_number: int) -> dict[str, set[str]]:
        if pr_number not in cache:
            r = subprocess.run(
                ["gh", "api", f"repos/{REPO}/issues/{pr_number}/timeline",
                 "--paginate", "--jq",
                 '.[] | select(.event=="review_requested") | '
                 '{actor:(.actor.login // ""), '
                 'reviewer:(.requested_reviewer.login // "")}'],
                capture_output=True, text=True,
            )
            mapping: dict[str, set[str]] = {}
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    rev, act = ev.get("reviewer"), ev.get("actor")
                    if rev:
                        mapping.setdefault(rev, set()).add(act or "")
            cache[pr_number] = mapping
        return cache[pr_number]

    return request_actors


# ---------------------------------------------------------------------------
# adjudication (pure, given the two resolvers)
# ---------------------------------------------------------------------------

def _classify_pr(
    pr: dict,
    can_merge: Callable[[str, list[str]], bool],
    request_actors: Callable[[int], dict[str, set[str]]],
) -> dict:
    author = (pr.get("author") or {}).get("login")
    labels = {l["name"] for l in (pr.get("labels") or [])}
    claimed = labels & CLAIMED_LABELS
    files = [f["path"] for f in (pr.get("files") or [])]

    # requestedReviewers entries are Users (login) or Teams (slug).  Teams can't
    # individually merge a PR, so keep only user logins for the merge check.
    reviewers = {r["login"] for r in (pr.get("reviewRequests") or []) if r.get("login")}

    if claimed:
        return {
            "number": pr["number"], "title": pr["title"], "author": author,
            "verdict": "claimed", "on_the_hook": [], "add_reviewers": [],
            "reasons": {}, "claimed_by": sorted(claimed),
        }

    # A PR authored by someone who can merge it themselves isn't waiting on the
    # triage rotation — they own it and can land it.  Surfaced as its own bucket
    # (not folded into mark_triaged) since a maintainer can't approve their own
    # PR, so it may still want a reviewer; --apply leaves these alone.
    if author and can_merge(author, files):
        return {
            "number": pr["number"], "title": pr["title"], "author": author,
            "verdict": "maintainer_authored", "on_the_hook": [],
            "add_reviewers": [], "reasons": {}, "claimed_by": [],
        }

    # Per candidate user (non-author, non-bot): what signals do they have?
    left_substantive: set[str] = set()  # real review or non-bot-command comment
    left_any: set[str] = set()          # any review or comment (incl bot cmd)

    for rv in pr.get("reviews") or []:
        u = (rv.get("author") or {}).get("login")
        if u and u != author and not _is_bot(u):
            left_any.add(u)
            left_substantive.add(u)  # any real review state is substantive
    for c in pr.get("comments") or []:
        u = (c.get("author") or {}).get("login")
        if u and u != author and not _is_bot(u):
            left_any.add(u)
            if not _is_bot_command(c.get("body", "")):
                left_substantive.add(u)

    # Candidates worth a merge check: anyone who engaged, plus requested
    # reviewers (needed for criteria 2 and 3).
    candidates = (left_any | reviewers) - {author}
    candidates = {u for u in candidates if not _is_bot(u)}

    # "Can merge this PR" is scoped to the PR's changed files.
    mergers = {u for u in candidates if can_merge(u, files)}

    on_the_hook: dict[str, str] = {}  # user -> criterion label
    need_timeline = any(
        u in reviewers and u not in left_any for u in mergers
    )
    actors = request_actors(pr["number"]) if need_timeline else {}

    for u in sorted(mergers):
        if u in left_substantive:
            on_the_hook[u] = "substantive"          # criterion 1
        elif u in reviewers and u in left_any:
            on_the_hook[u] = "reviewer+commented"   # criterion 2
        elif u in reviewers:
            # criterion 3: manual assignment (some actor other than author/bot)
            assigners = actors.get(u, set())
            if any(a and a != author and not _is_bot(a) for a in assigners):
                on_the_hook[u] = "manual-reviewer"

    engaged = sorted(on_the_hook)
    return {
        "number": pr["number"], "title": pr["title"], "author": author,
        "verdict": "mark_triaged" if engaged else "needs_triage",
        "on_the_hook": engaged,
        "add_reviewers": [u for u in engaged if u not in reviewers],
        "reasons": on_the_hook,
        "claimed_by": [],
    }


def adjudicate(
    prs: list[dict],
    can_merge: Callable[[str, list[str]], bool] | None = None,
    request_actors: Callable[[int], dict[str, set[str]]] | None = None,
) -> list[dict]:
    """Classify each PR.  Resolvers default to the live gh-backed ones."""
    if can_merge is None:
        can_merge = make_can_merge_resolver()
    if request_actors is None:
        request_actors = make_request_actors_resolver()
    return [_classify_pr(pr, can_merge, request_actors) for pr in prs]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _apply(r: dict) -> None:
    """Add the triaged label and any missing reviewers for one PR."""
    n = str(r["number"])
    subprocess.run(
        ["gh", "pr", "edit", n, "--repo", REPO, "--add-label", "triaged"],
        check=True, capture_output=True, text=True,
    )
    # --add-reviewer takes one reviewer per flag; add them one at a time.
    for u in r["add_reviewers"]:
        subprocess.run(
            ["gh", "pr", "edit", n, "--repo", REPO, "--add-reviewer", u],
            check=True, capture_output=True, text=True,
        )


def cmd_triage(args) -> None:
    print(f"fetching needs-triage queue from {REPO}…", file=sys.stderr)
    prs = fetch_prs()
    print(f"  {len(prs)} PR(s) in queue; checking merge rights…", file=sys.stderr)
    results = adjudicate(prs)
    if args.raw:
        print(json.dumps(results, indent=2))
        return

    mt = [r for r in results if r["verdict"] == "mark_triaged"]
    nt = [r for r in results if r["verdict"] == "needs_triage"]
    cl = [r for r in results if r["verdict"] == "claimed"]
    ma = [r for r in results if r["verdict"] == "maintainer_authored"]

    print(f"\n## mark_triaged ({len(mt)}) — maintainer already engaged")
    for r in sorted(mt, key=lambda x: x["number"]):
        hook = ", ".join(f"{u} ({r['reasons'][u]})" for u in r["on_the_hook"])
        add = f"  +reviewer {','.join(r['add_reviewers'])}" if r["add_reviewers"] else ""
        print(f"  #{r['number']}  {hook}{add}")
        print(f"      {r['title'][:80]}")

    if ma:
        print(f"\n## maintainer_authored ({len(ma)}) — author can merge it themselves, not auto-labeled")
        for r in sorted(ma, key=lambda x: x["number"]):
            print(f"  #{r['number']}  by {r['author']}  {r['title'][:60]}")

    if cl:
        print(f"\n## claimed ({len(cl)}) — labeled for landing, left alone")
        for r in sorted(cl, key=lambda x: x["number"]):
            print(f"  #{r['number']}  claimed_by={','.join(r['claimed_by'])}  {r['title'][:60]}")

    print(f"\n## needs_triage ({len(nt)}) — no maintainer engaged")
    for r in sorted(nt, key=lambda x: x["number"]):
        print(f"  #{r['number']}  by {r['author']}  {r['title'][:60]}")

    if not args.apply:
        print(
            f"\n[dry-run] {len(mt)} PR(s) would be labeled triaged. "
            "Re-run with --apply to act.",
            file=sys.stderr,
        )
        return

    print(f"\napplying to {len(mt)} PR(s)…", file=sys.stderr)
    for r in mt:
        try:
            _apply(r)
            add = f" +{','.join(r['add_reviewers'])}" if r["add_reviewers"] else ""
            print(f"  #{r['number']}: triaged{add}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  #{r['number']}: FAILED — {e.stderr.strip()}", file=sys.stderr)
