"""Reviewer knowledgebase: who is actually reviewing pytorch/pytorch right now.

Blame-based suggestion (`suggest.py`) names whoever WROTE the code a PR
touches.  That person may have moved teams, left, or simply never review OSS
PRs any more -- write access does not tell us that.  This module keeps a
small, committed knowledgebase (`reviewers.json` at the repo root) of
per-login activity observed from GitHub, and turns it into a yes/no
"should we route review to this person?" answer.

Observed signals per login (all cheap, no local checkout needed):
  - reviews_90d     PRs they reviewed that were updated in the last 90 days
                    (GitHub search `reviewed-by:`; approximate but robust)
  - last_seen       updated_at of the most recent such PR
  - pending         open PRs where they are a requested reviewer (load)
  - commits_90d     commits authored on main in the last 90 days (capped)
  - permission      repo permission (`read` = can't be requested at all;
                    also the cheapest "has left" tell)

Derived status (`classify`):
  - departed  no write access
  - active    reviewed >= MIN_REVIEWS_ACTIVE PRs in the window
  - cold      has access but is not reviewing (commits alone don't count --
              a prolific author who never reviews is not a good reviewer pick)

Manual overrides win over the derived status, so Edward can pin someone as
`active` ("reviews via internal tooling") or `departed` ("left, GitHub
access not yet revoked"), or pin `overloaded` true/false, with a note
explaining why.  Each observation is appended to `history` so the file
becomes a record of how activity trends, not just a snapshot.

Area profile ("revealed preference", `profile()`): the directories that
show up most across the PRs a person recently reviewed.  Someone who is
genuinely overloaded (albanD) should still be roped in on the areas they
demonstrably keep reviewing; `suggest` reports whether a PR's core files
overlap the candidate's profile.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .client import PROJECT_ROOT

REPO = "pytorch/pytorch"
KB_PATH = PROJECT_ROOT / "reviewers.json"

WINDOW_DAYS = 90
# Reviews in the window needed to count as "actively reviewing".  One is
# enough (Edward, 2026-09-10): the point is to drop people who have stopped
# reviewing entirely, not to rank by volume.
MIN_REVIEWS_ACTIVE = 1
# Re-observe a login when its record is older than this.
STALE_DAYS = 14
# Flag (don't gate) people with this many open review requests.  Raw pending
# count is a blunt signal -- fffrog's queue is one PR-heavy campaign of
# near-identical PRs -- so an override can pin `overloaded` either way.
OVERLOADED_PENDING = 100
# Area profile ("revealed preference"): how many recently reviewed PRs to
# sample and how deep a directory prefix to bucket by.
PROFILE_SAMPLE = 100
PROFILE_DEPTH = 3
PROFILE_TOP = 25
# Keep at most this many past observations per login.
HISTORY_KEEP = 24

WRITE_PERMS = {"write", "maintain", "admin"}

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

class ObservationError(RuntimeError):
    """GitHub didn't answer; never persist a guess as a real observation."""


def _search_count(run: Runner, q: str) -> tuple[int, str | None]:
    # Search API budget is 30 calls/min; back off and retry on rate limit.
    for attempt in range(4):
        r = run(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}",
                 "-f", "per_page=1", "-f", "sort=updated", "-f", "order=desc"])
        if r.returncode == 0:
            try:
                j = json.loads(r.stdout)
            except json.JSONDecodeError:
                raise ObservationError(f"bad search response for {q!r}")
            items = j.get("items") or []
            latest = items[0].get("updated_at") if items else None
            return int(j.get("total_count") or 0), latest
        if "rate limit" in (r.stderr or "").lower() and attempt < 3:
            time.sleep(25)
            continue
        raise ObservationError(f"search failed for {q!r}: {(r.stderr or '').strip()}")
    raise ObservationError(f"search failed for {q!r}")


