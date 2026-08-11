# greendog case notes

Full forensic write-ups from specific CI investigations. CLAUDE.md keeps
only the short generalizable pattern (the tell, the diagnosis, the fix);
the case-specific detail — dates, line numbers, measured rates, PR
archaeology — lives here. Consult a section only when a current failure
matches its signature.

## 2026-05: ghstack squash divergence (PR #182192)

The landed commit differed from the tested commit. Another PR (#181271)
landed between the ghstack base sync and merge time, touching the same
file. The squash onto main resolved conflicts silently but incorrectly —
tests referenced a method that the conflicting PR had already removed.

To find the conflicting commit: identify the ghstack base
(`gh/<user>/<n>/base`) and main at land time (parent of merge commit),
then `git log <base>..<main-at-land> -- <file>`.

## 2026-05: merge skew unexpected-success (PR #182293)

PR #182293 was autoreverted for "unexpected success" in
`test_impl_device_cpu`. The test ran and passed on PR CI (the expected
failure was still failing as expected). But PR #181328 (dynamo hash
reimplementation) landed in the skew window, fixed the underlying dynamo
tracing issue, and caused the test to start passing on trunk — making
the expected-failure marker stale.

## 2026-07: file_baton lazy-build lock hang (test_mps fall guy)

Named test blamed for the hang:
`test_mps.py::TestBinaryIteratorConformance::test_simple_add_bfloat16_float32_float32_shape2`
on `macos-m2-15` runners (issue #190674, malfet; fixed by #190543
`a84391f`, FileBaton→filelock). The lock lived in
`torch/utils/file_baton.py` (spin loop) before the filelock migration.

Tell: traceback ends in `KeyboardInterrupt` at `file_baton.py` and the
run summary shows `NNN passed ... in ~1796s (0:29:56)` — a wall-clock
timeout, not an assert failure.

Corollary learned here: the owning job had been green for 11+ days when
investigated; the HUD page was showing stale pre-fix hits.

## 2026-07: test_metal_capture SIGSEGV on macos-m2-15

The trunk `macos-py3-arm64 / test (mps, ...)` job runs the normal mps
suite via `run_test.py --mps` AND THEN a separate raw line in
`.ci/pytorch/macos-test.sh` (`test_python_mps`, ~line 44):
`MTL_CAPTURE_ENABLED=1 python3 test/test_mps.py --verbose -k
test_metal_capture`. That test is skipped everywhere else (guarded by
`is_metal_capture_enabled()`); this is the only place it runs. It drives
Apple's `MTLCaptureManager` (`torch.mps.profiler.metal_capture`) to emit
a `.gputrace`, and that Apple path can hard-crash the interpreter:
`macos-test.sh: line 40: NNNNN Segmentation fault: 11 ...` →
`##[error]Process completed with exit code 139`.

Tells that it's THIS and not the file_baton hang: (a) exit 139 /
`Segmentation fault: 11`, instant, NOT `~1796s (0:29:56)` +
`KeyboardInterrupt`; (b) the grep for the segfault line is
`.ci/pytorch/macos-test.sh: line 40`, i.e. the standalone invocation,
AFTER `Finished test_mps 3/3 ... successful` — the 2901-item main suite
passed, so ~all real tests are green and only the capture line killed
the job.

Observed flaky & RUNNER-SPECIFIC: on 2026-07-28 commit `a57db29aa6` it
crashed on `macos-m2-15` but PASSED on `macos-m1-14` (same commit), and
passed on ~17/18 surrounding commits. The commit was unrelated (an
inductor nogpu-skip PR) → it's an infra/GPU flake in Apple's capture
tooling, not a code regression; don't hunt a culprit or revert. No
dedicated disable issue existed as of 2026-07-28 (search
`test_metal_capture`).

Caveat: because it's a raw `python3 ...` line (not run_test.py), it has
NO rerun/flake retry — one Apple crash = hard red job. A `DISABLED
test_metal_capture (__main__.TestMPS)` issue WOULD skip it (the `-k`
invocation still goes through the unittest disable path) but at the cost
of losing metal-capture coverage.

Measured rate (2026-07-26→28, 72 trunk commits): 3 hits, ALL on
`macos-m2-15`, 0 on `macos-m1-14` (~4% of m2-15 runs; m1-14 clean).
Long-standing, not a regression: the test exists since #144561
(2025-01), the CI line since #153012 (2025-05), and the C++ capture path
(`aten/src/ATen/mps/MPSProfiler.mm`) has been stable through 2026.

