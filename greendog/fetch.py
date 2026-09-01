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

    Each page has its own jobNames list (the set of jobs that ran on that
    page's commits), and each row's jobs array is positionally aligned to
    ITS page's jobNames — pages differ wildly (page 0 vs 3 shared only 6
    positions when checked). So rows must be re-aligned onto a union name
    list before pages can be concatenated.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pages: list[tuple[list[str], list[dict]]] = []
    page = 0
    while page < 20:  # safety cap; ~50/page * 20 = 1000 commits
        data = client.get_json(
            f"/api/hud/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{page}",
            ttl=LIVE_TTL_S,
        )
        rows = data.get("shaGrid", []) or []
        if not rows:
            break
        pages.append((data.get("jobNames", []) or [], rows))
        oldest_t = rows[-1].get("time")
        if not oldest_t:
            break
        oldest = _parse_time(oldest_t)
        if oldest < cutoff:
            break
        page += 1

    union_names: list[str] = []
    index: dict[str, int] = {}
    for names, _ in pages:
        for n in names:
            if n not in index:
                index[n] = len(union_names)
                union_names.append(n)

    all_rows: list[dict] = []
    for names, rows in pages:
        col = [index[n] for n in names]
        for r in rows:
            jobs = r.get("jobs") or []
            aligned: list[dict] = [{} for _ in union_names]
            for i, j in enumerate(jobs):
                if i < len(col) and j:
                    aligned[col[i]] = j
            r["jobs"] = aligned
            all_rows.append(r)

    in_window = [r for r in all_rows if _parse_time(r["time"]) >= cutoff]
    return {"shaGrid": in_window, "jobNames": union_names, "pages_walked": page + 1}


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