def observe(login: str, run: Runner = _run, today: date | None = None) -> dict:
    """Fetch fresh activity signals for `login` from GitHub."""
    today = today or _today()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()

    perm = run(["gh", "api", f"repos/{REPO}/collaborators/{login}/permission",
                "--jq", ".permission"])
    if perm.returncode == 0:
        permission = perm.stdout.strip()
    elif "404" in (perm.stderr or ""):
        permission = "none"
    else:
        raise ObservationError(f"permission lookup failed for {login}: "
                               f"{(perm.stderr or '').strip()}")

    reviews, last_seen = _search_count(
        run, f"repo:{REPO} is:pr reviewed-by:{login} updated:>={since}")
    pending, _ = _search_count(
        run, f"repo:{REPO} is:pr is:open review-requested:{login}")

    c = run(["gh", "api", f"repos/{REPO}/commits?author={login}&since={since}"
             "T00:00:00Z&per_page=100",
             "--jq", "[length, (.[0].commit.committer.date // null)]"])
    commits, last_commit = 0, None
    if c.returncode == 0 and c.stdout.strip():
        try:
            commits, last_commit = json.loads(c.stdout)
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "observed_at": today.isoformat(),
        "permission": permission,
        "reviews_90d": reviews,
        "last_seen": (last_seen or "")[:10] or None,
        "pending": pending,
        "commits_90d": commits,
        "last_commit": (last_commit or "")[:10] or None,
    }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def classify(obs: dict) -> str:
    """Derived status from one observation (ignores overrides)."""
    if obs.get("permission") not in WRITE_PERMS:
        return "departed"
    if (obs.get("reviews_90d") or 0) >= MIN_REVIEWS_ACTIVE:
        return "active"
    return "cold"


def is_overloaded(rec: dict) -> bool:
    ov = rec.get("override") or {}
    if ov.get("overloaded") is not None:
        return bool(ov["overloaded"])
    return ((rec.get("latest") or {}).get("pending") or 0) >= OVERLOADED_PENDING


def status_of(rec: dict) -> tuple[str, str]:
    """(status, why) for a knowledgebase record, honouring overrides."""
    ov = rec.get("override") or {}
    obs = rec.get("latest") or {}
    if ov.get("status"):
        st, why = ov["status"], f"override: {ov.get('note') or 'no note'}"
    else:
        st = classify(obs)
        why = {
            "departed": f"permission={obs.get('permission')}",
            "active": f"{obs.get('reviews_90d')} reviews in {WINDOW_DAYS}d, "
                      f"last seen {obs.get('last_seen')}",
            "cold": f"only {obs.get('reviews_90d')} reviews in {WINDOW_DAYS}d "
                    f"(last seen {obs.get('last_seen')}), "
                    f"{obs.get('commits_90d')} commits",
        }[st]
        if ov.get("note"):
            why += f"; note: {ov['note']}"
    if is_overloaded(rec):
        why += f" [OVERLOADED: {obs.get('pending')} pending requests]"
    return st, why


def _area(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:-1][:PROFILE_DEPTH]) or "(root)"


def profile(login: str, run: Runner = _run, today: date | None = None) -> dict:
    """Directories most often touched by PRs `login` recently reviewed."""
    today = today or _today()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    q = (f"repo:{REPO} is:pr reviewed-by:{login} updated:>={since} "
         "sort:updated-desc")
    gql = ("query($q:String!,$n:Int!){ search(query:$q, type:ISSUE, first:$n){ "
           "nodes{ ... on PullRequest { number files(first:60){ nodes{ path } } } } } }")
    r = run(["gh", "api", "graphql", "-f", f"query={gql}", "-f", f"q={q}",
             "-F", f"n={PROFILE_SAMPLE}"])
    if r.returncode != 0:
        raise ObservationError(f"profile query failed for {login}: "
                               f"{(r.stderr or '').strip()}")
    nodes = json.loads(r.stdout)["data"]["search"]["nodes"]
    counts: dict[str, int] = {}
    for pr in nodes:
        for a in {_area(f["path"]) for f in (pr.get("files") or {}).get("nodes", [])}:
            counts[a] = counts.get(a, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:PROFILE_TOP]
    return {"profiled_at": today.isoformat(), "sample": len(nodes),
            "areas": dict(top)}


