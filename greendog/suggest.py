"""Suggest a reviewer for a PR from git history of the exact lines it changes.

The idea: the person who WROTE the lines a PR modifies is usually the person
best placed to review the change.  We blame the PR's changed line-ranges,
aggregate by GitHub login, and pick the dominant non-author owner.

We only suggest when there's a CLEAR owner (a majority of the changed core
lines) who also has REPEAT HISTORY in the area (more than one commit touching
these files) — so we name genuine area experts, not someone who touched the
code once incidentally.  Easy/mechanical PRs with no such owner get no
suggestion and are left for the human triager.

Requires a local pytorch checkout (for `git blame`/`git log`) whose path is
given to the resolvers; GitHub-login resolution uses `gh`.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from typing import Callable

REPO = "pytorch/pytorch"

# Files whose history is a meaningful "who owns this logic" signal.  Tests,
# docs, and CI config are excluded — a PR touching only those is "easy" in the
# sense that owning the code isn't the review bottleneck.
def is_core_path(path: str) -> bool:
    if path.startswith("test/") or "/test_" in path:
        return False
    if path.endswith((".md", ".rst")) or path.startswith(("docs/", ".github/")):
        return False
    return path.startswith(("torch/", "aten/", "c10/", "functorch/"))


# A clear owner must have written at least this fraction of the blamed core
# lines (loose gate — size-agnostic, per Edward: "any clear owner").
OWNER_SHARE_THRESHOLD = 0.5
# Repeat history: the owner must have this many distinct commits touching the
# changed core files (filters incidental one-time touchers).
MIN_OWNER_COMMITS = 2
# Don't trust blame if too few lines carry attribution (pure additions blame to
# the PR itself / are unattributable) — need a real signal to name someone.
MIN_BLAMED_LINES = 4


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


def fetch_pr_meta(number: int) -> dict:
    out = _run(
        ["gh", "pr", "view", str(number), "--repo", REPO,
         "--json", "number,title,author,files"]
    ).stdout
    return json.loads(out)


def fetch_diff_hunks(number: int) -> dict[str, list[tuple[int, int]]]:
    """Map each changed file → list of (start, end) line ranges in the OLD file.

    Only ranges with pre-image lines are useful for blame (pure insertions have
    count 0 and are skipped — you can't blame a line that didn't exist yet).
    """
    diff = _run(["gh", "pr", "diff", str(number), "--repo", REPO]).stdout
    cur: str | None = None
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for line in diff.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? ", line)
        if m and cur:
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            if count > 0:
                out[cur].append((start, start + count - 1))
    return dict(out)


def make_login_resolver(pytorch_dir: str) -> Callable[[str], str | None]:
    """Return `email → github_login` (cached), via a commit under that email."""
    cache: dict[str, str | None] = {}

    def resolve(email: str) -> str | None:
        if email not in cache:
            r = _run(["git", "log", "--all", f"--author={email}",
                      "--format=%H", "-1"], cwd=pytorch_dir)
            login = None
            if r.stdout.strip():
                sha = r.stdout.split()[0]
                j = _run(["gh", "api", f"repos/{REPO}/commits/{sha}",
                          "--jq", ".author.login"])
                login = j.stdout.strip() or None
            cache[email] = login
        return cache[email]

    return resolve


def _blame_line_owners(
    pytorch_dir: str, path: str, ranges: list[tuple[int, int]]
) -> dict[str, int]:
    """email → number of blamed lines across the given ranges of `path`."""
    counts: dict[str, int] = defaultdict(int)
    for (start, end) in ranges:
        r = _run(["git", "blame", f"-L{start},{end}", "--line-porcelain",
                  "--", path], cwd=pytorch_dir)
        if r.returncode != 0:
            continue
        for ln in r.stdout.splitlines():
            if ln.startswith("author-mail "):
                counts[ln.split(" ", 1)[1].strip("<>")] += 1
    return counts


def _file_commit_count(pytorch_dir: str, path: str, emails: set[str]) -> int:
    """Distinct commits touching `path` authored under any of `emails`."""
    r = _run(["git", "log", "--format=%H %ae", "--", path], cwd=pytorch_dir)
    if r.returncode != 0:
        return 0
    n = 0
    for line in r.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1] in emails:
            n += 1
    return n


def suggest_reviewer(
    number: int,
    pytorch_dir: str,
    resolve_login: Callable[[str], str | None] | None = None,
    meta: dict | None = None,
    hunks: dict[str, list[tuple[int, int]]] | None = None,
) -> dict | None:
    """Return a reviewer suggestion, or None if no clear repeat-history owner.

    Result: {reviewer, share, lines, blamed, commits, files, title, author}.
    """
    if resolve_login is None:
        resolve_login = make_login_resolver(pytorch_dir)
    if meta is None:
        meta = fetch_pr_meta(number)
    if hunks is None:
        hunks = fetch_diff_hunks(number)

    author = (meta.get("author") or {}).get("login")
    core_files = [f["path"] for f in (meta.get("files") or []) if is_core_path(f["path"])]
    if not core_files:
        return None  # docs/tests only → easy, leave to triager

    # Blame every changed core line; aggregate by login (people use >1 email).
    lines_by_login: dict[str, int] = defaultdict(int)
    emails_by_login: dict[str, set[str]] = defaultdict(set)
    total_blamed = 0
    for path in core_files:
        for email, c in _blame_line_owners(pytorch_dir, path, hunks.get(path, [])).items():
            total_blamed += c
            login = resolve_login(email)
            if login:
                lines_by_login[login] += c
                emails_by_login[login].add(email)

    if total_blamed < MIN_BLAMED_LINES:
        return None

    # Dominant non-author owner.
    ranked = sorted(
        ((lg, n) for lg, n in lines_by_login.items() if lg != author),
        key=lambda x: -x[1],
    )
    if not ranked:
        return None
    owner, owner_lines = ranked[0]
    share = owner_lines / total_blamed
    if share < OWNER_SHARE_THRESHOLD:
        return None  # no clear owner → leave for triager

    # Repeat history: owner must have >1 commit touching the changed core files.
    commits = 0
    for path in core_files:
        commits += _file_commit_count(pytorch_dir, path, emails_by_login[owner])
    if commits < MIN_OWNER_COMMITS:
        return None  # incidental one-time toucher → don't name them

    return {
        "number": number,
        "title": meta.get("title"),
        "author": author,
        "reviewer": owner,
        "share": round(share, 2),
        "lines": owner_lines,
        "blamed": total_blamed,
        "commits": commits,
        "files": core_files,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

import os
import sys


def _default_pytorch_dir() -> str:
    return os.environ.get("GREENDOG_PYTORCH_DIR") or os.path.expanduser("~/Dev/pytorch")


def _current_reviewers(number: int) -> list[str]:
    out = _run(
        ["gh", "pr", "view", str(number), "--repo", REPO,
         "--json", "reviewRequests",
         "--jq", "[.reviewRequests[]|(.login//.slug)]|join(\",\")"]
    ).stdout.strip()
    return [r for r in out.split(",") if r]


def _is_collaborator(login: str) -> bool:
    """Whether `login` can be requested as a reviewer (repo collaborator).

    Review requests only work for collaborators; the git-history owner may be a
    past contributor who no longer is one, so check before trying to add.
    """
    r = _run(["gh", "api", f"repos/{REPO}/collaborators/{login}",
              "--silent"])
    return r.returncode == 0


def _add_reviewer(number: int, login: str) -> tuple[bool, str]:
    """Request `login` as reviewer via REST (surfaces a real error, unlike
    `gh pr edit`, which exits 0 even when GitHub rejects the request)."""
    r = _run(["gh", "api", "-X", "POST",
              f"repos/{REPO}/pulls/{number}/requested_reviewers",
              "-f", f"reviewers[]={login}"])
    return r.returncode == 0, (r.stderr or "").strip()


def cmd_suggest(args) -> None:
    pytorch_dir = args.pytorch_dir or _default_pytorch_dir()
    check = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=pytorch_dir)
    if check.returncode != 0 or check.stdout.strip() != "true":
        print(f"error: no git checkout at {pytorch_dir} "
              "(set --pytorch-dir or GREENDOG_PYTORCH_DIR)", file=sys.stderr)
        sys.exit(1)

    s = suggest_reviewer(args.number, pytorch_dir)
    if not s:
        print(f"#{args.number}: no clear repeat-history owner — "
              "looks easy, leaving for the triager.")
        return

    existing = _current_reviewers(args.number)
    print(f"#{s['number']} {s['title']}")
    print(f"  suggested reviewer: {s['reviewer']}  "
          f"(wrote {s['lines']}/{s['blamed']} changed core lines = {s['share']}, "
          f"{s['commits']} commits to these files)")
    print(f"  core files: {', '.join(s['files'])}")
    if existing:
        print(f"  already has reviewers: {', '.join(existing)}")

    if s["reviewer"] in existing:
        print("  → already a reviewer; nothing to do.")
        return
    if not _is_collaborator(s["reviewer"]):
        print(f"  → {s['reviewer']} is not a pytorch/pytorch collaborator and "
              "cannot be requested as a reviewer; leaving for the triager.")
        return
    if not args.apply:
        print("  [dry-run] re-run with --apply to add this reviewer.")
        return
    ok, err = _add_reviewer(s["number"], s["reviewer"])
    if ok:
        print(f"  → added {s['reviewer']} as reviewer.")
    else:
        print(f"  → FAILED to add reviewer: {err}", file=sys.stderr)

