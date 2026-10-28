# Set up the registry sync

This guide takes a repository from nothing to a working registry sync: the Linear
tokens, the GitHub App, the install source, the GitHub environments, the locked caller
project, the caller workflow, branch protection, and a staged first rollout. Day-to-day
operation, failure recovery, retiring values and restoring snapshots are in
[`operations.md`](operations.md). How the system works is in
[`../customer-registry.md`](../customer-registry.md).

## How the pieces fit

Your repository (the "caller") holds the registry: a `registry-sync.yaml` config, the
data directory, and a small caller workflow. The caller workflow calls the reusable
workflow `.github/workflows/sync.yml` in the `gitops-engine` repository. The reusable workflow installs the tool from the
caller's `uv.lock` and runs one of four modes:

- **deploy**: on a push to the default branch that changed a deploy input, waits for a
  reviewer's approval in the deploy environment, runs `check` and then
  `deploy --apply --prod`, and opens a "state PR" with the refreshed mirror and the
  snapshot.
- **restore**: puts values back from a snapshot, behind the same approval.
- **e2e**: runs the tool's live GitOps tests against throwaway Linear records.
- **refresh**: once a day (and on demand), re-reads Linear without writing to it and
  opens a PR only when a refreshed file changed.

The workflow never reads Notion and holds no Notion token. Notion ingest is manual: on
your own machine you run `customer-registry-sync ingest` (with `NOTION_API_TOKEN` set),
then `generate`, and open a PR with the result.

**Install and visibility.** The caller's repository holds a uv project that depends on
the tool, with a committed `uv.lock` (Stage 5). Each job runs `uv sync --locked` and
calls the tool through `uv run --frozen`, so every job runs the locked tool and
dependencies, and a job that holds a token runs only locked code. The jobs hold no
deploy key, so the lock must use https git URLs or an index that the job can read
without credentials (Stage 3). The reusable workflow is in `gitops-engine`. GitHub can
call it from a caller in any owner when `gitops-engine` is public, and from a caller in
the same owner once Actions access is granted to it.

## Stage 0: Prerequisites

