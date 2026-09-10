"""Routing autorules: PRs with no engaged maintainer that nonetheless have an
obvious owner set, keyed on labels / title / paths.

This is stage 1 of `greendog suggest` (`suggest_by_rule`, ahead of the
blame-based stage); `greendog triage` calls the same entry point for
`needs_triage` PRs and reports a hit as verdict `route` (add crew, mark
triaged).  Rules are
deliberately dumb and high-precision -- an area whose owners are stable and
who *want* everything in the area sent their way.

Keep this table short.  Each rule needs an Edward-confirmed decision behind
it (see CLAUDE.md "Routing autorules").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

def is_bot(login: str) -> bool:
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


@dataclass(frozen=True)
class Route:
    name: str
    reviewers: tuple[str, ...]
    labels: frozenset[str] = frozenset()      # any label match
    label_prefixes: tuple[str, ...] = ()      # any label startswith
    title: re.Pattern | None = None           # search on title
    paths: re.Pattern | None = None           # search on any changed path
    why: str = ""

    def matches(self, pr: dict) -> bool:
        labels = {l["name"] for l in (pr.get("labels") or [])}
        if labels & self.labels:
            return True
        if any(l.startswith(self.label_prefixes) for l in labels if self.label_prefixes):
            return True
        if self.title and self.title.search(pr.get("title") or ""):
            return True
        if self.paths and any(
            self.paths.search(f["path"]) for f in (pr.get("files") or [])
        ):
            return True
        return False


ROUTES: tuple[Route, ...] = (
    Route(
        name="rocm",
        reviewers=("jeffdaily", "jithunnair-amd"),
        labels=frozenset({"module: rocm"}),
        label_prefixes=("ciflow/rocm", "ciflow/periodic-rocm"),
        title=re.compile(r"\[rocm\]|\brocm\b|\bhip\b|\bgfx\d{3,4}\b|\bmi[23]\d\d\b", re.I),
        paths=re.compile(r"rocm|/hip/|hipify|miopen", re.I),
        why="ROCm CODEOWNERS crew; jeffdaily's merge rule only covers "
            "**rocm**/**hip** paths, so a global approver co-signs the merge "
            "(decision 2026-09-10, #195900)",
    ),
    Route(
        name="device-agnostic-tests",
        reviewers=("fffrog",),
        title=re.compile(r"testcase refactoring|hw_classification", re.I),
        why="accelerator-generalization test campaign is driven by fffrog "
            "(see CLAUDE.md cross-functional campaign note)",
    ),
)


def route_for(pr: dict) -> Route | None:
    for r in ROUTES:
        if r.matches(pr):
            return r
    return None
