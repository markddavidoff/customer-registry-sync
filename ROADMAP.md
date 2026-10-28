# Roadmap

Status of `customer-registry-sync`. Done items keep the date they landed.

Some items defer to the engine's roadmap: [`gitops-engine/ROADMAP.md`](https://github.com/markddavidoff/gitops-engine/blob/main/ROADMAP.md). Each such item below says in one sentence what the engine item is.

## Done

- [x] **Phase A: Notion ingest (2026-10-06).** `ingest` is a re-runnable Notion API tool. It reads the bucket pages and merges hand-curated inputs (an on-prem deployment registry, classification overrides). It writes a per-customer dataset plus two CSV summaries, with domains, revenue, deployment and contacts. Notion is the source of truth for the customers it holds. Linear Customers is a downstream copy, except that `linear-found` stubs (Linear-only customers) are Linear-owned.
- [x] **Phase B: Linear-as-code plan/apply (2026-10-07).** `sync-inbound` mirrors live Linear into the repo. `generate` projects the dataset into desired customers. `deploy` plans (dry run by default), and `--apply` writes with a per-record check-and-set against the recorded `updatedAt`. Orphans are reported and never deleted.
- [x] **Phase C: GitOps PR and deploy workflow (2026-10-16).**
  - [x] Desired/state split: `linear/` holds desired config from `generate` and carries no state. `linear-state/` holds the mirror of live Linear from `sync-inbound`.
  - [x] `check`, a read-only gate with content-hash drift classes (changed, added, deleted) plus the plan. Exit 0 means in sync, 2 means operational error, 3 means drift or a plan with aborts.
  - [x] `deploy` recovers the pre-state from the mirror, exits 3 on any aborted record, refuses `--apply` on the configured data directory without `--prod`, and takes `--scope` for test isolation.
  - [x] The Linear client retries HTTP 429 and honors `Retry-After`.
  - [x] Live end-to-end harness against throwaway records. Four scenarios passed against a real workspace (2026-10-16). A probe confirmed that a domain under the reserved `.invalid` top-level domain is accepted, that the content hash and `updatedAt` change on edit, and that deletes are hard deletes.
  - [x] Status-suffixed display names so Linear search shows status, for example `Example Corp [Active]`.
- [x] **Runs on `gitops-engine` (2026-10-21).** Drift detection and the `check` exit codes come from `gitops-engine`, driven through a `LinearCustomersAdapter`. Planning and apply later moved back into this repository, and v0.2.0 moved them into the engine for good; see the v0.2.0 entry below.
- [x] **Ported the defensive apply, identity table and reusable workflow (2026-10-25).** What landed:
  - [x] Identity table (`identities.yaml`) and one `resolve` for "same company", by source id, then domain, then name. `ingest` appends to it and never rewrites a value.
  - [x] Ordered datasets under `datasets/<name>/` (`notion`, `linear-found`) listed in `datasets.yaml`, with a hard break from the old single `dataset/` directory. `ingest --only linear-found` builds stubs for Linear-only customers from the mirror, offline.
  - [x] Field-level planner with ownership rules. Unset optional fields (revenue, tier) keep the live value and are reported as untracked; Linear-only domains are kept. Known duplicates are reported and never written.
  - [x] Retire file (`linear-retire.yaml`) as the only removal path, with stale-entry detection, and a never-null guard in the client.
  - [x] Ignore list (`linear-ignore.yaml`) and the `ignore` command.
  - [x] Snapshot before the first write, post-write verification, stop on the first error, and `restore` with check-and-set.
  - [x] Generated-file manifests, so `check` fails on hand edits and out-of-date output.
  - [x] Reusable, config-driven GitHub workflow (`registry-sync.yml`) with an example caller: approval-gated deploy and restore, a state PR after each apply, a live `e2e` mode, and a daily read-only refresh that opens a PR. Supporting commands: `changed`, `config-get`, `tool-source`.
  - [x] Removed the alias file, the enrichment file and the `absorb` command; identities replace them.
  - [x] Seams where the ported planner bypasses the engine's `Adapter` are recorded on the engine roadmap.