On the operator's machine: [uv](https://docs.astral.sh/uv/), Python 3.12 or later, and
the [GitHub CLI](https://cli.github.com/) (`gh auth login`).

You need:

- A Linear workspace with **Customers** enabled (a workspace setting).
- A Notion integration token with the six bucket pages shared with it, for the manual
  `ingest` on your machine (see [`../notion-template.md`](../notion-template.md)). The
  bucket names are the template's fixed values, so name your pages to match.
- Admin rights on the caller repository (environments, secrets, rulesets).

For local runs, export the tokens in your shell. The tool reads `LINEAR_API_TOKEN`
(`sync-inbound`, `deploy`, `check`, `restore`) and `NOTION_API_TOKEN` (`ingest`).
`deploy` needs the Linear token even as a dry run, and so does `check`; both exit 2
without it. `ignore` and `ingest --only linear-found` need no token but need a state
mirror, so run `sync-inbound` before them.

## Stage 1: Linear tokens

Create two personal API keys in Linear (Settings, Security & access, Personal API keys):

| Key | Scope | Used by |
|---|---|---|
| deploy key | write | `deploy`, `restore` and `e2e`; the deploy environment |
| read-only key | read | `refresh`; the read-only environment (and any PR check) |

If Linear does not offer a read-only scope, use the write key in both places. To rotate
a key, revoke the old one in Linear first, then repeat this stage and Stage 4 with the
new value.

## Stage 2: The GitHub App

The App opens the state and refresh PRs. Pull requests opened with the workflow's
default token do not trigger other workflows, so a required check would never run on
them.

1. In your account or organization settings, open Developer settings, GitHub Apps, New
   GitHub App.
2. Give it a name and set the homepage URL to the caller repository. Clear **Active**
   under Webhook.
3. Repository permissions: **Contents** read and write, **Pull requests** read and
   write. Leave **Workflows** at no access, so the App cannot edit `.github/`.
4. Installable on: **Only on this account**. Create the App.
5. Note the **App ID**. Generate a private key; a `.pem` file downloads once. The App ID
   becomes the `REGISTRY_SYNC_APP_ID` secret and the `.pem` text becomes
   `REGISTRY_SYNC_APP_PRIVATE_KEY`; Stage 4 stores both.
6. Install the App on the caller repository only (Only select repositories).

## Stage 3: The install source

The jobs install from the caller's lock and hold no deploy key. Two rules follow:

- Every git source in the lock is an `https://` URL (or a package index). An
  `ssh://` URL fails in the job, because there is no key to read it with.
- The tool repository and `gitops-engine` must be readable over https without
  credentials. If either is private, the job cannot install it; make it public or
  host the package on an index the job can read.

Pin the tool and the engine by tag in the caller's `pyproject.toml` (Stage 5), and commit
the lock. The lock, not the workflow, decides what code runs, so upgrading means
changing the tag and committing the new lock.

## Stage 4: GitHub environments and secrets

The two environment names are your choice. You write them into `registry-sync.yaml`
under `workflow:` in Stage 5. Only the deploy environment needs a required reviewer.

The two Linear keys live only as **environment secrets**, under the names the reusable
workflow expects:

| Environment | Secret | Holds | Protection |
|---|---|---|---|
| deploy | `DESTINATION_TOKEN` | the Linear write key | required reviewer |
| read-only | `DESTINATION_READONLY_TOKEN` | the Linear read-only key | none |

The caller passes two repository secrets by name, which the reusable workflow declares
under `on.workflow_call.secrets`:

| Secret | Holds | Used by |
|---|---|---|
| `REGISTRY_SYNC_APP_ID` | the GitHub App's id | the state and refresh PRs |
| `REGISTRY_SYNC_APP_PRIVATE_KEY` | the App's private key (the `.pem` text) | the state and refresh PRs |

Rules for where they live:

- Store the App id as a **secret**, not a variable. The reusable workflow reads it from
  `secrets`, and a repository or environment variable does not reach it.
- The caller does **not** pass `DESTINATION_TOKEN` or `DESTINATION_READONLY_TOKEN`. A
  secret of the same name stored in a job's environment takes precedence over a passed
  one, so the write token is released only after the required reviewer approves the
  deploy, and no other workflow can read it. The deploy job runs in the deploy
  environment and the refresh job in the read-only environment, so each gets its own
  token. A repository-level secret of the same name is unnecessary and weakens that gate.
- The workflow exports the token under the tool's own variable (`LINEAR_API_TOKEN`, the
  caller's `token-env` input), only for the steps that call Linear: `check`, `deploy`,
  `restore`, `sync-inbound`, the live tests and `refresh`. Checkout, upload and PR steps
  never see it.
- `secrets: inherit` passes nothing to a workflow owned by another account, so the
  caller maps each App secret explicitly (the example does).

Create the environments and set the values. None of these commands print a value:

```
R=<owner>/<registry repo>
gh api -X PUT repos/$R/environments/<read-only environment>
echo '{"reviewers":[{"type":"User","id":<your GitHub user id>}]}' \
  | gh api -X PUT repos/$R/environments/<deploy environment> --input -

gh secret set DESTINATION_READONLY_TOKEN -R $R --env <read-only environment>   # paste the read-only key
gh secret set DESTINATION_TOKEN -R $R --env <deploy environment>               # paste the write key

printf '%s' "<App ID>" | gh secret set REGISTRY_SYNC_APP_ID -R $R
gh secret set REGISTRY_SYNC_APP_PRIVATE_KEY -R $R < <path to the .pem>
```

The caller passes the repository secrets `REGISTRY_SYNC_APP_ID` and
`REGISTRY_SYNC_APP_PRIVATE_KEY` through. Check the names:

```
gh secret list -R $R; gh secret list -R $R --env <deploy environment>; gh secret list -R $R --env <read-only environment>
```

Delete the `.pem` once the secret holds it.

## Stage 5: The caller repository

The caller needs five things in the repository that holds the registry.

**A uv project that depends on the tool, with a committed `uv.lock`.** The reusable
workflow runs `uv sync --locked` in the directory named by its `project_dir` input
(default: the repository root; set `project_dir` in the caller if the project lives in a
subdirectory), so the lock pins the tool and every transitive dependency. uv reads
`[tool.uv.sources]` only from the root project, so declare the engine's source here too,
at the tag the tool's own `pyproject.toml` pins:

```toml
[project]
name = "my-customer-registry"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = ["customer-registry-sync", "gitops-engine"]

[tool.uv.sources]
customer-registry-sync = { git = "https://github.com/markddavidoff/customer-registry-sync.git", tag = "v0.2.0" }
gitops-engine = { git = "https://github.com/markddavidoff/gitops-engine.git", tag = "v0.2.0" }
```

Run `uv lock`, then commit `pyproject.toml` and `uv.lock`. Upgrading the tool means
changing the tag and committing the new lock. Use `https://` URLs only (Stage 3).

**`registry-sync.yaml`**, copied from
[`examples/registry-sync.example.yaml`](../../examples/registry-sync.example.yaml): your
a `source:` section (`kind: notion`, the hub page id and the six bucket page ids), an
optional `data_dir`, and the two environment names the workflow binds its jobs to:

```yaml
workflow:
  deploy_environment: <deploy environment>
  readonly_environment: <read-only environment>
```

Only these two keys exist under `workflow:`. The CLI works without them; the workflow's
`plan` job reads them with `config-get` and fails naming any missing key.
`data_dir` must stay inside the repository.

**The caller workflow**, copied from
[`examples/caller-workflow.yml`](../../examples/caller-workflow.yml) to
`.github/workflows/registry-sync.yml`. It triggers on a push to `main`, a daily cron
(change the time if you like) and manual dispatch, and passes the two App secrets by
name. Set `token-env: LINEAR_API_TOKEN`, the variable the tool reads. Two points about
`uses:`:

- Pin it to the same release tag as the `gitops-engine` entry in your `pyproject.toml`
  (the example uses `@v0.2.0`), so a change to the engine's default branch cannot change
  your deploys.
- GitHub can call a reusable workflow when its repository is public, or has the same
  owner as the caller and allows access from it (repository Settings, Actions, General,
  Access). Otherwise the call fails before any job starts.

There is no `paths:` filter. Every push starts a run, and its `plan` job ends the run
(`changed --since`) when no deploy input changed. The state mirror and the snapshots
are not deploy inputs, so a state PR never starts a deploy.

**`<data_dir>/datasets.yaml`**, copied from
[`examples/datasets.example.yaml`](../../examples/datasets.example.yaml). It lists
`notion` and `linear-found`. Do this before the first refresh: the scheduled refresh runs
`ingest --only linear-found`, which creates `<data_dir>/datasets/linear-found/`, and
`check` fails with `datasets.yaml: directory datasets/linear-found is not listed` for a
dataset directory the file does not name (`datasets_errors` in
`src/customer_registry_sync/datasets.py`). Without `datasets.yaml` only `notion` is
listed, so `check` fails once the refresh has run and every deploy blocks.

**The registry data.** Put your curated files in `<data_dir>` first; samples of each
are in [`examples/`](../../examples/). Then build the registry once locally and commit
it. `ingest` reads Notion, so it needs `NOTION_API_TOKEN`, and you run it by hand; the
workflow never does. `ingest --only linear-found` needs the mirror that `sync-inbound`
writes, so it comes after it:

```
customer-registry-sync ingest
customer-registry-sync sync-inbound
customer-registry-sync ingest --only linear-found
customer-registry-sync generate
```

## Stage 6: Branch protection and the merge queue

Add one ruleset on the default branch:

- require a pull request (zero approvals is fine for a solo maintainer)
- require the PR check, only if you have added one (see below; none ships)
- block force pushes and deletion
- admin bypass in "pull request" mode only, so a broken check cannot lock the repository

After this, every change reaches the default branch through a PR, including the state
and refresh PRs.

This repository ships no PR-check workflow yet, so there is no status check to require
when you first set up the ruleset. In its place, run `customer-registry-sync check` from
a checkout of your PR branch before you merge (it needs `LINEAR_API_TOKEN`), and rely on
the deploy job: its own `check` step runs before every apply and blocks a bad one.
To gate pull requests on `check` automatically, add your own workflow that runs on
`pull_request` (and `merge_group`), installs the tool the same way, runs
`customer-registry-sync check` with a read-only `LINEAR_API_TOKEN`, and skips fork PRs so
they never receive the token. Then add its job to the ruleset as a required status
check, and add a merge queue if you want the check re-run on the queue's merge commit.

## Stage 7: Verify before the first write

Run these once, before anything writes to Linear:

1. **The registry.** From the caller repository:
   ```
   customer-registry-sync sync-inbound --no-input
   customer-registry-sync check
   ```
   Expect exit 0, `IN SYNC`, `drift changed=0 added=0 deleted=0`, and `orphans=0` and
   `destructive=0` in the `DeployPlan` line. Read every `+` (create) and `~` (update) line of the
   plan, because each one is a write the first deploy makes.
2. **The live end-to-end tests (optional).** They touch only throwaway test customers,
   which the sync always ignores (see [`../customer-registry.md`](../customer-registry.md#a-change-from-linears-side)),
   and clean up after themselves. In GitHub, dispatch the workflow with
   `mode: e2e`; it runs the tests from the exact tool commit your lock installed, and
   uses that tool's own lockfile for the test dependencies. From a checkout of the tool:
   ```
   uv run pytest tests/test_e2e_gitops.py --run-live -q
   ```
   Both need a write-scope `LINEAR_API_TOKEN`.
3. **A refresh.** Dispatch the workflow with `mode: refresh`. Expect either a refresh PR
   or "No refreshed files changed; no PR to open."

## Stage 8: Staged first rollout

Each stage lands as its own PR, and each needs its own deploy approval. Never combine a
structural change with a large data change in one PR. A small PR keeps the plan short
enough to read every line.

1. **Land the tooling.** Merge the PR that adds the config, the lock and the workflow.
   It may start a `deploy` run. Approve it only if its plan matches what you expect.
2. **Bring Linear in step with the registry.** Merge the PR whose plan holds the
   intended creates and updates. Before approving, compare the plan in the run with the
   expected list in the PR description. The deploy snapshots first, writes each row with
   check-and-set, and verifies it after the write.
3. **Land the state PR.** The App opens `linear-state/<run>-<attempt>` after each
   deploy. Its `check` should show `updates=0`. Merge it.
4. **Verify the refresh.** Dispatch the workflow with `mode: refresh` from the current
   default branch. Expect "No refreshed files changed; no PR to open."

Two mistakes to avoid:

- `gh run rerun` replays the run's original commit. Re-running an old refresh after a
  state PR merged produces a stale PR that conflicts with the default branch. Close it
  and dispatch a new run:
  `gh workflow run registry-sync.yml -R $R --ref main -f mode=refresh`.
- An environment's secrets are read when its job starts, after approval. A run that is
  waiting for approval picks up values you add while it waits. A run that has already
  failed does not.

## Steady state

| What | When | Who acts |
|---|---|---|
| PR check (if you added one) | every PR and every merge-queue entry | nobody, unless it fails |
| `deploy` | every push to the default branch that changes `<data_dir>/linear/**`, `identities.yaml`, `linear-ignore.yaml`, `datasets.yaml` or `linear-retire.yaml` under `<data_dir>`, or `registry-sync.yaml` | the reviewer approves |
| state PR | after every deploy | someone merges it |
| `refresh` | daily, or on demand | someone reviews and merges the PR, if one opens |

Curated files are edited by hand, through PRs:

| File | Purpose |
|---|---|
| `<data_dir>/datasets.yaml` | dataset priority order and the Linear status to classification table |
| `<data_dir>/identities.yaml` | one entry per company: canonical id, names, domains, source ids |
| `<data_dir>/linear-ignore.yaml` | Linear customers the sync never touches, each with a reason |
| `<data_dir>/linear-retire.yaml` | values to remove from Linear (the only way the sync removes anything) |
| `<data_dir>/self-managed-customers.yaml`, `<data_dir>/classification-overrides.yaml` | inputs to `ingest` |

Never edit generated files by hand: `<data_dir>/datasets/*/`, `<data_dir>/linear/`,
`<data_dir>/linear-state/` and the two CSVs. `check` fails on a hand edit.

When `check` fails, its lines name the fix. Recovery steps for each failure are in
[`operations.md`](operations.md#runbook-5-recovering-from-a-failing-check).

## Reference: where each secret lives

| Name | Local shell | Repository secret | Deploy environment | Read-only environment |
|---|---|---|---|---|
| `LINEAR_API_TOKEN` | yes (the tool's variable; the workflow sets it per step) | none | none | none |
| `DESTINATION_TOKEN` | none | none | secret | none |
| `DESTINATION_READONLY_TOKEN` | none | none | none | secret |
| `NOTION_API_TOKEN` | yes (`ingest`) | none | none | none |
| `REGISTRY_SYNC_APP_ID` | none | secret | none | none |
| `REGISTRY_SYNC_APP_PRIVATE_KEY` | none | secret | none | none |
