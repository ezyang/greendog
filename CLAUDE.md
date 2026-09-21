greendog makes it easier to investigate and fix master CI failures on
pytorch/pytorch, and (second modality) keeps the OSS-PR-triage queue moving.

This file holds only short, generalizable guidance. Add a pattern here only
after confirming with Edward that it is genuinely reusable.

- `docs/ci-investigation.md` — autorevert/merge-skew checklist, target
  determination reference, investigation anti-patterns, marking jobs
  unstable, disabling tests, known persistent breakage.
- `docs/pr-triage.md` — full triage rubric, `greendog triage` / `suggest`
  internals, routing autorules, reviewer knowledgebase, sweep patterns.
- `CASE_NOTES.md` — dated write-ups of specific investigations.

## Design space

- Interventions must be **one-shot** (no local PyTorch build) and simple
  enough to run autonomously; complex fixes need a human.
- **Agents have no internet access.** The harness feeds in information.
- Configs broken >1 week are tracked as **persistently broken**. There are
  a huge number of configs; subdivide into subagents.
- Useful sources: HUD (https://hud.pytorch.org/, sparsely documented API),
  `ci: sev` issues, the autorevert dashboard
  (https://hud.pytorch.org/hud/pytorch/pytorch/main/autorevert).

## Repo workflow

- After making repository changes, stage relevant files and commit before
  reporting back, unless told not to. Never commit generated artifacts,
  caches, virtualenvs, credentials, or unrelated user changes.

## CI analysis methodology

1. **Pick one representative commit** with 90%+ of per-commit jobs
   concluded, ~6h back from HEAD. Don't aggregate failures across a window.
2. **Per-commit jobs only** by default: `pull`, `trunk`, `Lint`. Exclude
   `periodic`, `nightly`, `perf`, `slow`, `benchmark`. Verify with
   `push: branches: [main]` in the workflow YAML; `dynamo-unittest` and
   `inductor-unittest` are cron+ciflow, not per-commit.
3. **Main shards first.** `mem_leak_check` / `rerun_disabled_tests` are
   secondary.
4. **Validate with the window.** Red on one commit, green on neighbors =
   flake. Report only persistent failures or clear green→red transitions.
5. **Cross-reference HUD.** If the analysis disagrees with HUD, the
   methodology is wrong.
6. **Logs are ground truth.** Pull the job log before concluding anything
   about TD, shards, or the named test (HUD's named test is often a fall
   guy). Check all shards. Details in `docs/ci-investigation.md`.

## OSS PR triage essentials

A PR is `mark_triaged` when someone who can **actually merge it per
`merge_rules.yaml`** (path-scoped), not the author or a bot, has reviewed,
commented, or is a requested reviewer (incl. CODEOWNERS). Add them as a
reviewer if missing. `greendog triage` does this deterministically;
`greendog suggest <n>` picks a reviewer from blame + `reviewers.json`.

Hard rules:
- **jansel's `@claude review ...` comments are bot automation, not
  ownership.** High-volume rando reviewers are agent spam.
- `mergedog`-labeled PRs are claimed; leave them alone.
- Check a blame-derived reviewer is still active before assigning.
- **Any bulk sweep MUST be shown to the user and confirmed before any
  mutation.** Never auto-apply.
