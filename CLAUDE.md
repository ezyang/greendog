greendog makes it easier to investigate and fix master CI failures on
pytorch/pytorch, and (second modality) keeps the OSS-PR-triage queue moving.

This file holds only short, generalizable guidance. Detailed playbooks live
in `docs/`, case forensics in `CASE_NOTES.md`. Add a pattern here only after
confirming with Edward that it is genuinely reusable.

- `docs/ci-investigation.md` — autorevert/merge-skew checklist, target
  determination (TD) reference, investigation anti-patterns, marking jobs
  unstable, disabling tests, known persistent breakage.
- `docs/pr-triage.md` — full triage rubric, `greendog triage` / `suggest`
  internals, routing autorules, reviewer knowledgebase, liveness checks,
  sweep patterns.
- `CASE_NOTES.md` — dated write-ups of specific investigations.

## Design space

- Interventions must be **one-shot**: no local PyTorch build to iterate on.
  Keep them simple so the tool can run autonomously; complex fixes need a
  human.
- We care about **situational awareness** of trunk: last-24h breakage,
  `ci: sev` issues, periodic jobs (little signal, so mine the logs), and
  flakiness stack-ranked by incidence.
- **Agents have no internet access.** The harness feeds in information.
- The HUD (https://hud.pytorch.org/) has a sparsely documented API; document
  and use it. Green→red edges sometimes have AI "advisor" assessments.
  There is an autorevert system of unknown quality
  (https://hud.pytorch.org/hud/pytorch/pytorch/main/autorevert).
- Configs broken >1 week are tracked as **persistently broken** and need a
  dedicated stab. There are a huge number of configs; subdivide into
  subagents.
- We can only test live; features arrive as we hit real breakages.

## Repo workflow

- After making repository changes, stage relevant files and commit before
  reporting back, unless told not to. Never commit generated artifacts,
  caches, virtualenvs, credentials, or unrelated user changes.

## CI analysis methodology

1. **Pick one representative commit** with near-complete CI (90%+ of
   per-commit jobs concluded), ~6h back from HEAD (HEAD has 1000+ pending
   jobs). Don't aggregate failures across a window; that conflates current
   redness with already-autoreverted regressions.
2. **Per-commit jobs only** by default: `pull`, `trunk`, `Lint`. Exclude
   `periodic`, `nightly`, `perf`, `slow`, `benchmark`. Some workflows look
   per-commit but are cron+ciflow (e.g. `dynamo-unittest`,
   `inductor-unittest`); check for `push: branches: [main]` in the workflow
   YAML under `~/Dev/pytorch/.github/workflows/`.
3. **Main shards first.** `mem_leak_check` and `rerun_disabled_tests`
   variants are secondary signals.
4. **Validate with the window.** Failing on one commit but green on
   neighbors = flake; report only persistent failures or clear green→red
   transitions.
5. **Cross-reference HUD.** If the analysis disagrees with HUD, the
   methodology is probably wrong.

## Investigation rules of thumb

- **Logs are ground truth.** Never conclude "TD skipped it" or "wrong
  shard" without pulling the job log (`gh run view --job <id> --log`; use
  `gh api repos/pytorch/pytorch/actions/jobs/<id>/logs` for logs >5k lines).
  Check ALL shards.
- **For autoreverts, first check HOW the PR merged** (`merge -i` / `-f`
  land over red), then whether the landed commit equals the tested commit
  (ghstack skew), then merge skew via the BASE_SHA window. Don't confuse the
  autorevert confirmation run with the original trunk run.
- Each test file runs twice per shard (`(serial)` then `(not serial)`); the
  failure summary is at the end of the second run.
- HUD's named test is often a fall guy: lazy `cpp_extension` lock hangs,
  `test_metal_capture` SIGSEGV on m2-15, and `backwards_compat` meta-tests
  all blame the wrong test. See `docs/ci-investigation.md` for tells.
- `nogpu` periodic shards crash any GPU-touching test lacking
  `@requires_gpu`.

## OSS PR triage essentials

Queue = open, non-draft PRs with `open source` but not `triaged`, not
approved (query in `docs/pr-triage.md`). A PR is done (`mark_triaged`) when
someone who can **actually merge it per `merge_rules.yaml`** (path-scoped,
not a global write bit), who is not the author or a bot, has reviewed,
commented, or is a requested reviewer at all (incl. CODEOWNERS). Add them
as a reviewer if missing. Implemented deterministically as `greendog
triage`; `greendog suggest <n>` names a reviewer from blame + the reviewer
knowledgebase (`reviewers.json`); routing autorules live in
`greendog/routes.py`.

Hard rules:
- **jansel's `@claude review ...` comments are bot automation, not
  ownership.** Rando high-volume reviewers (e.g. `sylvesterkaczmarek`) are
  agent spam, not engagement.
- `mergedog`-labeled PRs are claimed; leave them alone.
- Before assigning a blame-derived reviewer, check they're still active
  (repo permission, recent commits, knowledgebase status).
- Device-agnostic test-campaign PRs route to `fffrog`, not the module
  owner. ROCm PRs route to `jeffdaily` + `jithunnair-amd`.
- **Any bulk sweep MUST be shown to the user and confirmed before any
  mutation.** Never auto-apply.
