greendog is a tool for making it easier to investigate and fix master CI
failures on pytorch/pytorch.  Here is the design space we live in:

- The first iteration of the tool does NOT assume we have a working build of
  PyTorch that we can iterate on.  So we are basically looking for
  interventions that we can *one shot* without having the ability to locally
  test our changes.  This limits the set of potential interventions we can do,
  but that's good because we also want this tool to operate autonomously, and
  if we do complicated interventions it's more important for a human operator
  to intervene.

- We care about "situational awareness" about trunk.  E.g., consider all
  commits in the last 24 hours, what is not working (even if we can't easily fix it?)
  For example, pytorch/pytorch has a concept of ci: sev which is used to communicate
  breakage, we want our agents to have access to this info (example:
  https://github.com/pytorch/pytorch/issues/182227)  For example, the HUD view
  is intended to be a way for humans to visually understand trunk redness, but
  it has gone beyond human parseability.  Another important part of
  situational awareness is the periodic jobs, which we have far less signal
  on, it's much more important to sift out as much info as we can get from the
  logs.

- When reporting "current trunk state", don't focus on HEAD — it typically
  has 1000+ pending/missing jobs and tells us nothing useful.  Instead, look
  back ~6 hours to find commits whose CI has substantially completed.  The
  "trunk HEAD" section of the report should really be "most recent commit
  with meaningful CI results" (i.e., the majority of jobs have a conclusion).

- To add on, flakiness at scale is important, because if something keeps
  flaking at a nontrivial percentage, we should work on it.  We can think of
  stack ranking flakiness in terms of incidence in some period, and using that
  to prioritize work we want to do.

- Our agents do NOT have internet access, for security reasons.  The harness
  is responsible for feeding in information.

- The HUD at https://hud.pytorch.org/ has lots of useful information, in a
  sparsely documented API we have access to that is maintained by Dev Infra.  We should
  document and make use of it as appropriate.  For example, on green-red edges, it seems
  that we already have AI assessments about whether or not something broke master or not.
  These show up like https://github.com/pytorch/pytorch/actions/runs/25282086754 (advisor run).  But it seems these advisor runs don't always run.

- We can only easily test this live.  We'll work on features as we discover
  particular trunk breakages.

- There is an autorevert system.  I don't know how good it is.  We'll be
  evaluating how good it is as we work on this.
  https://hud.pytorch.org/hud/pytorch/pytorch/main/autorevert

- There are some configs that have been presistently broken.  If something's
  been broken for more than a week, let's maintain state about these as
  persistently broken, and we will need a dedicated stab to try to fix them.

- There are a HUGE number of configs. It will be important to subdivide the
  problem appropriately into subagents.

## Repo workflow

- After making repository changes, automatically stage relevant files and
  commit them before reporting back, unless the user explicitly asks not to
  commit. Do not include generated artifacts, caches, virtualenvs, credentials,
  or unrelated user changes in the commit.

## Analysis methodology

When analyzing CI health, follow this approach:

1. **Pick a single representative commit.** Find one recent commit with
   near-complete CI (ideally 90%+ of per-commit jobs concluded). Analyze
   that commit's failures as the "current state of trunk." Don't aggregate
   failure counts across many commits — that conflates current redness with
   regressions that were already autoreverted.

2. **Filter to per-commit jobs by default.** The job grid includes periodic,
   nightly, and perf-benchmark workflows that don't run on every commit.
   Unless specifically asked about periodic jobs, exclude them. Per-commit
   workflow prefixes: `pull`, `trunk`, `Lint`. Exclude: anything with
   `periodic`, `nightly`, `perf`, `slow`, `benchmark` in the workflow name.
   **Caution**: some workflows look per-commit but are actually
   nightly/on-demand hybrids — e.g., `dynamo-unittest` and
   `inductor-unittest` are triggered by cron schedule + ciflow tags, NOT by
   every push to main. Check the workflow YAML triggers in
   `~/Dev/pytorch/.github/workflows/` before assuming a workflow is
   per-commit. A workflow is truly per-commit only if it has
   `push: branches: [main]` (or equivalent) as a trigger.

3. **Distinguish main shards from auxiliary runs.** Each test config runs
   three variants: the main test, a `mem_leak_check` rerun, and a
   `rerun_disabled_tests` rerun. When assessing trunk health, focus on
   main shard failures first. `mem_leak_check` and `rerun_disabled_tests`
   failures are secondary signals.

4. **Use the window to validate failures before reporting them.** Once you've
   identified failures on the representative commit, look across the window
   to check: does the same job fail on neighboring commits too? A job that
   fails on 1 commit but succeeds on the 5 commits before and after it is
   a **flake** — don't report it as breakage. Only report failures that are
   either persistent (failing across multiple commits) or that correspond
   to a clear green→red transition at a specific commit. One-off failures
   are noise at this scale.

5. **Cross-reference with HUD.** The HUD at hud.pytorch.org shows the
   same data visually. If analysis seems wrong (e.g., claiming a config
   is broadly red when HUD shows it green), the analysis methodology is
   likely flawed — revisit assumptions about which jobs and commits are
   being examined.

## Investigating autoreverts and landed-then-broken PRs

When a PR lands and gets autoreverted, the key question is always: why
did CI pass pre-merge but fail post-merge? Follow this checklist —
and after completing the investigation, write up learnings into this
file if the failure mode was novel.

1. **First verify: is the landed commit the same as the tested commit?**
   This is the most important check and should be done early. The PR head
   commit (what CI tests) and the merge commit on main (what actually
   lands) can diverge, especially for ghstack PRs. Compare them with:
   ```
   git diff <pr-head-sha> <merge-commit-sha> -- <relevant files>
   ```
   If they differ, the merge/squash/rebase onto main silently produced
   different code than what was tested. This happened with PR #182192:
   another PR (#181271) landed between the ghstack base sync and merge
   time, touching the same file. The squash onto main resolved conflicts
   silently but incorrectly — tests referenced a method that the
   conflicting PR had already removed.

   To find the conflicting commit: identify the ghstack base
   (`gh/<user>/<n>/base`) and main at land time (parent of merge commit),
   then `git log <base>..<main-at-land> -- <file>`.

2. **Pull actual CI logs to verify test execution.** Don't assume tests
   ran or didn't run — check. Use `gh run view --repo pytorch/pytorch
   --job <job-id> --log` and grep for specific test names. Verify:
   - Did the test file appear in the shard's test list?
   - Did the specific test methods get collected and executed?
   - What was the pass/fail result for each sub-shard?
   Note: `test_aotdispatch.py` is split into 8 sub-shards per CI shard.
   Log access requires no special auth for public repos via `gh` CLI
   (the REST API returns 403 for non-admins, but `gh run view --log`
   works).

3. **Understand the pull vs trunk workflow differences.**
   - `pull` workflow: triggered by `pull_request` event, `PR_NUMBER` is
     set, target determination (TD) is enabled (runs top 25% of tests by
     score). Uses `linux.arm64.m8g.4xlarge` runners for aarch64.
   - `trunk` workflow: triggered by `push` to `main` or `ciflow/trunk/*`,
     `PR_NUMBER` is unset, TD is disabled (runs 100% of tests). Uses
     `lf.linux.arm64.m7g.4xlarge` runners for aarch64.
   - Both run on the same commit SHA for the same PR (via ciflow), but
     the code checked out may differ for `pull_request` events (GitHub
     creates a temporary merge commit).

4. **Don't trust WebFetch summaries of PR content.** AI-summarized PR
   diffs and comments can be wrong about specific details (class names,
   method names, which tests failed). Always verify claims against actual
   code (`git show`, `curl` raw files) and actual logs.

5. **Check for masking by known-flaky tests.** A CI job can fail for
   multiple reasons. If a known-flaky test (e.g., `DivTensorV2`) fails
   in the same shard as a new regression, CI triage may attribute the
   job failure to the known-flaky test, hiding the real issue. The
   `merge -i` (ignore failures) flag then reasonably bypasses what looks
   like pre-existing flakiness.

6. **Check for merge skew (test passed on PR but fails on trunk).**
   This is the subtlest failure mode. The test ran on the PR, passed,
   but fails on the merge commit because other PRs landed between CI
   and merge. Investigation steps:

   a. **Confirm the test actually ran on PR CI.** Pull the logs for
      the specific shard and grep for the test name. Don't assume —
      tests are sharded across multiple jobs and TD may have excluded
      them. Check ALL shards of the relevant config (e.g.,
      `dynamo_wrapped` has 3 shards; `test_custom_ops` may be in
      shard 2, not shard 1).

   b. **If the test ran and passed, compute the skew window.** Get
      the BASE_SHA from the PR CI logs (grep for `BASE_SHA=` in the
      job log) and the parent of the merge commit on trunk:
      ```
      gh api repos/pytorch/pytorch/commits/<merge-sha> --jq '.parents[].sha'
      ```
      Then list commits in the window:
      ```
      gh api repos/pytorch/pytorch/compare/<base-sha>...<trunk-parent> \
        --jq '.commits[] | "\(.sha[0:10]) \(.commit.message | split("\n")[0])"'
      ```

   c. **Search for the culprit in the skew window.** Check which
      commits touch files related to the failure. For dynamo expected
      failure issues, check who originally created the marker file
      (`gh api "repos/pytorch/pytorch/commits?path=<marker-path>"`)
      and look for related PRs in the skew window that touch the
      same subsystem.

   d. **For ghstack PRs, check the entire stack.** If the PR is part
      of a ghstack, the top-of-stack CI includes lower commits. Pull
      CI logs from the TOP of the stack too — if the combined stack
      also passed, the failure is definitely from trunk skew, not
      from the stack itself.

   Example: PR #182293 was autoreverted for "unexpected success" in
   `test_impl_device_cpu`. The test ran and passed on PR CI (the
   expected failure was still failing as expected). But PR #181328
   (dynamo hash reimplementation) landed in the skew window, fixed
   the underlying dynamo tracing issue, and caused the test to start
   passing on trunk — making the expected-failure marker stale.

## Target determination (TD) reference

TD decides which tests to run in pre-merge CI. Key facts:

- Enabled when `PR_NUMBER` is set, not on main branch, not macOS/XPU/ONNX.
- Runs top 25% of tests by aggregated heuristic score; bottom 75% skipped.
- `EditedByPR` heuristic gives score 1.0 (maximum) to any test file
  directly modified by the PR (whole-file granularity).
- Scores are additive across heuristics. Score 0 = no heuristic cares.
- Code: `tools/testing/target_determination/` and consumed in
  `test/run_test.py` at the `get_top_per_tests(percent_to_run)` call.
- TD operates at test-file level primarily; `TestRun` can include/exclude
  specific test classes but most heuristics use full-file `TestRun`s.
- TD runs AFTER other filters. The `--dynamo` flag, `--exclude-*` flags,
  and shard assignment all happen before TD. TD only selects among the
  tests that survive those earlier filters. So if a test doesn't appear
  in TD's "tests to run" OR "excluded" lists, it was filtered out at an
  earlier stage (e.g., not assigned to this shard).
- When TD has no historical timing data for a job name (e.g., new OSDC
  runner infra), it falls back to running ALL tests. Check for the log
  line `Running all tests` vs `Running 25% of tests based on TD`.

## Investigation anti-patterns

Traps to avoid when investigating CI failures:

- **Don't theorize without logs.** Every theory about "TD skipped it" or
  "the test wasn't in this shard" must be verified by pulling actual job
  logs. Theories are cheap; logs are ground truth.
- **Check ALL shards, not just shard 1.** Tests are distributed across
  shards. `test_custom_ops` might be in shard 2 of `dynamo_wrapped`,
  not shard 1. The shard assignment is visible in the TD output or the
  `td_exclusions` artifact.
- **Don't confuse the autorevert confirmation run with the original
  failure.** The autorevert system re-dispatches a filtered workflow
  with `tests-to-include: <failing_test>` to confirm the failure isn't
  a flake. This confirmation run has different inputs than the original
  trunk CI. When investigating, find the ORIGINAL push-triggered trunk
  run, not the autorevert confirmation.
- **Investigate before concluding.** Early in an investigation, resist
  the urge to declare a root cause. Multiple plausible theories can be
  wrong (TD excluded it? no, `--dynamo` excluded it? no, wrong shard?
  no, it ran and passed — it's actually merge skew). Follow the evidence
  step by step.

## Marking CI jobs as unstable

When a job is persistently broken and not worth blocking on, there are
two mechanisms to mark it "unstable":

1. **Add "unstable" to the job name in the workflow YAML.** The trymerge
   bot (`trymerge.py:~1858`) checks `if "unstable" in name` and ignores
   failures for such jobs. Example: a test-matrix entry like
   `{ config: "foo", runner: "...", unstable }` produces a job name
   containing "unstable". This is the lightweight option for individual
   jobs within an otherwise stable workflow.

2. **Move the job from `trunk.yml` / its own workflow into
   `unstable.yml`.** The unstable workflow
   (`.github/workflows/unstable.yml`) runs on every push to main but is
   NOT in `mandatory_checks_name` in `merge_rules.yaml`, so it never
   blocks merging. Jobs graduate back to trunk when red rate < 5% and
   TTS < 3h.

Merge rules (`merge_rules.yaml`) only mandate `pull`, `Lint`, `EasyCLA`
(and sometimes `trunk`, `inductor`). Workflows like `dynamo-unittest`
are already non-mandatory — failures there don't block merging but do
create noise in trunk health and may trigger autorevert.

## Disabling individual tests

To disable a flaky/broken test without a repo PR, create a GitHub issue
in pytorch/pytorch with a title like:

    DISABLED test_method_name (__main__.TestClassName)

The test-infra system picks it up, publishes to S3, and CI skips the
test automatically. Add `Platforms: <platform>` in the issue body to
restrict the disable. Valid platforms: `mac`, `win`, `linux`, `rocm`,
`xpu`, `asan`, `dynamo`, `dynamo_wrapped`, `inductor`, `slow`.
No Python-version filtering is supported.

## Known persistent breakage

Track things we know are broken but are being handled elsewhere, so
sitrep doesn't re-investigate them each time.

- **dynamo-unittest / Python 3.13** — `test_input_no_stdout_fileno`
  (dynamo_core) and `test_namedtuple_default_values_Tensor_type`
  (dynamo_wrapped shard 2) are persistently red. Pending work by
  William Wen on dynamo_wrapped. Non-mandatory workflow, not blocking
  merges. (As of 2026-05-08)

- **vllm multi_model_processor_test** — persistently red, marked
  unstable in the job name. (As of 2026-05-08)

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
`authorAssociation`, both of which over-count: `authorAssociation`
reports bohnstingl/ZhaoqiongZ as `COLLABORATOR` though they only have
`read`; the permission API reports repo write, which isn't the same as
being in a merge rule for this PR's files. We replicate trymerge's own
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
   while assigned as reviewer is evidence they've accepted the review
   (this is the PR-188976 case: Richard/zou3519 was a reviewer and
   commented); OR
3. **was MANUALLY assigned as a reviewer by someone other than the
   author** — a real triage action, counts even if they haven't
   commented yet. If it's an easy PR you may still want to look
   personally to help move it along.

What does NOT count:
- **Codeowner / author auto-assignment.** A silent reviewer who was
  auto-added (the `review_requested` timeline event's `actor` is the PR
  author, added in a batch at open) is NOT evidence of acceptance.
  Criterion 3 requires a non-author, non-bot assigner. Distinguish the
  two via `gh api repos/pytorch/pytorch/issues/<n>/timeline` and reading
  the `actor` on each `review_requested` event.
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
module-owner review" flow). Example: PR #191062 added a ROCm-only OCKL
`CUDA_KERNEL_ASSERT` variant entirely inside `#if defined(USE_ROCM)` in
`torch/headeronly/macros/Macros.h`.

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
(`BOT_COMMAND_PREFIXES`), the claimed-label skip (`CLAIMED_LABELS`), and
manual-vs-codeowner reviewer provenance via the issue timeline
(`make_request_actors_resolver`, fetched lazily only when a silent
merger is a pending reviewer). The two impure resolvers (`can_merge`,
`request_actors`) are injected into `adjudicate` so the classification
logic is unit-testable without network. Needs `gh pr list --json ...,files`
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
(skips if they're already a reviewer). Validated on PR #188996 (FP8
blockwise scaling fix): blames 26/26 changed lines to `jananisriram`, who
introduced blockwise FP8 scaling in Inductor — assigned as reviewer.

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
requested reviewers reveal the coordinator (e.g. #187650 → `fffrog`).

Caveat: these test files usually don't match the campaign's merge-rule
patterns, so the campaign owner can't necessarily *merge* via that rule —
but they're still the correct *reviewer*; a global approver (`albanD`)
or the module owner co-signs the actual merge. Resolution: add the
campaign owner as reviewer, mark triaged. Examples: #191177 (export
test), #191162 (fsdp test_wrap) → both routed to `fffrog`.

#### Liveness check: don't route to people who've left (use the `metamates` team)

Blame/history-based suggestion routinely names the plurality author of the
touched code — but that person may have LEFT. Before assigning a
blame-derived reviewer, verify they're still around. The authoritative,
up-to-date roster of current Meta employees is the GitHub team
**`pytorch/metamates`** (~690 members), fetched via
`gh api orgs/pytorch/teams/metamates/members --paginate --jq '.[].login'`.
This is the same team `merge_rules.yaml`'s "Metamates" `*` rule expands,
and it's kept current (departures are removed) — far more reliable than
the static name list in `merge_rules.yaml` or commit-recency guessing.

Interpretation depends on the candidate's commit email:
- **Meta email (`@fb.com`/`@meta.com`) but NOT in `metamates`** → they've
  left Meta; do NOT route to them. (Verified: `davidberard98`,
  `jamesjwu` — both plurality authors of static-launcher code, both gone.)
- **In `metamates`** → current, safe to assign (`jananisriram`,
  `bobrenjc93`, `eellison`).
- **External contributor (`@intel.com`/`@amd.com`/gmail/etc.)** →
  `metamates` doesn't apply (e.g. `fffrog` is Intel, not a metamate but
  very much active); judge them on their own recent activity / team.

Fallback when the top blame owner is a departed Meta employee: pick the
most recent *active* (in-`metamates`, or externally active) contributor
on the SAME subsystem, optionally paired with a high-commit-volume
adjacent engineer for responsiveness. Example: #191133 ("global scratch
in static Triton launcher") blamed to the departed `davidberard98`/
`jamesjwu`; rerouted to `jananisriram` (current static-launcher feature
owner) + `bobrenjc93` (very active runtime launch-metadata). This
liveness gate is a good future `greendog suggest` enhancement — check
blame-derived logins against `metamates` and drop departed Meta authors.

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
First applied 2026-07-27: swept 12 PRs to `fffrog` (#191080, #191179,
#191160, #191180, #191170, #191094, #191079, #191131, #191087, #191088,
#191090, #191093).

#### Sweep pattern B — human review-like engagement → assign the human + triage

Archetype: PR #191086, where `Skylion007` (a Core Reviewer) left a
substantive human review comment ("is there no way to optimize this fast
path … without template bloat?"). That's real engagement → the engaged
human should be a reviewer, and the PR gets `triaged`. Sweep the queue for
PRs carrying evidence of **human** review-like action — a real review
state (CHANGES_REQUESTED / COMMENTED / APPROVED) or a substantive comment
(design question/critique), by a non-author, non-bot human. Then add that
human as reviewer (if missing) + triage. This is the same spirit as
`greendog triage` criterion 1, done as a manual sweep.
Disqualifications: all bots (`pytorch-bot`, `pytorchmergebot`,
`pytorchbot`, `facebook-github-bot`, `claude`, `*bot`) AND **jansel** —
his `@claude review these changes` are bot automation, NOT human
engagement (per the jansel note above), so a jansel comment alone never
qualifies a PR.