def area_overlap(rec: dict, paths: list[str]) -> list[tuple[str, int]]:
    """(area, prs-reviewed-in-area) for each of `paths` inside the profile."""
    areas = (rec.get("profile") or {}).get("areas") or {}
    hits = {}
    for p in paths:
        a = _area(p)
        if a in areas:
            hits[a] = areas[a]
    return sorted(hits.items(), key=lambda kv: -kv[1])


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class ReviewerKB:
    path: Path = KB_PATH
    run: Runner = _run
    data: dict | None = None
    consulted: list | None = None  # [(login, status, why)] this session

    def __post_init__(self) -> None:
        self.consulted = []
        if self.data is None:
            self.data = self.load()

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(dict(sorted(self.data.items())), indent=2, sort_keys=True) + "\n"
        )

    def record(self, login: str) -> dict | None:
        return self.data.get(login)

    def is_stale(self, login: str, today: date | None = None) -> bool:
        rec = self.data.get(login)
        if not rec or not rec.get("latest"):
            return True
        seen = date.fromisoformat(rec["latest"]["observed_at"])
        return ((today or _today()) - seen).days >= STALE_DAYS

    def refresh(self, login: str, today: date | None = None) -> dict:
        """Observe `login` now and append to its history; returns the record."""
        obs = observe(login, self.run, today)
        rec = self.data.setdefault(login, {"history": []})
        prev = rec.get("latest")
        if prev:
            rec["history"] = ([prev] + rec.get("history", []))[:HISTORY_KEEP]
        rec["latest"] = obs
        return rec

    def ensure(self, login: str, today: date | None = None) -> dict:
        """Return a fresh-enough record for `login`, observing if needed."""
        if self.is_stale(login, today):
            self.refresh(login, today)
        return self.data[login]

    def set_override(self, login: str, *, status: str | None = None,
                     note: str | None = None, overloaded: bool | None = None,
                     clear: bool = False) -> dict:
        rec = self.data.setdefault(login, {"history": []})
        if clear:
            rec.pop("override", None)
            return rec
        ov = rec.setdefault("override", {})
        if status is not None:
            ov["status"] = status
        if note is not None:
            ov["note"] = note
        if overloaded is not None:
            ov["overloaded"] = overloaded
        ov["date"] = _today().isoformat()
        return rec

    def reprofile(self, login: str, today: date | None = None) -> dict:
        rec = self.data.setdefault(login, {"history": []})
        rec["profile"] = profile(login, self.run, today)
        return rec

    def eligible(self, login: str) -> bool:
        """Should `suggest` route a review to `login`?  Observes lazily."""
        try:
            rec = self.ensure(login)
        except ObservationError as e:
            self.consulted.append((login, "unknown", str(e)))
            return False
        st, why = status_of(rec)
        self.consulted.append((login, st, why))
        return st == "active"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt(login: str, rec: dict) -> str:
    st, why = status_of(rec)
    obs = rec.get("latest") or {}
    return (f"{login:<22} {st:<9} rev90={obs.get('reviews_90d', '?'):<4} "
            f"pending={obs.get('pending', '?'):<4} commits90={obs.get('commits_90d', '?'):<4} "
            f"perm={obs.get('permission', '?'):<8} seen={obs.get('last_seen') or '-':<10} "
            f"| {why}")


def cmd_reviewers(args) -> None:
    kb = ReviewerKB()
    if args.action == "show":
        logins = args.logins or sorted(kb.data)
        for lg in logins:
            rec = kb.record(lg)
            print(_fmt(lg, rec) if rec else f"{lg:<22} (unknown; run `reviewers refresh {lg}`)")
        return
    if args.action == "refresh":
        logins = args.logins or sorted(kb.data)
        for lg in logins:
            if args.force or kb.is_stale(lg):
                try:
                    kb.refresh(lg)
                except ObservationError as e:
                    print(f"{lg:<22} ERROR {e}", file=sys.stderr)
                    continue
                print(_fmt(lg, kb.record(lg)))
            else:
                print(_fmt(lg, kb.record(lg)) + "  (fresh, skipped)")
        kb.save()
        return
    if args.action == "set":
        (lg,) = args.logins
        ov = None if args.overloaded is None else (args.overloaded == "yes")
        rec = kb.set_override(lg, status=args.status, note=args.note,
                              overloaded=ov, clear=args.clear)
        if not rec.get("latest"):
            kb.refresh(lg)
        kb.save()
        print(_fmt(lg, kb.record(lg)))
        return
    if args.action == "profile":
        logins = args.logins or sorted(kb.data)
        for lg in logins:
            try:
                rec = kb.reprofile(lg)
            except ObservationError as e:
                print(f"{lg:<22} ERROR {e}", file=sys.stderr)
                continue
            pf = rec["profile"]
            print(f"{lg}: {pf['sample']} PRs reviewed in {WINDOW_DAYS}d; top areas:")
            for a, n in list(pf["areas"].items())[:12]:
                print(f"    {n:>3}  {a}")
        kb.save()
        return
    print(f"unknown action {args.action}", file=sys.stderr)
    sys.exit(2)
