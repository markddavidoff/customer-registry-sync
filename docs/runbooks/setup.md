# Set up the registry sync

This guide takes a repository from nothing to a working registry sync: the Linear
tokens, the GitHub App, an SSH key for the private git dependencies, the GitHub
environments, the caller workflow, branch protection, and a staged first rollout. Day-to-day
operation, failure recovery, retiring values and restoring snapshots are in
[`operations.md`](operations.md). How the system works is in
[`../customer-registry.md`](../customer-registry.md).

## How the pieces fit

Your repository (the "caller") holds the registry: a `registry-sync.yaml` config, the
data directory, and a small caller workflow. The caller workflow calls the reusable
workflow `.github/workflows/registry-sync.yml` in the `customer-registry-sync`
repository (the "tool repository"). The reusable workflow installs the tool from the
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

**Visibility today.** The tool repository and `gitops-engine` are both private, so
installing the tool needs an SSH key with read access to both (Stage 3). A caller in the
same owner account as the tool repository can call the reusable workflow once Actions
access is granted to it. A caller in a different owner can call it only if the tool
repository is public; that is an owner decision still pending. No release tag exists
yet, so pin a commit sha wherever these steps say to pin a version (Stage 5).

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

## Stage 3: SSH key for the private git dependencies

The caller's lock installs two git repositories, both private today: the tool repository
and its engine, [`gitops-engine`](https://github.com/markddavidoff/gitops-engine). The
workflow's jobs read them over SSH with the key in the `REGISTRY_SYNC_SSH_KEY` secret.

- The key must have read access to both repositories. A deploy key is tied to one
  repository and cannot cover both, so use a machine user that is a collaborator on
  both, and put its private key in the secret.
- Use `ssh://git@github.com/...` URLs in the caller's `pyproject.toml` (Stage 5). An
  `https://` git install of a private repository fails in the job, because no
  credentials are set up for it.
- If the lock has an SSH git dependency and the secret is empty, each job fails and
  names `REGISTRY_SYNC_SSH_KEY`. If both repositories are public, skip this stage and
  use `https://` URLs; the secret is then not needed.

## Stage 4: GitHub environments and secrets

The two environment names are your choice. You write them into `registry-sync.yaml`
under `workflow:` in Stage 5. Only the deploy environment needs a required reviewer.

| Environment | Holds | Protection |
|---|---|---|
| deploy | `LINEAR_API_TOKEN` (write key) | required reviewer |
| read-only | `LINEAR_READONLY_TOKEN` (read-only key) | none |

The caller passes five secrets by name, and the reusable workflow declares exactly
these under `on.workflow_call.secrets`:

| Secret | Holds | Used by |
|---|---|---|
| `LINEAR_API_TOKEN` | Linear key with write scope | `check`, `deploy`, `restore`, `e2e`, and the post-apply `sync-inbound` |
| `LINEAR_READONLY_TOKEN` | Linear key with read scope | `refresh` |
| `REGISTRY_SYNC_APP_ID` | the GitHub App's id | the state and refresh PRs |
| `REGISTRY_SYNC_APP_PRIVATE_KEY` | the App's private key (the `.pem` text) | the state and refresh PRs |
| `REGISTRY_SYNC_SSH_KEY` | the SSH private key from Stage 3 | installing the git dependencies |

Rules for where they live:

- Store the App id as a **secret**, not a variable. The reusable workflow reads it from
  `secrets`, and a repository or environment variable does not reach it.
- If a secret with the same name exists both in the caller's passed values and in the
  environment a job runs in, the job uses the environment's value. The deploy job runs in
  the deploy environment and the refresh job in the read-only environment, so each job
  gets its Linear token from its own environment. Store the two Linear tokens only as
  environment secrets, which keeps them behind the approval gate. The caller still lists
  `LINEAR_API_TOKEN` and `LINEAR_READONLY_TOKEN` in its `secrets:` block; those two
  references resolve to nothing and the environment values replace them.
- `secrets: inherit` passes nothing to a workflow owned by another account, so the
  caller maps each name explicitly (the example does).
- The workflow hands the Linear tokens only to the steps that call Linear.

Create the environments and set the values. None of these commands print a value:

```
R=<owner>/<registry repo>
gh api -X PUT repos/$R/environments/<read-only environment>
echo '{"reviewers":[{"type":"User","id":<your GitHub user id>}]}' \
  | gh api -X PUT repos/$R/environments/<deploy environment> --input -

gh secret set LINEAR_READONLY_TOKEN -R $R --env <read-only environment>   # paste the read-only key
gh secret set LINEAR_API_TOKEN -R $R --env <deploy environment>           # paste the write key

printf '%s' "<App ID>" | gh secret set REGISTRY_SYNC_APP_ID -R $R
gh secret set REGISTRY_SYNC_APP_PRIVATE_KEY -R $R < <path to the .pem>
gh secret set REGISTRY_SYNC_SSH_KEY -R $R < <path to the SSH private key>
```

The caller passes the repository secrets `REGISTRY_SYNC_APP_ID`,
`REGISTRY_SYNC_APP_PRIVATE_KEY` and `REGISTRY_SYNC_SSH_KEY` through. Check the names:

```
gh secret list -R $R; gh secret list -R $R --env <deploy environment>; gh secret list -R $R --env <read-only environment>
```

Delete the `.pem` once the secret holds it.

## Stage 5: The caller repository

The caller needs five things in the repository that holds the registry.

**A uv project that depends on the tool, with a committed `uv.lock`.** The reusable
workflow runs `uv sync --locked` in the directory named by its `project_dir` input
(default: the repository root), so the lock pins the tool version. uv reads
`[tool.uv.sources]` only from the root project, so declare the engine's source here
too, at the tag the tool's own `pyproject.toml` pins:

```toml
[project]
name = "my-customer-registry"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = ["customer-registry-sync", "gitops-engine"]

[tool.uv.sources]
customer-registry-sync = { git = "ssh://git@github.com/markddavidoff/customer-registry-sync.git", rev = "<sha>" }
gitops-engine = { git = "ssh://git@github.com/markddavidoff/gitops-engine.git", tag = "v0.1.0" }
```

No release tag of the tool exists yet, so pin a commit: put the full commit sha in
`rev`. Switch to `tag = "<release tag>"` once one is published. Run `uv lock`, then
commit `pyproject.toml` and `uv.lock`. Upgrading the tool means changing the sha and
committing the new lock.

**`registry-sync.yaml`**, copied from
[`examples/registry-sync.example.yaml`](../../examples/registry-sync.example.yaml): your
Notion page ids, an optional `data_dir`, and the two environment names the workflow
binds its jobs to:

```yaml
workflow:
  deploy_environment: <deploy environment>
  readonly_environment: <read-only environment>
```

Only these two keys exist under `workflow:`. The CLI works without them; the workflow's
`plan` job reads them with `config-get` and fails naming any missing key.
`data_dir` must stay inside the repository.

**The caller workflow**, copied from
[`examples/workflows/registry-sync.yml`](../../examples/workflows/registry-sync.yml) to
`.github/workflows/registry-sync.yml`. It triggers on a push to `main`, a daily cron
(change the time if you like) and manual dispatch, and passes the five secrets by name.
Two points about `uses:`:

- The example says `@main` and carries a comment telling you to pin a sha. No release tag
  exists yet, so change it to `@<sha>` (the full commit sha, the same commit as the
  `rev` in your `pyproject.toml`), so a change to the tool repository cannot change your
  deploys. Use a tag once one is published.
- GitHub can call a reusable workflow only when the tool repository is public, or has
  the same owner as the caller and allows access from it (tool repository, Settings,
  Actions, General, Access). The tool repository is private today, so a caller in a
  different owner cannot call it until it is made public. Otherwise the call fails
  before any job starts.

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
| `LINEAR_API_TOKEN` | yes | optional | secret | none |
| `LINEAR_READONLY_TOKEN` | none | optional | none | secret |
| `NOTION_API_TOKEN` | yes (`ingest`) | none | none | none |
| `REGISTRY_SYNC_APP_ID` | none | secret | none | none |
| `REGISTRY_SYNC_APP_PRIVATE_KEY` | none | secret | none | none |
| `REGISTRY_SYNC_SSH_KEY` | none | secret | none | none |
