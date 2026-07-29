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

    Each HUD page carries its OWN ``jobNames`` list aligned to that page's
    rows' ``jobs`` arrays — the column set (count and order) differs page to
    page as jobs come, go, and get renamed over time. So we cannot keep only
    the first page's ``jobNames`` and index every row by it; ``jobs[j]`` on a
    later-page commit would resolve to the wrong name. Instead we build a
    canonical union of column names and remap every page's jobs into that
    single column space, so ``jobs[j] ↔ jobNames[j]`` holds for all rows.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pages: list[tuple[list[dict], list[str]]] = []
    page = 0
    while page < 20:  # safety cap; ~50/page * 20 = 1000 commits
        data = client.get_json(
            f"/api/hud/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{page}",
            ttl=LIVE_TTL_S,
        )
        rows = data.get("shaGrid", []) or []
        if not rows:
            break
        pages.append((rows, data.get("jobNames", []) or []))
        oldest_t = rows[-1].get("time")
        if not oldest_t:
            break
        if _parse_time(oldest_t) < cutoff:
            break
        page += 1

    # Canonical column space: union of all pages' names, first-seen order.
    canonical: list[str] = []
    col_of: dict[str, int] = {}
    for _, names in pages:
        for n in names:
            if n not in col_of:
                col_of[n] = len(canonical)
                canonical.append(n)

    # Remap each row's jobs from its page's column order into the canonical
    # one, padding absent columns so every row is len(canonical) wide.
    all_rows: list[dict] = []
    for rows, names in pages:
        page_to_canon = [col_of[n] for n in names]
        for row in rows:
            jobs = row.get("jobs") or []
            remapped: list[dict] = [{} for _ in canonical]
            for i, job in enumerate(jobs):
                if i < len(page_to_canon):
                    remapped[page_to_canon[i]] = job
            all_rows.append({**row, "jobs": remapped})

    in_window = [r for r in all_rows if _parse_time(r["time"]) >= cutoff]
    return {"shaGrid": in_window, "jobNames": canonical, "pages_walked": len(pages)}


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
