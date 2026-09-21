# OSS PR triage reference

Full rubric and tooling notes for the PR-triage modality, referenced from CLAUDE.md.

## OSS PR triage modality

A second greendog modality (beyond CI health): keep the pytorch/pytorch
OSS-PR-triage queue moving. The queue is the set of open, non-draft PRs
that carry the `open source` label but NOT the `triaged` label and are
not yet approved. Maintainers are supposed to look at each and either
engage or apply `triaged`; the queue accretes when nobody does.

### The search query

The canonical "needs triage" GitHub search:

```
is:pr repo:pytorch/pytorch base:main -label:triaged draft:false
label:"open source" NOT WIP NOT TESTING in:title -review:approved
sort:updated-desc is:open
```

Fetch it via `gh api -X GET search/issues -f q='<query>' -f per_page=100`.
Then pull per-PR detail with
`gh pr view <n> --repo pytorch/pytorch --json number,title,author,createdAt,updatedAt,labels,reviewRequests,reviews,comments`.

### Step 1: "is a maintainer already engaged?"

The first (and currently only) triage step: if someone who can ACTUALLY
MERGE the PR is ALREADY engaged, we can just bulk-mark it `triaged` — a
human is on the hook, so it isn't stuck.

**"Can actually merge it" = satisfies `merge_rules.yaml` for this PR.**
Merges go through `@pytorchbot merge`, which enforces
`.github/merge_rules.yaml`: a PR can be merged only when approved by
someone in the `approved_by` list of a rule whose `patterns` cover ALL
the PR's changed files. So merge rights are **path-scoped**, not a global
write bit:
- Global approvers — rules with pattern `*` (Metamates ~91, Core
  Reviewers ~14, Core Maintainers ~9) — can merge ANY PR.
- Scoped approvers — e.g. the ROCm rule's `jeffdaily`, the XPU rule's
  `EikanWang`, the MPS rule's `jhavukainen`/`malfet`/`kurtamohler` — can
  merge only PRs whose EVERY changed file matches their rule's patterns.

This is stricter and more accurate than the repo permission API or
`authorAssociation`, both of which over-count: `authorAssociation` can
report `COLLABORATOR` for people with only `read`, and the permission
API reports repo write, which isn't the same as being in a merge rule
for this PR's files. We replicate trymerge's own
glob→regex (`patterns_to_regex`) and "all files must match" logic so our
notion of "can merge" matches what the bot would allow. Teams in
`approved_by` (e.g. `pytorch/pytorch-dev-infra`) are expanded via the
org teams API.

A PR hits the bar (→ `mark_triaged`) when a person who can merge THIS PR
(per the above), who is NOT the PR author and NOT a bot (`claude`,
`pytorch-bot`, `pytorchmergebot`, `pytorchbot`, `facebook-github-bot`,
`*bot`):

1. **left a substantive review or comment** — any real review state
   (CHANGES_REQUESTED / COMMENTED / APPROVED) or a substantive comment
   (design discussion, questions, requesting changes); OR
2. **is a requested reviewer AND left any comment at all** — even a
   mechanical bot command like `@claude review this please`. Commenting
   while assigned as reviewer is evidence they've accepted the review; OR
