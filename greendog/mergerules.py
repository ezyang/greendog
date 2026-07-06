"""Model pytorch/pytorch's merge_rules.yaml — "who can actually merge this PR?"

A PR merges via `@pytorchbot merge`, which enforces `.github/merge_rules.yaml`:
a PR can be merged when it is approved by someone in the `approved_by` list of
a rule whose `patterns` cover ALL the PR's changed files.  So "can merge" is
path-scoped, not a global write bit:

  - Global approvers (rules with pattern `*` — Metamates, Core Reviewers,
    Core Maintainers) can merge any PR.
  - Scoped approvers (e.g. the ROCm rule's jeffdaily) can merge only PRs whose
    every changed file matches their rule's patterns.

We replicate trymerge's own glob→regex translation (`patterns_to_regex`) and
its "all files must match" rule so our notion of merge rights matches what the
bot would actually allow.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Any, Callable

import yaml

REPO = "pytorch/pytorch"
MERGE_RULES_PATH = ".github/merge_rules.yaml"


def _patterns_to_regex(patterns: list[str]) -> re.Pattern:
    """Port of pytorch/.github/scripts/gitutils.py:patterns_to_regex.

    Glob semantics: `?` = one char, `*` = non-separator run, `**` = anything.
    Only `.` and `+` are regex-escaped; braces/brackets/backslash are invalid.
    """
    rc = "("
    for idx, pattern in enumerate(patterns):
        if idx > 0:
            rc += "|"
        chars = list(pattern)
        i = 0
        while i < len(chars):
            c = chars[i]
            if c == ".":
                rc += "\\."
            elif c == "+":
                rc += "\\+"
            elif c == "*":
                if i + 1 < len(chars) and chars[i + 1] == "*":
                    i += 1
                    rc += ".*"
                else:
                    rc += "[^/]*"
            else:
                rc += c
            i += 1
    rc += ")"
    return re.compile(rc)


def _all_files_match(patterns: list[str], files: list[str]) -> bool:
    """True iff every file matches a positive pattern and no negative one.

    Mirrors trymerge._find_non_matching_files: a leading `-` marks an exclude.
    """
    positive = [p for p in patterns if not p.startswith("-")]
    negative = [p[1:] for p in patterns if p.startswith("-")]
    pos_re = _patterns_to_regex(positive) if positive else None
    neg_re = _patterns_to_regex(negative) if negative else None
    for f in files:
        if pos_re is None or not pos_re.match(f):
            return False
        if neg_re is not None and neg_re.match(f):
            return False
    return True


def _expand_team(approver: str, gh_team_members: Callable[[str, str], list[str]]) -> list[str]:
    if "/" in approver:
        org, name = approver.split("/", 1)
        return gh_team_members(org, name)
    return [approver]


def fetch_merge_rules() -> list[dict]:
    """Fetch merge_rules.yaml via the GitHub contents API (no local checkout)."""
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{MERGE_RULES_PATH}", "--jq", ".content"],
        capture_output=True, text=True, check=True,
    ).stdout
    return yaml.safe_load(base64.b64decode(out))


def _gh_team_members(org: str, name: str) -> list[str]:
    r = subprocess.run(
        ["gh", "api", f"orgs/{org}/teams/{name}/members", "--jq", ".[].login"],
        capture_output=True, text=True,
    )
    return r.stdout.split() if r.returncode == 0 else []


def make_can_merge_resolver(
    rules: list[dict] | None = None,
    gh_team_members: Callable[[str, str], list[str]] = _gh_team_members,
) -> Callable[[str, list[str]], bool]:
    """Return `can_merge(user, changed_files) -> bool` per merge_rules.yaml.

    Team `approved_by` entries are expanded once and cached.
    """
    if rules is None:
        rules = fetch_merge_rules()

    # Precompute each rule's expanded approver set (teams flattened).
    compiled: list[tuple[list[str], set[str]]] = []
    team_cache: dict[str, list[str]] = {}
    for rule in rules:
        patterns = rule.get("patterns") or []
        approvers: set[str] = set()
        for a in rule.get("approved_by") or []:
            if "/" in a:
                if a not in team_cache:
                    org, name = a.split("/", 1)
                    team_cache[a] = gh_team_members(org, name)
                approvers.update(team_cache[a])
            else:
                approvers.add(a)
        compiled.append((patterns, approvers))

    def can_merge(user: str, changed_files: list[str]) -> bool:
        for patterns, approvers in compiled:
            if user in approvers and _all_files_match(patterns, changed_files):
                return True
        return False

    return can_merge
