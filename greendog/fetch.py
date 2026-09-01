"""Bulk fetchers — one HUD endpoint each. Output is raw JSON, no analysis."""
from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import HudClient

REPO_OWNER = "pytorch"
REPO_NAME = "pytorch"
REPO = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH = "main"

# Effectively-immutable data → long TTL so re-runs are cheap.
IMMUTABLE_TTL_S = 7 * 24 * 3600
# Things that change as commits land → short TTL.
LIVE_TTL_S = 300


def fetch_sevs(client: HudClient) -> list[dict]:
    label = urllib.parse.quote("ci: sev", safe="")
    return client.get_json(f"/api/issue/{label}", ttl=LIVE_TTL_S)


def fetch_hud_grid(client: HudClient, hours: int) -> dict:
    """Walk pages until the oldest commit on a page is past the cutoff.

    Each HUD page has its OWN jobNames column ordering (and may add/drop
    columns), so a job at index i on page 0 is not necessarily the same job at
    index i on page 1. We remap every page's rows onto a single canonical
    jobNames list (a superset, in first-seen order) so downstream code can
    safely align jobs[i] with jobNames[i] across all commits.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    canonical: list[str] = []
    col_of: dict[str, int] = {}
    all_rows: list[dict] = []
    page = 0
    while page < 20:  # safety cap; ~50/page * 20 = 1000 commits
        data = client.get_json(
            f"/api/hud/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{page}",
            ttl=LIVE_TTL_S,
        )
        rows = data.get("shaGrid", []) or []
        if not rows:
            break
        page_names = data.get("jobNames", []) or []
        for name in page_names:
            if name not in col_of:
                col_of[name] = len(canonical)
                canonical.append(name)
        # Remap each row's jobs from this page's column order to canonical.
        for r in rows:
            src = r.get("jobs") or []
            remapped: list[dict] = [{} for _ in canonical]
            for j, job in enumerate(src):
                if j < len(page_names):
                    remapped[col_of[page_names[j]]] = job
            r["jobs"] = remapped
            all_rows.append(r)
        oldest_t = rows[-1].get("time")
        if not oldest_t:
            break
        if _parse_time(oldest_t) < cutoff:
            break
        page += 1
    # canonical may have grown after earlier rows were remapped; pad them all
    # to a uniform width so jobs[i] aligns with jobNames[i] for every row.
    width = len(canonical)
    for r in all_rows:
        jobs = r["jobs"]
        if len(jobs) < width:
            jobs.extend({} for _ in range(width - len(jobs)))
    in_window = [r for r in all_rows if _parse_time(r["time"]) >= cutoff]
    return {"shaGrid": in_window, "jobNames": canonical, "pages_walked": page + 1}


def fetch_advisor_verdicts(client: HudClient, shas: list[str]) -> list[dict]:
    if not shas:
        return []
    return client.clickhouse(
        "advisor_verdicts_for_hud",
        {"repo": REPO, "shas": shas},
        ttl=IMMUTABLE_TTL_S,
    )


def fetch_autorevert_commits(client: HudClient, shas: list[str]) -> list[dict]:
    if not shas:
        return []
    return client.clickhouse(
        "autorevert_commits",
        {"repo": REPO, "shas": shas},
        ttl=IMMUTABLE_TTL_S,
    )


def collect(client: HudClient, hours: int) -> dict:
    print(f"  sevs…", file=sys.stderr)
    sevs = fetch_sevs(client)
    print(f"  hud grid (window={hours}h)…", file=sys.stderr)
    grid = fetch_hud_grid(client, hours=hours)
    shas = [r["sha"] for r in grid["shaGrid"]]
    print(f"  found {len(shas)} commits across {grid['pages_walked']} page(s)", file=sys.stderr)
    print(f"  advisor verdicts…", file=sys.stderr)
    verdicts = fetch_advisor_verdicts(client, shas)
    print(f"  autorevert commits…", file=sys.stderr)
    autorevert = fetch_autorevert_commits(client, shas)
    return {
        "repo": REPO,
        "branch": BRANCH,
        "window_hours": hours,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sevs": sevs,
        "grid": grid,
        "advisor_verdicts": verdicts,
        "autorevert_commits": autorevert,
    }


def _parse_time(s: str) -> datetime:
    # HUD returns ISO with "Z" or with offset.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
