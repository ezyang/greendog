"""greendog CLI: fetch + report on pytorch/pytorch trunk health."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import auth
from .benchmark import cmd_benchmark
from .client import HudClient, PROJECT_ROOT
from .diff import render_diff
from .fetch import collect
from .report import render
from .triage import cmd_triage
from .suggest import cmd_suggest

RUNS_DIR = PROJECT_ROOT / "runs"


def cmd_run(args):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    client = HudClient(offline=args.offline)
    print(f"greendog: window={args.hours}h{' [offline]' if args.offline else ''}", file=sys.stderr)
    data = collect(client, hours=args.hours)
    raw = run_dir / "raw.json"
    raw.write_text(json.dumps(data, indent=2))
    s = client.stats
    print(
        f"  cache: {s['hits']} hit / {s['misses']} miss · "
        f"{s['requests']} request(s) · {s['bytes']:,} bytes",
        file=sys.stderr,
    )
    report = render(data)
    out = run_dir / "report.md"
    out.write_text(report)
    print(report)
    print(f"\n[wrote {out.relative_to(PROJECT_ROOT)}]", file=sys.stderr)


def cmd_report(args):
    p = Path(args.run)
    raw = p / "raw.json" if p.is_dir() else p
    data = json.loads(raw.read_text())
    report = render(data)
    out = raw.parent / "report.md"
    out.write_text(report)
    print(report)
    print(f"\n[wrote {out}]", file=sys.stderr)


def cmd_diff(args):
    def _load(path_str):
        p = Path(path_str)
        raw = p / "raw.json" if p.is_dir() else p
        return json.loads(raw.read_text())

    before = _load(args.before)
    after = _load(args.after)
    report = render_diff(before, after)
    print(report)


def main():
    ap = argparse.ArgumentParser(prog="greendog")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch + render a fresh report")
    p_run.add_argument("--hours", type=int, default=24, help="lookback window")
    p_run.add_argument("--offline", action="store_true", help="cache only, no network")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="re-render report from raw.json")
    p_report.add_argument("run", help="path to runs/{ts} dir or raw.json file")
    p_report.set_defaults(func=cmd_report)

    p_auth = sub.add_parser(
        "auth", help="configure HUD auth (bot token or Chrome cookies)"
    )
    p_auth.add_argument("--bot-token", help="HUD internal bot token (from Keeper)")
    p_auth.add_argument("--user-agent", help="override Chrome UA string")
    p_auth.set_defaults(func=auth.cmd_auth)

    p_bench = sub.add_parser(
        "benchmark", help="fetch benchmark time series from HUD"
    )
    p_bench.add_argument("--start", required=True, help="start time (ISO 8601)")
    p_bench.add_argument("--stop", required=True, help="stop time (ISO 8601)")
    p_bench.add_argument("--arch", default="h100", help="GPU arch (default: h100)")
    p_bench.add_argument("--device", default="cuda", help="device (default: cuda)")
    p_bench.add_argument("--dtype", default="bfloat16", help="dtype (default: bfloat16)")
    p_bench.add_argument("--mode", default="inference", help="mode (default: inference)")
    p_bench.add_argument("--suites", help="comma-separated suites (default: torchbench,huggingface,timm_models)")
    p_bench.add_argument("--granularity", default="hour", help="time granularity (default: hour)")
    p_bench.add_argument("--left-commit", help="left commit SHA for comparison")
    p_bench.add_argument("--right-commit", help="right commit SHA for comparison")
    p_bench.add_argument("--raw", action="store_true", help="output raw JSON")
    p_bench.add_argument("--offline", action="store_true", help="cache only, no network")
    p_bench.set_defaults(func=cmd_benchmark)

    p_diff = sub.add_parser("diff", help="compare two runs: what broke, healed, persists")
    p_diff.add_argument("before", help="path to earlier runs/{ts} dir or raw.json")
    p_diff.add_argument("after", help="path to later runs/{ts} dir or raw.json")
    p_diff.set_defaults(func=cmd_diff)

    p_triage = sub.add_parser(
        "triage", help="adjudicate OSS PRs needing triage (maintainer engagement)"
    )
    p_triage.add_argument(
        "--apply", action="store_true",
        help="label mark_triaged PRs + add engaged maintainers as reviewers",
    )
    p_triage.add_argument("--raw", action="store_true", help="output raw JSON verdicts")
    p_triage.set_defaults(func=cmd_triage)

    p_suggest = sub.add_parser(
        "suggest", help="suggest a reviewer for a PR from git history of changed lines"
    )
    p_suggest.add_argument("number", type=int, help="PR number")
    p_suggest.add_argument(
        "--pytorch-dir", help="path to local pytorch checkout "
        "(default: $GREENDOG_PYTORCH_DIR or ~/Dev/pytorch)",
    )
    p_suggest.add_argument(
        "--apply", action="store_true", help="add the suggested reviewer to the PR",
    )
    p_suggest.set_defaults(func=cmd_suggest)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
