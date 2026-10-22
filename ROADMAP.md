# Roadmap

Status of `customer-registry-sync`. Done items keep the date they landed.

Some items defer to the engine's roadmap: [`gitops-engine/ROADMAP.md`](https://github.com/markddavidoff/gitops-engine/blob/main/ROADMAP.md). Each such item below says in one sentence what the engine item is.

## Done

- [x] **Phase A: Notion ingest (2026-10-06).** `ingest` is a re-runnable Notion API tool. It reads the bucket pages and merges hand-curated inputs (an on-prem deployment registry, classification overrides). It writes a per-customer dataset plus two CSV summaries, with domains, revenue, deployment and contacts. Notion is the source of truth. Linear Customers is a downstream copy.
- [x] **Phase B: Linear-as-code plan/apply engine (2026-10-07).** `sync-inbound` mirrors live Linear into the repo. `generate` projects the dataset into desired customers. `deploy` plans (dry run by default), and `--apply` writes with a per-record check-and-set against the recorded `updatedAt`. Orphans are reported and never deleted. A curated alias file prevents duplicate creates.
- [x] **Phase C: GitOps PR and deploy workflow (2026-10-16).**
  - [x] Desired/state split: `linear/` holds desired config from `generate` and carries no state. `linear-state/` holds the mirror of live Linear from `sync-inbound`.
  - [x] `check`, a read-only gate with content-hash drift classes (changed, added, deleted) plus the plan. Exit 0 means in sync, 2 means operational error, 3 means drift or a plan with aborts.
  - [x] `deploy` recovers the pre-state from the mirror, exits 3 on any aborted record, refuses `--apply` on the configured data directory without `--prod`, and takes `--scope` for test isolation.
  - [x] The Linear client retries HTTP 429 and honors `Retry-After`.
  - [x] Live end-to-end harness against throwaway records. Four scenarios passed against a real workspace (2026-10-16). A probe confirmed that a domain under the reserved `.invalid` top-level domain is accepted, that the content hash and `updatedAt` change on edit, and that deletes are hard deletes.
  - [x] A CI job that runs `check` on each pull request (read-only token, SHA-pinned actions) was verified on a real pull request with the check required and a merge queue enabled (2026-10-16). **That workflow is not included in this repo** (see the open item below).
  - [x] Status-suffixed display names so Linear search shows status, for example `Example Corp [Active]`. The planner's name fallback and alias lookup ignore the suffix (2026-10-16). The renames reach Linear at the next apply.
- [x] **Runs on `gitops-engine` (2026-10-21).** The plan, drift, and check-and-set logic now lives in `gitops-engine`, driven through a `LinearCustomersAdapter`. `deploy` and `check` are thin Linear-specific wrappers; behavior is unchanged and the full suite still passes.

## Open

- [ ] **Ship an example GitHub Actions workflow for the check.** Add a workflow that runs `check` with a read-only `LINEAR_API_TOKEN` on each pull request, so it can be made a required check (optionally behind a merge queue).
- [ ] **Leave unset optional fields unmanaged.** When revenue or tier is unset in the registry, `deploy` must keep the live value instead of clearing it, and report it as untracked. This is ownership rule (i) in the block below. An apply once wiped values that existed only in Linear, and some rows that carry only a tier re-plan forever because the planner keeps trying to reconcile the unset fields.
- [ ] **Report-only orphans.** Linear customers with no desired row are reported and never deleted. Keep it that way. Removal needs an explicit, reviewable step: the retire file, item (iii) in the block below.
- [ ] **Hand deploy automation to `gitops-engine`.** A deploy-on-merge workflow belongs to the engine: after a merge it waits for an environment approval, runs `deploy --apply --prod`, runs `sync-inbound`, and opens a pull request with the refreshed state files. The engine roadmap tracks it as the post-apply state PR workflow.
- [ ] **Require all six Notion bucket ids only for `ingest`.** The Linear-only commands (`deploy`, `check`, `generate`, `sync-inbound`) currently need a config with every Notion bucket id. Relax the config loader so only `ingest` needs them.
- [ ] **Deployment enrichment from a self-managed environments database (config key removed until implemented).** Read deployment details from a Notion database of self-managed installs during `ingest`. The `self_managed_environments_data_source` config key was parsed but never used, so it was removed; add it back with the feature.
- [ ] **Revisit the narrowed ruff rule set.** The repo lints with E4, E7, E9 and F only. Widen it when the code is refactored onto the engine.
- [ ] **Verify the live e2e harness config helper.** The helper that writes a dummy config for the live tests is unverified. Run it against a throwaway workspace.

### Field ownership and convergence (proposal, not yet agreed)

Today `deploy` writes every deployable field from the registry, so a value that exists only in Linear can be overwritten or cleared. This proposal decides, per field, who owns it. The engine's generic counterpart is the ownership and convergence design in the [`gitops-engine` roadmap](https://github.com/markddavidoff/gitops-engine/blob/main/ROADMAP.md).

- [ ] **(i) Ownership rules for Linear Customer fields** (drafted):
  - `name`, `status` and external ids are always written from the registry.
  - `domains` are the registry value unioned with Linear's. Domains that exist only in Linear are never dropped; they are reported as untracked.
  - `revenue` and `tier` are registry-owned when set. When unset in the registry, the Linear value is kept and reported as untracked.
- [ ] **(ii) `absorb` command.** Records untracked Linear values into an enrichment file that `generate` reads. The source dataset wins; enrichment only fills gaps.
- [ ] **(iii) Retire file.** A file `generate` reads, and the only way a value is removed from Linear. Each entry carries the exact current value it removes, and a destructive-change guard checks it before the write. Entries whose value no longer matches Linear are flagged as stale.
- [ ] **(iv) Scheduled drift check.** A scheduled `sync-inbound` that opens a pull request when Linear changed out of band. Depends on the engine's planned drift-PR feature.
- [ ] **(v) Not yet designed.** Only the ownership rules (i) are drafted. The absorb flow, retire flow, CI wiring, credentials and rollout still need a design.
- [ ] **(vi) CI credential for opening pull requests.** Options: a fine-grained personal access token; a GitHub App (the current leaning); or upload the refreshed state as a build artifact and open the pull request by hand.
