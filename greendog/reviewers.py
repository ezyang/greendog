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
access not yet revoked") with a note explaining why.  Each observation is
appended to `history` so the file becomes a record of how activity trends,
not just a snapshot.
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
# Reviews in the window needed to count as "actively reviewing".
MIN_REVIEWS_ACTIVE = 3
# Re-observe a login when its record is older than this.
STALE_DAYS = 14
# Flag (don't gate) people with this many open review requests.
OVERLOADED_PENDING = 100
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


def status_of(rec: dict) -> tuple[str, str]:
    """(status, why) for a knowledgebase record, honouring overrides."""
    ov = rec.get("override")
    if ov and ov.get("status"):
        return ov["status"], f"override: {ov.get('note') or 'no note'}"
    obs = rec.get("latest") or {}
    st = classify(obs)
    why = {
        "departed": f"permission={obs.get('permission')}",
        "active": f"{obs.get('reviews_90d')} reviews in {WINDOW_DAYS}d, "
                  f"last seen {obs.get('last_seen')}",
        "cold": f"only {obs.get('reviews_90d')} reviews in {WINDOW_DAYS}d "
                f"(last seen {obs.get('last_seen')}), "
                f"{obs.get('commits_90d')} commits",
    }[st]
    if (obs.get("pending") or 0) >= OVERLOADED_PENDING:
        why += f"; OVERLOADED ({obs.get('pending')} pending requests)"
    return st, why


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
        rec["status"] = classify(obs)
        return rec

    def ensure(self, login: str, today: date | None = None) -> dict:
        """Return a fresh-enough record for `login`, observing if needed."""
        if self.is_stale(login, today):
            self.refresh(login, today)
        return self.data[login]

    def set_override(self, login: str, status: str | None, note: str | None) -> dict:
        rec = self.data.setdefault(login, {"history": []})
        if status is None and note is None:
            rec.pop("override", None)
        else:
            rec["override"] = {
                "status": status, "note": note, "date": _today().isoformat(),
            }
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
        rec = kb.set_override(lg, None if args.clear else args.status,
                              None if args.clear else args.note)
        if not rec.get("latest"):
            kb.refresh(lg)
        kb.save()
        print(_fmt(lg, kb.record(lg)))
        return
    print(f"unknown action {args.action}", file=sys.stderr)
    sys.exit(2)
