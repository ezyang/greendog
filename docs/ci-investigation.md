# CI investigation reference

Detailed playbooks referenced from CLAUDE.md. Case-specific forensics live in `CASE_NOTES.md`.

## Investigating autoreverts and landed-then-broken PRs

When a PR lands and gets autoreverted, the key question is always: why
did CI pass pre-merge but fail post-merge? Follow this checklist.

0. **First, check HOW the PR was merged — it often explains landing
   directly.** Read the merge command and the mergebot's response before
   theorizing. `@pytorchbot merge -i` (ignore-current) and `merge -f`
   (force) land over red checks, so a red signal being present pre-merge
   is not a contradiction. Pull the author's command and the mergebot's
   "Merge started ... ignoring the following N checks" comment via
   `gh api repos/pytorch/pytorch/issues/<n>/comments`; that comment lists
   exactly which checks `-i` bypassed. Two tells:
   - **Reverted signal IS in the ignored list** → a real failure was
     visible pre-merge and dismissed (often because that job is
     chronically flaky, e.g. m2-15 mps metal_capture SIGSEGV, so `-i` on
     it is habitual). Dr. CI may reinforce this by bucketing it "unrelated"
     — it keys on whether the *job* was red on the merge base, not on the
     failure *reason*, so a new PR-caused failure in a perennially-red job
     gets hidden.
   - **Reverted signal is NOT in the ignored list** → the failure wasn't
     visible pre-merge; suspect merge skew (go to item 1).
   For ghstack, the merge command lives on the *stack-top* PR, not
   necessarily the reverted one — the mergebot comments "Starting merge as
   part of PR stack under #<top>" on lower PRs; follow that to find the
   command. (Cases: #188029 stale-base skew; #186352 `-i` over a real
   m2-15 mps regression.)

(If an investigation reveals a genuinely novel, *reusable* pattern,
propose a short CLAUDE.md addition and ask before writing it. Full
case-specific forensics — dates, line numbers, measured rates — go in
CASE_NOTES.md, not here.)

1. **First verify: is the landed commit the same as the tested commit?**
   This is the most important check and should be done early. The PR head
   commit (what CI tests) and the merge commit on main (what actually
   lands) can diverge, especially for ghstack PRs. Compare them with:
   ```
   git diff <pr-head-sha> <merge-commit-sha> -- <relevant files>
   ```
   If they differ, the merge/squash/rebase onto main silently produced
   different code than what was tested — typically because another PR
   touching the same file landed between the ghstack base sync and merge
   time. To find the conflicting commit: identify the ghstack base
   (`gh/<user>/<n>/base`) and main at land time (parent of merge commit),
   then `git log <base>..<main-at-land> -- <file>`. (Case: PR #182192,
   see CASE_NOTES.md.)

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

   Skew can also work in reverse: an "unexpected success" autorevert
   can mean another PR in the skew window fixed the underlying issue
   and made an expected-failure marker stale. (Case: PR #182293, see
   CASE_NOTES.md.)

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
- **A named test can be the fall guy for a shared lazy-build/lock hang,
  not the actual failure.** Tests that lazily build a `cpp_extension`
  (e.g. `load_inline`) block on a shared build lock. If a runner has a
  stale lock file, the WHOLE shard hangs until the ~30-min wall-clock
  timeout kills it; pytest then marks the FIRST test that triggered the
  build as `FAILED CONSISTENTLY` — the named test is arbitrary. Tell:
  traceback ends in `KeyboardInterrupt` in the lock spin loop and the
  run summary shows `NNN passed ... in ~1796s (0:29:56)` — a timeout,
  not an assert failure. Infra, not a regression; don't hunt a culprit.
  Corollary: before deep-diving a HUD failure, first check whether the
  owning job is even red on recent trunk — HUD pages can show stale hits.
  (Case: 2026-07 test_mps / file_baton, see CASE_NOTES.md.)
- **A macOS `mps` red is NOT necessarily the lock hang — distinguish an
  instant SIGSEGV from a 30-min timeout.** The trunk mps job also runs a
  standalone `MTL_CAPTURE_ENABLED=1 ... -k test_metal_capture` line in
  `.ci/pytorch/macos-test.sh` (only place this test runs), which drives
  Apple's `MTLCaptureManager` and can segfault the interpreter inside
  Apple's framework. Tell: `Segmentation fault: 11` / exit 139, instant,
  AFTER the main mps suite already passed. Known runner-specific infra
  flake (~4% on `macos-m2-15`, clean on m1-14), long-standing, no
  rerun/retry since it bypasses run_test.py — not a regression, don't
  hunt a culprit. (Full forensics + fixable-defect notes: CASE_NOTES.md.)
- **A test file runs TWICE per shard: `-m '(serial)'` then
  `-m '(not serial)'`.** `run_test.py` splits each file into a serial
  invocation and a non-serial invocation, each running a DIFFERENT subset
  of the file's tests. So a log line like `inductor/test_triton_heuristics
  1/1 was successful` refers ONLY to the serial subset — the non-serial
  invocation of the SAME file runs later and can fail. When grepping a log
  to confirm/deny a test failure, find BOTH `Executing [...] '-m',
  '(serial)'` and `'-m', '(not serial)'` runs; the `FAILED CONSISTENTLY`
  and `The following tests failed consistently: [...]` summary lines live
  at the very end of the second run. Corollary: `gh run view --log` can
  truncate large logs mid-run (~5k lines) — if you don't see the failure,
  fetch the raw log via `gh api repos/pytorch/pytorch/actions/jobs/<id>/logs`
  (returned the full 26k-line log where `gh run view --log` stopped at 5k).
- **`nogpu` shards crash GPU-requiring tests that lack a skip guard.**
  Periodic `nogpu_AVX512` / `nogpu_NO_AVX2` shards run on machines with NO
  NVIDIA driver. Any test whose setup calls `DeviceProperties.create(
  torch.device(GPU_TYPE))` (or otherwise touches `torch._C._cuda_init()`)
  dies with `RuntimeError: Found no NVIDIA driver on your system` unless
  gated by `@requires_gpu()`/`@requires_cuda`. These tests typically only
  appear in the `(not serial)` group, so they pass the serial run and fail
  the non-serial one (see above). A test docstring claiming "pure-Python,
  no GPU" is not a guarantee — check whether its fixtures build a
  `CachingAutotuner`, which needs a real device.
- **`backwards_compat` red is almost never the test HUD names.** The job
  runs deliberate failure-injection meta-tests that write a failed XML
  for `test_correct_module_names` / `test_modules_can_be_imported` on
  EVERY run by design, then the real C++ schema check
  (`check_forward_backward_compatibility.py`), which emits no named-test
  XML. When the job goes red for any reason, HUD's per-test classifier
  latches onto the ever-present meta-test failure. **Ignore the named
  test — scroll to the END of the log for the real cause,** typically a
  `Broken ops: [...]` block. Corollary: reverting a PR that changed an
  ATen op signature legitimately trips this check; the clean fix is an
  ALLOW_LIST entry in the revert. (Mechanics + case: CASE_NOTES.md.)

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