3. **is a requested reviewer at all** — however they got there: manual
   triage, the author's own pick, or CODEOWNERS auto-assignment. The
   codeowners are the people who can and should land the PR, so a silent
   codeowner-assigned merger still means the PR has an owner (decision
   2026-09-10; e.g. #195633 → janeyx99/albanD via `/torch/optim/`). If
   it's an easy PR you may still want to look personally to help move it
   along. Caveat: this puts silent load on heavy codeowners (albanD got
   21 of 48 in the first sweep) — watch that it doesn't become a dumping
   ground.

What does NOT count:
- **A requested reviewer who can't merge this PR.** Merge rights are
  path-scoped (see above); a requested reviewer outside the applicable
  `merge_rules` entry is not an owner. Neither is a rando reviewer with
  a high review volume (e.g. `sylvesterkaczmarek`) — that pattern is
  LLM-agent spam, not engagement.
- **Mechanical drive-bys by a non-reviewer.** `@pytorchbot fix-lint` or
  `@claude review ...` from someone who is NOT a requested reviewer is
  not engagement (see the jansel note below). These only count under
  criterion 2, i.e. when the commenter is also a reviewer.
- **jansel's `@claude review these changes` is NOT ownership.** Jason
  Ansel runs bot automation that leaves `@claude review these changes`
  on OSS PRs to help unstick CI; per direct agreement with him this does
  NOT mean he's signed up to review or land the PR. Do not mark such PRs
  triaged on that basis alone (unless he's also a reviewer → criterion 2,
  or left a real human review → criterion 1).

**Backend-ifdef'd core files: scoped owner review + global rubber-stamp
is fine.** A PR can be labeled `module: rocm` (or xpu, etc.) yet change
only a *core* file whose path doesn't match the backend's `merge_rules`
patterns (`**rocm**`/`**hip**`), so the scoped owner (e.g. `jeffdaily`)
can't merge it via that rule. But if the ENTIRE diff is guarded behind
the backend ifdef (`#if defined(USE_ROCM)...` — the non-backend path
byte-for-byte unchanged), the module owner's review is still the
substantive gate; a global approver (Edward) just needs a light final
sign-off to merge. `greendog triage` can't detect this — it only sees
file paths, not that the diff is ifdef-guarded — so it's a manual call.
Acceptable resolution: add the module owner AND a merge-capable global
approver as reviewers, then mark triaged (the "bat it to me after
module-owner review" flow).

### Step 2: on-the-hook people must actually be reviewers

If we conclude a maintainer is "on the hook" for a PR, they should be a
requested reviewer on it. When marking triaged, also add the engaged
maintainer via `gh pr edit <n> --repo pytorch/pytorch --add-reviewer <user>`
if they are not already in `reviewRequests`. (Note: `gh pr edit
--add-reviewer` takes ONE reviewer per flag; in zsh, looping
`read -ra` breaks — add reviewers one at a time.)

Apply the label with
`gh pr edit <n> --repo pytorch/pytorch --add-label triaged`.

### mergedog coordination

`mergedog` is Edward's own landing automation. A PR labeled `mergedog`
means someone has claimed it for landing. Whoever claims it should have
marked themselves as a reviewer on the PR — so a `mergedog` PR normally
already shows an engaged maintainer and will be caught by Step 1. Don't
fight over these: if it's under mergedog it's owned. (jansel's
automation similarly now only acts on PRs carrying the `mergedog` label,
and there are `jansel-agent-skip` / `agents-banned` labels that skip his
automation entirely.)

### Workflow implementation note

Step 1 is implemented deterministically as `greendog triage` (see
`greendog/triage.py` + `greendog/mergerules.py`) — no LLM needed. It
encodes the three criteria and their exclusions as code: "can merge this
PR" via `mergerules.py` (fetches `merge_rules.yaml` once, ports
trymerge's `patterns_to_regex` + "all files match", expands teams), the
three engagement criteria in `_classify_pr`, the bot-command filter
(`BOT_COMMAND_PREFIXES`), and the claimed-label skip (`CLAIMED_LABELS`).
The impure `can_merge` resolver is injected into `adjudicate` so the
classification logic is unit-testable without network. (Reviewer
provenance via the issue timeline was removed 2026-09-10 once codeowner
assignment started counting as engagement.) Needs `gh pr list --json ...,files`
so each PR's changed files are available for the scoped merge check.

`greendog triage` prints a dry-run table (with the criterion that fired
per maintainer); `greendog triage --apply` labels the `mark_triaged` PRs
and adds the engaged maintainer as a reviewer where missing.

This was originally prototyped as a subagent workflow (condense each PR,
fan out over chunks, apply the rubric), and the LLM verdicts agreed
exactly with the deterministic scan. Keep the subagent-workflow pattern
in reserve for the *later* triage steps (beyond "is someone engaged?")
that need real judgment; those are not yet built.

### Suggesting a reviewer (for tricky PRs)

Beyond "is someone engaged?", the next triage step is: for a PR nobody
has picked up, name the person best placed to review it. `greendog
suggest <n>` (see `greendog/suggest.py`) does this from git history —
specifically, it blames the EXACT line-ranges the PR changes (parsed from
`gh pr diff` hunk headers, mapped to old-file ranges), aggregates the
authors by GitHub login, and picks the dominant non-author owner.

The design deliberately only fires when the PR is *tricky enough* to be
worth taking off the triager's plate — "tricky" being operationalized as
"there is a clear code owner," not a churn/size metric:

- Only **core** files count (`torch/`, `aten/`, `c10/`, `functorch/`;
  tests/docs/CI excluded). A docs-only or test-only PR → no suggestion,
  left for the triager.
- A **clear owner** must have written ≥50% of the blamed core lines
  (`OWNER_SHARE_THRESHOLD`; loose gate per Edward — size-agnostic).
- The owner must have **repeat history**: ≥`MIN_OWNER_COMMITS` (2)
  distinct commits touching the changed files, so we name genuine area
  experts, not someone who edited a line once incidentally.
- Need ≥`MIN_BLAMED_LINES` (4) attributable lines — pure insertions blame
  to nothing, so a PR that only *adds* code yields no signal.

Key gotchas learned building this:
- **Blame gives emails, not logins, and people commit under several
  emails** (`@fb.com`, `@meta.com`, ghstack noise). Aggregate by resolved
  GitHub login (`email → login` via a commit under that email +
  `gh api .../commits/<sha> --jq .author.login`), not by raw email — else
  the same person's lines split and no owner clears the threshold.
- **`git log --author=<email>` under-counts** because the landed commit's
  author email often differs from the `git config user.email`. Count
  file-touching commits by matching the blame-derived email(s) in
  `git log --format=%H\ %ae -- <path>` instead.
- Requires a **local pytorch checkout** for blame/log
  (`--pytorch-dir`, `$GREENDOG_PYTORCH_DIR`, default `~/Dev/pytorch`);
  its `.git` is a worktree *file*, so probe with `git rev-parse
  --is-inside-work-tree`, not `os.path.isdir(".git")`.

`greendog suggest <n>` prints the suggestion (owner, share, lines,
commits) as a dry-run; `--apply` adds them via `gh pr edit --add-reviewer`
(skips if they're already a reviewer).

#### Routing autorules (stage 1 of `suggest`)

Some areas have a stable crew who want *everything* in the area sent to
them, so no blame is needed. These live as a short table in
`greendog/routes.py` (label / title-regex / path-regex → reviewers) and
run as the first stage of `greendog suggest` (`suggest_by_rule`), ahead
of blame. `greendog triage` calls the same entry point: a `needs_triage`
PR that matches becomes verdict `route` and `--apply` adds the crew +
`triaged`. Current rules (each is an Edward-confirmed decision):
- **rocm** → `jeffdaily`, `jithunnair-amd` (CODEOWNERS crew). Matches
  `module: rocm`, `ciflow/rocm*`, `[ROCm]`/rocm/hip/gfxNNN/miNNN in the
  title, or rocm/hip/hipify/miopen paths. jeffdaily's merge rule only
  covers `**rocm**`/`**hip**` paths, so a global approver co-signs the
  merge. (2026-09-10, #195900)
- **device-agnostic-tests** → `fffrog`. Title matches
  `[Testcase Refactoring]` / `hw_classification`.
Keep the table short and high-precision; add a rule only after confirming
with Edward.

#### Cross-functional campaign PRs → route to the campaign owner, not the module owner

Some PRs belong to a coordinated, repo-wide effort rather than to one
subsystem. The clearest current example is the **device-agnostic /
accelerator-generalization test campaign** — titles like `[Testcase
Refactoring] ...`, `Make <file> device-agnostic`, `Replace CUDA/XPU-only
skips with generic accelerator checks`; changes that swap
`TEST_MULTIGPU`→`TEST_MULTIACCELERATOR`, hardcoded `cuda` →
`instantiate_device_type_tests` + injected `device`, to enable
privateuse1/NPU/XPU backends. These are usually test-only, so `greendog
suggest` declines them (no core-file owner) AND the per-file module owner
(e.g. export→`angelayi`, fsdp→`weifengpy`) is the WRONG reviewer — they
own the subsystem, not the xfn effort.

The right reviewer is whoever drives the campaign across all files. For
the accelerator-generalization effort that's **`fffrog`** (with the
"Accelerator / PrivateUse1" `merge_rules` group: `guangyey`, `EikanWang`,
`albanD` the core sponsor). How to identify the owner for a campaign:
the PR body often @-pings them and/or references a foundational PR whose
requested reviewers reveal the coordinator.

Caveat: these test files usually don't match the campaign's merge-rule
patterns, so the campaign owner can't necessarily *merge* via that rule —
but they're still the correct *reviewer*; a global approver (`albanD`)
or the module owner co-signs the actual merge. Resolution: add the
campaign owner as reviewer, mark triaged.

#### Liveness check: don't route to people who've left (commit-recency is primary)

Blame/history-based suggestion routinely names the plurality author of the
touched code — but that person may have LEFT. Before assigning a
blame-derived reviewer, verify they're still around.

**Cheapest reliable signal: repo permission.** `gh api
repos/pytorch/pytorch/collaborators/<login>/permission` — people who
leave Meta drop to `read` (seen 2026-09-10: XilunWu, colesbury,
davidberard98, zhxchen17, mengluy), and `read` can't be requested as a
reviewer anyway, so `greendog suggest` now skips them (`eligible=`).
`write` doesn't prove *active* though (peterbell10, lezcano, fritzo still
have it), so for a write-access candidate the next check is
**recent commit activity under the person's Meta email**, NOT team
membership. Check:
`git log --since=<~90 days ago> --author='<email>' --oneline | wc -l`
(and eyeball the latest commit date). Recent `@meta.com`/`@fb.com` commits
⇒ still at Meta, full stop.

The GitHub team **`pytorch/metamates`** (~690 members, fetched via
`gh api orgs/pytorch/teams/metamates/members --paginate --jq '.[].login'`,
the same team `merge_rules.yaml`'s "Metamates" `*` rule expands) is a
useful *positive* confirmation — but it is NOT a complete Meta roster: it
has **false negatives** (e.g. `ngimel` is a current, active Meta employee
yet is not in the team). So absence from `metamates` does NOT mean
departed.

Decision rule by candidate commit email:
- **Meta email (`@fb.com`/`@meta.com`)** → CURRENT if EITHER in `metamates`
  OR has recent Meta-email commits. Conclude DEPARTED only when BOTH are
  false (not in `metamates` AND ~0 commits in the last 90 days).
- **External contributor (`@intel.com`/`@amd.com`/gmail/etc.)** →
  `metamates` doesn't apply (e.g. `fffrog` is Intel, not a metamate but
  very much active); judge them on their own recent activity / team.

Fallback when the top blame owner is genuinely departed: pick the most
recent *active* contributor on the SAME subsystem, optionally paired with
a high-commit-volume adjacent engineer for responsiveness.

**Implemented: the reviewer knowledgebase (`reviewers.json` +
`greendog/reviewers.py`).** `greendog suggest` no longer gates blame
candidates on write access alone; it asks the knowledgebase whether the
person is *actively reviewing*. Per login it records observed GitHub
activity — PRs reviewed in the last 90 days (`reviewed-by:` search),
open review requests pending on them (load), commits in the window, repo
permission — and derives `active` (≥1 review/90d — one at all is fine,
Edward 2026-09-10), `cold` (has access, isn't reviewing; commits alone
don't count) or `departed` (no write access). Records go stale after 14
days and are re-observed lazily, each observation is appended to a
per-login `history`, and the file is committed so the base accumulates
across sessions. `suggest` prints the candidates it skipped and why.
- **Overload is a judgment call, not a count.** ≥100 pending requests
  flags OVERLOADED but never excludes. fffrog's queue is one PR-heavy
  campaign of near-identical PRs → pinned `--overloaded no`. albanD is
  genuinely overloaded → pinned `yes`, and should only be roped in on
  his **revealed preference**: `greendog reviewers profile <login>`
  buckets the directories of the last 100 PRs they reviewed; `suggest`
  reports whether an overloaded candidate's profile covers the PR's core
  files ("in their wheelhouse" vs "NOT in their recently-reviewed areas").
- Overrides: `greendog reviewers set <login> [--status ...]
  [--overloaded yes|no] [--note "..."]` (any subset; `--clear` removes);
  `show|refresh [logins]` to inspect/update.
- Search API budget is 30 calls/min (two per observation); refresh backs
  off on rate limit and never persists a failed observation. There is
  **no internal GitHub API proxy** (OSS team won't share bot tokens). The
  internal mirror is Dev Infra's **ClickHouse** (HUD backend, webhook-fed
  PR/issue/comment data; console via SSO at console.clickhouse.cloud,
  programmatic creds from Huy Do / Catherine Lee; docs:
  test-infra wiki "Querying-ClickHouse-database-for-fun-and-profit").
  Review-*request* events may not be stored there; the Hive table
  `aml.fct_pytorch_github_review_events` (devserver only) has them.
  Wiring ClickHouse in as an alternate observer is future work.

#### Double-check jansel-assigned reviewers

jansel runs reviewer-assignment automation, and it picks plausible-but-
wrong reviewers via naive signal — someone with a single tangential commit
to the area, not the actual owner. When you find a PR whose only reviewer
was requested by `jansel` (check the issue timeline
`review_requested` actor), re-derive the owner yourself before trusting it.
Blame the *feature* (`git log -S '<function/symbol>' -- <file>`), not just
the changed line-region, since the line-region is often generic churn —
the jansel-assigned reviewer may have exactly one tangential commit in
the area while someone else authored the whole feature.

### Triage sweeps: ALWAYS confirm before actioning

Any sweep that edits PRs in bulk (adds reviewers, applies `triaged`, etc.)
MUST be presented to the user in this terminal and confirmed BEFORE any
mutation. Gather + classify + show the proposed actions (a table of
PR → reviewer → action, ideally in confidence tiers), then wait for the
user to approve/adjust scope. Never auto-apply a sweep's edits. (Standing
instruction from Edward.)

#### Sweep pattern A — device-agnostic test campaign → `fffrog`

Scan the triage queue for `fffrog`-shaped PRs (see the campaign note
above): test-only PRs making tests device-agnostic / adding PrivateUse1 /
NPU / OOT-backend support, or swapping `torch.cuda`→`torch.accelerator`.
Strong signal = `@fffrog` (and the campaign crew `wjlFlyer`/`FuDdd`/
`JiasenTian`/`jishuangfeng`/`lyriexs666`/`pjyFight`/`JamesD18`) @-pinged in
the PR body, or fffrog already a reviewer. Route to `fffrog` + triage.
When scanning body @-mentions, DROP false pings that are actually code
decorators (`@requires_gpu`, `@skipIfXpu`, `@unittest`,
`@skip_if_lt_x_gpu`, `@requires_accelerator_dist_backend`, …) — match only
real logins. Distinguish from *inductor-XPU enablement* PRs (edit core
`torch/_inductor/**` or bump a triton-xpu submodule) which route to
inductor-XPU owners (`guangyey`/`EikanWang`), not the test campaign.

#### Sweep pattern B — human review-like engagement → assign the human + triage

Sweep the queue for PRs carrying evidence of **human** review-like
action — a real review state (CHANGES_REQUESTED / COMMENTED / APPROVED)
or a substantive comment (design question/critique), by a non-author,
non-bot human. Then add that human as reviewer (if missing) + triage.
This is the same spirit as `greendog triage` criterion 1, done as a
manual sweep.
Disqualifications: all bots (`pytorch-bot`, `pytorchmergebot`,
`pytorchbot`, `facebook-github-bot`, `claude`, `*bot`) AND **jansel** —
his `@claude review these changes` are bot automation, NOT human
engagement (per the jansel note above), so a jansel comment alone never
qualifies a PR.