- [x] **Moved the generic machinery into `gitops-engine` v0.2.0.** Plans, writes, generated and mirrored files, snapshots and exit codes are unchanged for an existing data repository; a golden run on a copy of a real data directory matched v0.1.0 file for file (same sha256s, same plans, same exit codes). What moved and what stayed:
  - [x] The engine now holds the dataset format and writers, the identity table and `resolve`, merge by identity, destination-only stubs, the ignore and retire files, the state mirror, the field planner with ownership policies, check, apply with the snapshot, post-write verification and the never-null guard, restore, `changed`, and the reusable workflow.
  - [x] This repository keeps the declarations: `LINEAR_SCHEMA` (field policies and nouns), `IDENTITY_SPEC` (identity keys), `LinearDestination`, the file names, and the CLI. Tests of the moved mechanisms moved with them, so this repository's test count is lower than at v0.1.0.
  - [x] Source adapters: `registry-sync.yaml` picks one with `source.kind`, and `NotionSource` is the first. The v0.1 top-level `hub_page_id` and `buckets` are now an error that points to `source:`.
  - [x] The caller workflow installs from the caller's locked uv project (`project_dir`) with https git URLs and holds no deploy key. The two Linear tokens are environment secrets named `DESTINATION_TOKEN` and `DESTINATION_READONLY_TOKEN`.

## Open

- [ ] **Ship a pull-request check workflow.** The reusable workflow covers deploy, restore, e2e and refresh. It does not run `check` on pull requests. Add a workflow that runs `check` with a read-only `LINEAR_API_TOKEN` on each pull request and in the merge queue, so it can be a required check.
- [ ] **Close older refresh PRs.** Make the refresh job close its own older refresh PRs. Re-running an old refresh run replays its original commit and opens a PR that conflicts with the default branch.
- [ ] **Adopt or replace foreign external ids.** Decide whether a stub adopts an external id another tool set. Today the sync replaces `externalIds` on every managed customer with the canonical id alone.
- [ ] **Re-adding removed domains.** A domain removed in Linear is re-added from the identity table on the next apply. Project 3 (below) absorbs removals instead.
- [ ] **Exit 2 on a Linear API error.** `sync-inbound`, `deploy` and `restore` exit 1 with a traceback on a Linear API error. Make them exit 2, as `check` does.
- [ ] **Run the live e2e suite once.** The live end-to-end tests (`tests/test_e2e_gitops.py`) are skipped without credentials. Run them against a throwaway workspace, and verify the helper that writes the dummy config for them.
- [ ] **Require all six Notion bucket ids only for `ingest`.** The Linear-only commands (`deploy`, `check`, `generate`, `sync-inbound`) currently need a config with every Notion bucket id. Relax the config loader so only `ingest` needs them.
- [ ] **Deployment enrichment from a self-managed environments database (config key removed until implemented).** Read deployment details from a Notion database of self-managed installs during `ingest`. The `self_managed_environments_data_source` config key was parsed but never used, so it was removed; add it back with the feature.
- [ ] **Revisit the narrowed ruff rule set.** The repo lints with E4, E7, E9 and F only. The refactor onto the engine has landed, so widen the rule set.
- [ ] **Publish v0.2.0 of both repositories.** Until the engine tag exists, this repository's `pyproject.toml` points at a local engine checkout. Tag `gitops-engine` v0.2.0, switch the dependency to its https git URL and tag, then tag this tool, so callers pin tags in their lock and in `uses:`.
- [ ] **Decide repository visibility.** The jobs hold no deploy key and install from the caller's lock over https, so both repositories must be readable without credentials for a caller to install them. A caller in a different owner can call the reusable workflow only if `gitops-engine` is public. The decision is pending.
- [ ] **Move the existing data repository onto v0.2.** Its `registry-sync.yaml` needs `hub_page_id` and `buckets` under `source:` with `kind: notion`, and its workflow needs the locked uv project and environment-held tokens.

## Project 3: the registry owns every written field (proposal)

Not built. This replaces the rule that revenue and tier are owned only when set, and the ignore list. The points below are the proposal as recorded.

- Every written field, including revenue and tier, lives in the registry, generated from the upstream dataset. The owned-when-set rule goes away.
- A Linear edit to an upstream-derived field fails `check`; the refresh absorbs it, and a PR resolves it one of two ways: an **override** entry that pins the value regardless of upstream, or **upstream wins** (the next apply overwrites it). Later: a tool or reconcile app that pushes overrides back upstream.
- An optional value removed in Linear is absorbed into the registry. A required field cleared in Linear is written back from git.
- A customer deleted in Linear is double-confirmed (re-read, and Linear's trash or archive if the API exposes one), then recorded as a `deleted` override.
- A `hidden` override excludes a record from sync; it replaces the ignore list.