Root cause (source read, not repro'd): the crash is almost certainly
inside Apple's `MTLCaptureManager` serializing a **device-scope**
`.gputrace` document of a freshly `compile_shader`-compiled pipeline —
`MPSProfiler.mm:807` captures at whole-device scope (stream is nullptr)
with `destination=MTLCaptureDestinationGPUTraceDocument`; there is NO
`MACOS_VERSION`/`@available` guard anywhere in start/stop, so stability
is inherited from the host Metal framework (explains M2-vs-M1 split).
The `startCaptureWithDescriptor:error:` return IS checked
(`MPSProfiler.mm:811` `TORCH_CHECK`), so a failed start = clean
RuntimeError, not the segfault.

Two genuinely-fixable PyTorch defects that could amplify a native crash
(cheap PRs for an MPS owner): (1) `stopCapture` (`MPSProfiler.mm:814-819`)
is UNGUARDED — Python's `finally: _mps_stopCapture()`
(`torch/mps/profiler.py:101`) runs even when start threw, calling
`[captureManager stopCapture]` while not capturing (stop-without-start /
double-stop UB); guard with
`if (captureManager == nil || ![captureManager isCapturing]) return;`.
(2) `stopCapture(nullptr)` does no drain of its own; the only sync is
`torch.mps.synchronize()` in the `try` body, skipped if the `with` body
raises → serializing an in-flight command buffer. Narrowing capture to
command-queue scope (pass the stream at `MPSProfiler.mm:807`) would also
shrink the blast radius.

## 2026-07-27: nogpu shard crashes (test_triton_heuristics)

`test_triton_heuristics.py::TestCheckLauncherCallArgs` et al. red on all
periodic nogpu shards — test setup built a `CachingAutotuner` (needs a
real device) despite a docstring claiming "pure-Python, no GPU".

## 2026-07-28: backwards_compat blamed on test_modules_can_be_imported

Mechanics of the meta-tests: `test_forward_backward_compatibility()` in
`.ci/pytorch/test.sh` runs three failure-injection meta-tests
(`check_public_api_test_fails`, test.sh:~1864) that INTENTIONALLY break
something and assert the public-API test catches it — Step 1&2 make
`test_correct_module_names` fail, Step 3 `mktemp`s a module with
`"invalid syntax garbage"` and makes `test_modules_can_be_imported`
fail; each prints `Success!` on the EXPECTED failure and emits
`Generating XML reports...`, so a failed-XML for those two tests is
written on EVERY run by design. Then the real C++ schema check
(`check_forward_backward_compatibility.py`) runs — a plain script that
emits NO named-test XML. HUD's per-test classifier latches onto the
ever-present meta-test failure — usually `test_modules_can_be_imported`
(Step 3, runs last).

Seen 2026-07-28: commit `569f1f64e1` `Revert "Add keepdim parameter to
cosine_similarity (#189654)"` → `Broken ops:
[aten::cosine_similarity(..., bool keepdim=False)]`, HUD blamed
`test_modules_can_be_imported`. The revert legitimately tripped the
schema check (removing an op parameter is backward-incompatible); the
clean fix is a `check_forward_backward_compatibility.py` ALLOW_LIST
entry in the revert.

## 2026-08-11: binaries red on PR #191638 — rebase triage + the nightly libtorch-extract skip

Question asked: PR #191638 (libtorch-extract `needs`/`!cancelled()`) had 9
red binary jobs; would rebasing clear them? Answer: 8 yes, 1 no.

Method that settled it (generalizable): the same workflow file
(`generated-linux-binary-manywheel-nightly.yml`) runs both on the
`nightly` branch and on every `ciflow/binaries/<pr>` tag, so OTHER
people's recent `ciflow/binaries` runs are a free top-of-main baseline for
these jobs. `gh api "repos/pytorch/pytorch/actions/workflows/<file>/runs?per_page=30"`,
then per-run `jobs?per_page=100 --paginate` and grep the job name.

- The 8 `manywheel-py3_*-rocm7_14-test` reds were `ImportError:
  libatomic.so.1: cannot open shared object file`, fixed on main by
  #192254 `[ROCm 7.14] Install libatomic in the manywheel builder image`
  (`6fa4b8c5749`, 2026-08-05 21:48 UTC) — ~4.6h AFTER the PR's binaries
  run started (17:10 UTC), and not an ancestor of the PR head. Proof from
  the baseline: nightly run 30988588492 (08-05, pre-fix) failed the exact
  same 8 jobs; 31084446145 (08-06) and every nightly since are green.
- `libtorch-rocm7_14-shared-with-deps-release-extract` red is real and
  pre-existing: `OSError: librocprofiler-sdk.so.1` from
  `ctypes.CDLL()` in `.ci/libtorch/smoke_test_extract_libtorch.py`. Fails
  on 5/5 `ciflow/binaries` runs 08-05 → 08-11 (latest: run 31463583592,
  job 93741372050), i.e. on current main. `rocm7_2` extract passes; only
  7.14, added 08-04 by #190276 (TheRock wheels + RPATH) — so its libtorch
  RPATH/bundling is broken, nothing to do with the PR.

Bonus finding (the actual landing risk): **the whole libtorch-extract
matrix is skipped on real `nightly`-branch runs, and libtorch-upload
silently no-ops.** `get-docker-tag` is gated
`if: github.ref_type == 'tag'`, so on branch pushes it is skipped; that
skip propagates down (`manywheel-build` survives via
`!failure() && !cancelled()`, but plain-`if` `libtorch-extract` does not)
and the matrix never expands — HUD/API shows the unexpanded name
`${{ matrix.build_name }}-extract` as `skipped`, which is the tell that a
matrix job died before expansion. `libtorch-*-upload` already carries
`!cancelled()`, so it runs, logs `##[error]Unable to download
artifact(s): Artifact not found for name: libtorch-cpu-shared-with-deps-release`,
and still reports **success**. There is no other libtorch nightly
workflow, so libtorch nightlies are not being published from this path at
all. Adding `!cancelled()` to extract (what #191638 does) fixes that —
and simultaneously makes the broken rocm7_14 leg start reddening nightly
runs that are currently green, which will look like the PR's fault.

Also verified: `.github/templates/*.j2` uses a custom Jinja
`variable_start_string="!{{"` (so GHA `${{ }}` passes through), i.e.
`!{{ n }}` in a template is correct, not a stray `!`. Regenerating at the
PR head produced a byte-identical diff.

**Resolution, same day.** Rebased the stack (worktree at the ghstack
`orig` commit → `git rebase origin/main` → regenerate → amend → `ghstack`;
PR head is now `e9440da440f` on base parent `c00fc935c96`). The rebase
itself was conflict-free, but **regenerating after a rebase is mandatory**:
cu13.4 had been added to main since the PR was written, so the checked-in
generated file was stale w.r.t. its own template by exactly one line
(`manywheel-cuda-cu134-build` missing from the extract `needs`). New
binaries run: 31503480813.

Root cause of the nightly publish outage, confirmed and quantified: Linux
libtorch nightlies stopped at `2.14.0.dev20260629` on ALL channels (cpu,
cu126, cu130, rocm7.2) while `libtorch-win-*` / `libtorch-macos-arm64-*`
in the same index are current — checked by scraping
`https://download.pytorch.org/libtorch/nightly/<channel>/` and taking the
max `dev2026MMDD` per filename prefix. That date is the day #187174
(`1bc58b82340`, atalman, 2026-06-29 15:43 UTC) added the tag-gated
`get-docker-tag`. Every other job in the workflow already carries
`!failure() && !cancelled()` / `!cancelled()` to survive that skip;
`libtorch-extract` was the only holdout. Second half of the bug is in
`_binary-upload.yml`: `Download Build Artifacts` has
`continue-on-error: true` with an NB reading "Binary build jobs can only
be skipped on CI, not nightly" — the invariant #187174 broke — and
`Upload binaries` is gated on `steps.download-artifacts.outcome ==
'success'`, so the leg silently no-ops and reports green.

Filed: #192997 (rocm7.14 libtorch extract — ROCm libs live in the sibling
`_rocm_sdk_core` pip package per `repair_wheel.py:rocm_rpaths()`, so
`copy_libraries` has nothing to copy and `fix_rpath`'s flat `$ORIGIN`
can't reach them; rocm7_2 rpath-fixes 52 libs vs 7.14's 12) and #192998
(the publish outage). Both cross-linked from a comment on the PR.
