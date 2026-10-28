# How the registry and the Linear sync work

The registry is a version-controlled list of your customers, kept in step with Linear's
Customers. `customer-registry-sync` reads customer facts from Notion pages and from
Linear, merges them into one record per company, and writes the result to Linear
through a reviewed plan/apply cycle, in the style of Terraform. Git holds what Linear
should contain. Linear changes only when a reviewer approves an apply of a merged
change.

This document describes the system. To set it up, read
[`runbooks/setup.md`](runbooks/setup.md). To operate it or recover from a failure, read
[`runbooks/operations.md`](runbooks/operations.md).

Commands below are written as `customer-registry-sync <command>`. From a checkout, run
`uv run customer-registry-sync <command>`. Every command reads the config file
(`registry-sync.yaml` in the working directory, or `--config <path>` before the command).
`<data_dir>` is the config's `data_dir` (default `customers`, relative to the config
file).

## Terms

| Term | Meaning |
|---|---|
| **Dataset** | A directory of customer records under `<data_dir>/datasets/<name>/`, one YAML file per company. There are two: `notion` and `linear-found`. List both in `<data_dir>/datasets.yaml` once you run the refresh; `check` fails on a dataset directory the file does not name. |
| **Stub** | A record in the `linear-found` dataset: a Linear customer that Notion does not know. Linear owns its fields. |
| **Identity** | One entry in `<data_dir>/identities.yaml`: a company's canonical id plus every name, domain and source id it is known by. |
| **Canonical id** | The key of an identity. The sync writes it into Linear as the customer's external id, and it never changes. |
| **Desired rows** | `<data_dir>/linear/*.yaml`: what Linear should contain, written by `generate`. |
| **State mirror** | `<data_dir>/linear-state/*.yaml`: a copy of live Linear as last read by `sync-inbound`, with a `linear_synced` block (Linear id, `updatedAt`, content hash) per row. |
| **Plan** | The field-level difference between the desired rows and live Linear: creates, updates, aborts, unmatched live customers and unchanged rows. |
| **Orphan** | A managed Linear customer that is in no dataset: it resolves to no identity, or to one that no dataset record resolves to. `check` fails on it. |
| **Apply** | `deploy --apply`: writes the plan to Linear. |
| **Managed customer** | A live Linear customer that `linear-ignore.yaml` does not exclude. |

## Data flow

Two sources feed the registry. `ingest` turns each into a dataset, the identity table
ties the datasets to companies, and `generate` merges them into the desired rows.
`deploy` compares the desired rows with live Linear and writes the difference.
`sync-inbound` copies Linear back into the state mirror.

```
Notion (bucket pages)                      Linear Customers
        |                                     |        ^
     ingest                             sync-inbound   |
        v                                     v        |
datasets/notion/                linear-state/  (state mirror)
        |                                     |        |
        |              ingest --only linear-found      |
        |                                     v        |
        |                       datasets/linear-found/  (stubs)
        |                                     |        |
        +---- identities.yaml (resolve) ------+        |
                          |                            |
                       generate                        |
                          v                            |
     linear/  (desired)  +  registry.csv, real-customers.csv
                          |                            |
              check / deploy: plan = desired vs live   |
                          |                            |
              deploy --apply (approved) ---------------+
```

**Notion to `datasets/notion/`.** This step is manual: you run `ingest` on your machine with `NOTION_API_TOKEN`, run `generate`, and open a PR. The GitHub workflow never reads Notion. `ingest` walks the six bucket pages (Customers,
Churned Customers, Archive, POCs, Prospects, Partner & Supporting Accounts) through the
Notion API. It parses each customer page's `Label: value` bullets and the Partner &
Supporting Accounts bucket's table rows. The parse is deterministic, with no LLM; the
exact layout it reads is in [`notion-template.md`](notion-template.md). Each page gets a
bucket classification: Customers is `real`, Churned Customers is `churned`, POCs and
Prospects are `prospect`, Partner & Supporting Accounts is `partner`, and Archive is
`internal`. `ingest` then merges the pages and the curated `self-managed-customers.yaml`
entries into one record per company. Every record contributes its domains and its
normalised name as keys, and records that share any key fall into one group,
transitively. A group takes the highest classification among its pages, ranked
real > partner > churned > prospect > internal, and a curated self-managed deployment
wins over a page's. `classification-overrides.yaml` is applied last and wins over the
bucket. Unknown values stay empty; the parser never guesses one.

**Linear to `linear-state/`.** `sync-inbound` reads every Linear customer and writes one
mirror row per customer, copied field for field from Linear.

**`linear-state/` to `datasets/linear-found/`.** `ingest --only linear-found` reads only
the mirror, so it needs no Notion or Linear access. It resolves each managed Linear
customer to an identity and writes a stub for each identity that has no `notion`
record. A stub takes its classification from the Linear status through
`linear_status_classification` in `<data_dir>/datasets.yaml`.

**Datasets to `linear/`.** `generate` resolves every record to an identity and keeps one
record per identity: the one from the dataset listed first in `<data_dir>/datasets.yaml`
(by default `notion`, then `linear-found`). The winner is used whole; `generate` never
mixes fields from two records. The winner gains any extra domains on its identity.
`generate` then projects the winner onto Linear's schema:

- classification `real` and `partner` become status `Active`, `prospect` becomes
  `Prospect`, `churned` becomes `Churned`; `internal` and unclassified records get no
  Linear row
- ARR becomes `revenue`, rounded to whole dollars; `tier` passes through
- the canonical id becomes the only external id
- a `notion` row's name carries its status, as in `Example Corp [Active]`, because
  Linear's customer search does not show status; a stub's name is its Linear name with
  any ` [Active]`-style suffix stripped, carries no suffix, and is marked
  `linear_owned: true` (the planner never writes a stub's name)

`generate` also writes `registry.csv` (every merged record, with a `dataset` column) and
`real-customers.csv` (records classified `real` or `partner`).

## Identity resolution

`identities.yaml` decides which records and Linear customers are the same company. One
function, `resolve`, answers that question everywhere: in `generate`, in the planner, in
`ingest` and in `check`.

```
item (dataset record, desired row, or live Linear customer)
  |
  1. source id  : an external id or stub file name equal to a canonical id,
  |               or the notion slug / Linear id in an identity's sources
  2. domain     : any domain on an identity, compared case-insensitively
  3. name       : any name on an identity, compared after stripping a
  |               " [Active]"-style suffix, lowercasing, and dropping
  |               everything but letters and digits
  v
first key that hits decides  -->  canonical id
no hit                       -->  ingest appends a new identity
later key hits another one   -->  conflict: check fails
```

An identity looks like this (`sources` accepts only the keys `notion` and `linear`):

```yaml
acme-robotics:
  names: [Acme Robotics]
  domains: [acme-robotics.example.test]
  sources:
    notion: acme-robotics
    linear: 00000000-0000-4000-8000-000000000001
```

The canonical id stays stable through these rules:

- `ingest` is the only program that writes `identities.yaml`, and it only appends. It
  adds an identity for a record or managed Linear customer that resolves to none, and
  adds missing names, domains and source ids to an existing identity. It never changes
  or removes a value, never adds a name or domain that another identity holds (it
  reports that instead), and keeps comments and key order.
- A new identity's id is the first label of the record's first domain
  (`acme-robotics.example.test` gives `acme-robotics`), else a slug of its name, with
  `-2`, `-3` appended when the id is taken.
- A stub keeps its file name, which is its canonical id, across refreshes: `ingest`
  looks the stub up by its Linear id, so renaming the customer or changing its domain
  in Linear does not change the id stamped into Linear.
- When a `notion` record starts to match a stub's identity, the stub drops out on the
  next `ingest` and the `notion` record takes over the same canonical id. Linear sees
  no external-id change.
- A human merges two identities by moving the duplicate's names, domains and source ids
  into the survivor and deleting the duplicate's key. A Linear customer stamped with
  the deleted id resolves to the survivor through its recorded Linear id, and the next
  apply restamps it.

`check` fails when a domain, name or source id sits on two identities, or when a record
or Linear customer resolves to two identities. It reports, without failing, an identity
that nothing resolves to.

## Planning and applying

The planner (`plan_deploy`) pairs each desired row with a live Linear customer by
identity, then decides each field by its owner.

- A desired row with no live customer is a **create**. A stub with no live customer is
  reported and skipped.
- A live customer with no desired row is an **unmatched live customer**. The plan lists
  it (`ORPHAN (report-only, not deleted)` in the output) and the plan alone does not
  fail on it; the sync never deletes it. `check` separately fails on an **orphan**, a
  managed customer in no dataset (see Terms). A customer whose only record has no
  desired row, such as an `internal` record, is unmatched but not an orphan.
- Two or more live customers on one identity: the planner writes the one carrying the
  canonical id, else the one whose Linear id the identity records. The others are
  **known duplicates**, never written. When neither id picks one, or two carry the
  canonical id, the planner reports an error and writes none of them.
- A desired domain that another live customer already holds is **withheld**, since a
  shared domain is unverified.

Field ownership:

| Field | `notion` row | Stub (`linear_owned`) |
|---|---|---|
| `name`, `status` | Registry-owned: always written | Linear-owned: never written |
| `external_ids` | Registry-owned: set to the canonical id | Set to the canonical id (the only write) |
| `domains` | Registry list unioned with live; a Linear-only domain stays | Linear-owned |
| `revenue`, `tier` | Registry-owned when Notion has a value; otherwise the live value stays | Linear-owned |

A live value the registry does not carry (a Linear-only domain, revenue or tier) is
**untracked**: `check` prints it as an `UNTRACKED:` line and passes. The next refresh
appends an untracked domain to the identity. Notion is the only place to maintain
revenue and tier.

`deploy --apply` runs the plan in this order:

1. Refuse before any write on a planner error, an ignore conflict, or a destructive
   change with no retire entry.
2. Resolve every status and tier the plan writes to a Linear id; refuse if one is
   missing.
3. Write a snapshot of every live row it will update to
   `<data_dir>/linear-snapshots/<UTC>.yaml`.
4. Create the new customers, sending only non-empty fields.
5. For each update: re-read the customer, abort the row if it is gone, newly ignored,
   or its `updatedAt` differs from the plan-time read; send only the planned fields;
   record the returned `updatedAt` in the snapshot; compare the returned row with the
   target on every managed field.
6. Stop at the first Linear error or verification mismatch. The remaining rows are
   aborted as "not attempted".
7. Merge the written rows into the state mirror. The workflow then runs a full
   `sync-inbound`.

This tool takes drift detection and the exit codes from the
[`gitops-engine`](https://github.com/markddavidoff/gitops-engine) library. The
field-level planning and the apply run in this repository.

## Safety model

Each layer stops a different kind of mistake before it reaches Linear, or makes it
reversible after.

```
edit in git
  |  manifests        generated files must match their recorded hashes
  |  offline checks   identities, datasets.yaml, ignore file, generate output
  v
pull request: check (recommended; see "Workflows")
  |  drift            state mirror must equal live Linear (content hash)
  |  orphans          every managed Linear customer must be in a dataset
  |  retire           every removal must be named in linear-retire.yaml
  v
approval in the deploy environment
  v
apply
  |  snapshot         before the first write
  |  check-and-set    re-read each row; abort if it changed
  |  never-null       the client refuses to send an empty value
  |                   unless the change is a retire
  |  verification     compare each returned row with its target
  v
restore             write a snapshot back, skipping rows edited since
```

- **Field ownership.** The apply never removes or clears a live domain, revenue or tier
  on its own. It adds domains, and sets revenue and tier only when Notion has a value.
  The one field it replaces wholesale is `external_ids`, on every managed customer,
  stubs included: it sets the list to the canonical id alone.
- **The retire file is the only removal path.** `<data_dir>/linear-retire.yaml` names,
  per canonical id, the live values the next apply removes: domains, a revenue or a
  tier, each with its exact current value. The planner emits a removal only from an
  entry there and marks it as a retire. `check` fails on an entry that cannot do its
  job: the value is not live, the id has no desired row or live match, the row is a
  stub, or the registry still carries the value. After the apply, someone deletes the
  entry on the state PR branch.
- **Drift and check-and-set.** `check` compares a hash of each mirror row's deployable
  fields with live Linear, not `updatedAt`, which Linear bumps for edits to fields the
  sync ignores. At plan time a row aborts when both its `updatedAt` and its content
  changed since the mirror. At write time a row aborts when its `updatedAt` changed
  since the plan read it. Linear has no server-side compare-and-swap, so the re-read
  and the write are separate requests.
- **Snapshots and `restore`.** Every apply writes a snapshot before its first write.
  `restore --from <snapshot>` writes the snapshot values back. It skips and reports a
  row whose current `updatedAt` is not the one the apply left (or, for an unwritten
  row, the one the snapshot saw), so it never overwrites newer work.
- **Manifests.** `ingest`, `generate` and `sync-inbound` write a `.manifest.yaml` of
  file hashes beside their output. A generator prompts before replacing a hand-edited
  file and refuses with `--no-input` or without a terminal. `check` fails on a hand
  edit, a missing manifest, or desired rows and CSVs that differ from what `generate`
  produces now.
- **The never-null client guard.** `LinearClient` refuses to send a null or empty list
  unless the caller passes `retire=True`, and `create_customer` takes no `retire`. A bug
  in the planner cannot erase a Linear value by sending an empty field.
- **Production guards.** `deploy --apply` and `restore` against the configured
  `data_dir` need `--prod`. Outside GitHub Actions, `--prod` also needs `--break-glass`,
  which prints a warning.

## A change from the contributor's side

A contributor changes a curated file, or runs `ingest` after a Notion edit, then runs
`generate` and opens a PR with the result.

```
contributor          GitHub                                 Linear
    |
    | edit curated file or run ingest; generate; open PR
    +-------------> PR check: check  --------------read---> live
    |               red on drift, orphans, conflicts, ...
    | merge
    +-------------> push to main
    |               registry-sync: changed? yes; deploy waits for approval
reviewer approves
    +-------------> check, then deploy --apply --prod ------> snapshot,
    |                                                      check-and-set,
    |                                                      write, verify
    |               sync-inbound  <-----------------read---- live
    |               state PR: linear-state/<run>-<attempt>
    |               (mirror + snapshot, opened by the GitHub App)
    | merge state PR
    +-------------> main (state paths do not start a deploy)
```

The state PR touches only `<data_dir>/linear-state/` and `<data_dir>/linear-snapshots/`,
which are not deploy inputs. A retire apply is the exception: the reviewer deletes the
applied retire entry on the state PR branch, so merging it starts a deploy whose plan is
a no-op.

## A change from Linear's side

People also create and edit customers in Linear directly. The sync treats those edits as
follows.

```
someone creates customer X in Linear
  -> next check on any PR: DRIFT added + orphan X  (red)
  -> refresh (daily, or dispatched):
       sync-inbound              X enters linear-state/
       ingest --only linear-found
         X ignored?              nothing happens
         X matches an identity   Linear id and new domains appended to it
           with a notion record    no stub; the notion row owns X
         X matches another       known duplicate; never written
           written customer
         otherwise               new identity + stub datasets/linear-found/<id>.yaml
       generate                  linear/<id>.yaml (linear_owned for a stub)
       open refresh PR           linear-refresh/<run>-<attempt>
  -> merge refresh PR -> deploy (approved) stamps X's external id
```

- **An edit to a stub** (name, status, domains, revenue, tier) is Linear's to make.
  `check` does not count it as drift, the apply never reverts it, and the next refresh
  copies it into the stub.
- **An edit to a `notion` customer** shows as `DRIFT changed` until a refresh PR records
  it in the mirror. After that, the next apply writes registry-owned fields back to the
  Notion values. A Linear-only domain stays and is appended to the identity; a revenue
  or tier the registry lacks stays and is reported as untracked. To keep any other
  edit, make it in Notion.
- **A customer deleted in Linear** shows as `DRIFT deleted`. After a refresh, its stub
  disappears; a `notion` customer becomes a create in the next plan.
- **A customer with an unmapped status** (one missing from
  `linear_status_classification`) gets a stub with no classification and no desired
  row, and `check` fails until someone maps the status or ignores the customer.
- **An ignored customer** (`<data_dir>/linear-ignore.yaml`, by Linear id or name prefix)
  is never stubbed, never given an identity, never an orphan and never written. `check`
  lists each ignored customer with its reason, and fails when a desired record resolves
  to the same identity as an ignored customer. The exception is the `linear-found` stub
  of that same Linear id: `check` only reports it, and the next refresh drops it. The
  prefix `zzz-gitops-test-` is built in (`BUILTIN_PREFIX` in `gitops_engine.ignore`). The live e2e
  tests (`tests/test_e2e_gitops.py`) create their throwaway customers in a real Linear
  workspace under names that start with it, so the sync ignores any customer with that
  prefix and never stubs, writes or reports it as an orphan. The built-in entry is
  always present, so you do not list it, and `check` shows it as an `IGNORED PREFIX`
  line. A run whose `--scope` starts with the prefix lifts the exemption for itself,
  which is how the e2e tests write their own records.

## Files in `<data_dir>`

| Path | Kind | Written by | Content |
|---|---|---|---|
| `datasets/notion/<slug>.yaml` | Generated | `ingest` | One record per Notion company (the slug is also its `notion` source id): domains, buckets, classification, deployment, ARR, tier, status, contact, `hubspot_id`, sources, notes. Every field is listed in [`notion-template.md`](notion-template.md#what-ingest-writes-the-dataset-record). |
| `datasets/linear-found/<canonical id>.yaml` | Generated | `ingest` | One stub per Linear-only customer, with `linear_customer_id` and a `linear:<id>` source. |
| `linear/<canonical id>.yaml` | Generated | `generate` | Desired rows: name, domains, revenue, tier, status, external ids, and `linear_owned` on a stub. No sync state. |
| `linear-state/<canonical id>.yaml` | Generated | `sync-inbound`, and `deploy --apply` for the rows it wrote | State mirror: each live customer plus `linear_synced`. |
| `registry.csv`, `real-customers.csv` | Generated | `generate` | Merged records as CSV; the second holds only `real` and `partner`. |
| `*/.manifest.yaml` | Generated | the directory's generator | Generator name, time, and a sha256 per file. |
| `linear-snapshots/<UTC>.yaml` | Written by the apply | `deploy --apply` | Pre-apply copy of each updated row, with the post-write `updatedAt`. Lands on `main` through the state PR. |
| `datasets.yaml` | Curated | hand | Dataset priority order and the Linear status to classification table. |
| `identities.yaml` | Curated | `ingest` appends; hand edits | The identity table. |
| `linear-ignore.yaml` | Curated | `ignore` appends; hand edits | Linear customers the sync never touches, each with a reason. |
| `linear-retire.yaml` | Curated | hand | Values the next apply removes from Linear. |
| `self-managed-customers.yaml` | Curated | hand | On-prem and hybrid deployments, merged by `ingest`. |
| `classification-overrides.yaml` | Curated | hand | Classification overrides by domain or name, applied by `ingest`. |

A file name in `linear/` and `linear-state/` is its first external id, else a slug of
its name. Generated files change only by re-running their command; to change one,
change the curated input or the source behind it. [`examples/`](../examples/) has a
sample of each curated file.

## Commands

| Command | What it does | Linear | Exit codes |
|---|---|---|---|
| `ingest [--only notion\|linear-found]` | Builds `datasets/notion/` from Notion (needs `NOTION_API_TOKEN`), then `datasets/linear-found/` from the mirror; appends to `identities.yaml`. `--only linear-found` needs a state mirror from an earlier `sync-inbound` and exits 2 without one; a plain `ingest` skips the `linear-found` step and says so. | none | 0 written or dry run; 2 refused or error |
| `generate` | Merges the datasets into `linear/` and both CSVs. | none | 0; 2 refused (hand edits) |
| `sync-inbound [--scope P]` | Rewrites `linear-state/` from live Linear. | read | 0; 2 no token or refused; 1 with a traceback on a Linear API error |
| `check [--scope P]` | Drift, plan and every offline check; prints the plan. Needs `LINEAR_API_TOKEN`. | read | 0 in sync; 2 no token, invalid curated file, or a Linear read error; 3 something to fix |
| `deploy [--apply --prod [--break-glass]] [--scope P]` | Prints the plan; with `--apply`, writes it. Needs `LINEAR_API_TOKEN` even as a dry run. | read; write with `--apply` | 0 done; 2 no token, invalid curated file, guard refused, or apply refused before any write; 3 refused plan, aborted rows or a failed create/update; 1 with a traceback on a Linear API error while reading customers or the status and tier maps |
| `restore --from <snapshot> [--rows ID...] --prod [--break-glass]` | Writes snapshot values back with check-and-set. | write | 0 all restored; 2 no token, guard refused, unreadable snapshot, or a write error that stopped it; 3 some rows skipped; 1 with a traceback on a Linear API error while reading the status and tier maps |
| `ignore --linear-id ID --reason TEXT` | Appends a mirrored Linear customer to `linear-ignore.yaml`. The id must be in the state mirror, so run `sync-inbound` first. | none | 0 appended; 4 already listed; 2 refused |
| `changed --since SHA` | Prints `changed` or `unchanged`: did a deploy input change between SHA and HEAD? Any doubt (unknown sha, first push, force-push, git failure) prints `changed`. Run it inside the git repository. | none | 0 always |
| `config-get KEY` | Prints one config value by dotted key, such as `data_dir` (the absolute path, a relative `data_dir` resolved against the config file's directory) or `workflow.deploy_environment`. | none | 0; 2 unknown or missing key, named on stderr |
| `tool-source` | Prints where the installed tool came from: `git <commit> <url>` or `dir - <path>`. | none | 0; 2 when the install source cannot be recovered |

Every command takes the global `--config <path>`. The commands that read or write
registry files take `--out-dir` (default: the config's `data_dir`). `--break-glass`
lets `deploy --apply --prod` and `restore --prod` run outside GitHub Actions, with a
warning. `--dry-run` on `ingest`, `generate` and `sync-inbound` prints what would
change. `--no-input` makes a command refuse instead of prompting. `--scope P` limits a
run to rows whose name starts with `P`; the live e2e tests use it.

`changed` treats these paths as deploy inputs: `<data_dir>/linear/**`, `identities.yaml`,
`linear-ignore.yaml`, `datasets.yaml` and `linear-retire.yaml` under `<data_dir>`, and the
config file itself. The state mirror and the snapshots are not deploy inputs.

`check` fails (exit 3) on: drift, plan-time aborts, planner errors, destructive changes
with no retire entry, stale retire entries, ignore conflicts, a live customer on two
identities, orphans, and the offline errors (hand-edited or out-of-date generated files,
identity conflicts, an invalid `datasets.yaml` or `linear-ignore.yaml`, unmapped stub
statuses). It reports without failing: untracked values, known duplicates, withheld
domains, unused identities, records with no identity, stale ignore entries and the
ignored customers. A passing run ends with `IN SYNC`.

## Workflows

The repository ships one reusable GitHub Actions workflow,
[`.github/workflows/registry-sync.yml`](../.github/workflows/registry-sync.yml), and a
short caller to copy into the repository that holds the registry,
[`examples/workflows/registry-sync.yml`](../examples/workflows/registry-sync.yml). The
[setup runbook](runbooks/setup.md) covers adoption.

The caller triggers it on a push to `main`, a daily schedule and manual dispatch. The
workflow reads the data directory and both environment names from the registry config
(`workflow.deploy_environment` and `workflow.readonly_environment`), so the caller has no
path filter. Its jobs:

| Job | Runs when | Environment | Steps |
|---|---|---|---|
| `plan` | always | none | Installs the tool, then decides the mode. On a push it runs `changed --since <before>` and ends the run when no deploy input changed. Reads the two environment names with `config-get`. |
| `deploy` | a push that changed a deploy input, or a dispatch with `mode` `deploy`, `restore` or `e2e` | `workflow.deploy_environment` (write token; the required reviewer is environment configuration) | `deploy`: `check`, then `deploy --apply --prod --no-input`. `restore`: `restore --from <snapshot> [--rows]`. Both then upload the snapshots, run `sync-inbound`, summarise the run, and open a state PR. `e2e`: runs the live GitOps test from the installed tool's own source and opens no state PR. |
| `refresh` | the daily schedule, or a dispatch with `mode: refresh` | `workflow.readonly_environment` (no approval, read-only token) | `sync-inbound`, `ingest --only linear-found`, `generate`, then a refresh PR. It never runs `check` and never writes to Linear. |

- The `deploy` and `refresh` jobs use separate concurrency groups, so a daily refresh
  never cancels a deploy that waits for approval. Each group name ends with the
  workflow's `config` input, so two registries in one repository (two config files)
  do not share a group.
- The refresh creates `<data_dir>/datasets/linear-found/`. List `linear-found` in
  `<data_dir>/datasets.yaml` before the first refresh (copy
  [`examples/datasets.example.yaml`](../examples/datasets.example.yaml)), or `check`
  fails and every deploy blocks.
- The workflow never reads Notion. Notion ingest stays manual: run `ingest` locally,
  run `generate`, and open a PR.
- This repository and the engine are private today. A caller in a different owner can
  call the reusable workflow only if this repository is public (an owner decision
  still pending), and no release tag exists, so pin a commit sha in the caller's
  `uses:` line and in the `uv` source.
- A failing `check` in the `deploy` job stops the job before the apply, so no state PR
  opens; a refresh PR is the way out.
- A failed apply still uploads its snapshot and opens the state PR, then marks the run
  failed.
- A GitHub App opens the state and refresh PRs, because a PR opened with the workflow
  token runs no checks. Neither job opens a PR when nothing changed.
- The workflow does not run `check` on pull requests. The repository ships no PR-check
  workflow yet (see [`ROADMAP.md`](../ROADMAP.md)). To get one, add a workflow of your
  own that runs `check` on pull requests and in the merge queue with a read-only
  `LINEAR_API_TOKEN`, and make it a required check.

## Known limits

- The sync owns `externalIds` on every managed customer, stubs included: a write
  replaces any external id another tool set.
- The client retries HTTP 429 and honours `Retry-After`, but it does not throttle a
  burst of creates, and Linear's live 429 behaviour has not been observed.
- Check-and-set runs on the client: a Linear edit that lands between the write-time
  re-read and the write is overwritten.
- `restore` writes back every snapshot row whose `updatedAt` is unchanged, including
  rows the apply never wrote.
- A Linear API error in `sync-inbound`, `deploy` or `restore` exits 1 with a traceback
  instead of 2. Only `check` catches it and exits 2.
- `linear-retire.yaml` is read without validation: a malformed entry yields no error
  that names it.
- After a Linear customer is renamed into a `--scope` prefix, the mirror holds two rows
  for its Linear id until an unscoped `sync-inbound`.
- The refresh job does not close older refresh PRs. Re-running an old refresh run
  replays its original commit and produces a PR that conflicts with `main`.
- The sync never deletes a Linear customer. An unmatched live customer or orphan stays
  in Linear until a person removes or merges it there.
- `ingest` reads only pages that exist in Notion. A customer that has no Notion page is
  missing from the `notion` dataset until it has one.

## Code map

| Module | Role |
|---|---|
| `notion_ingest.py`, `assemble.py`, `inputs.py`, `config.py` | Notion extraction, cross-bucket merge, curated self-managed and override inputs, the config file. |
| `datasets.py`, `schema.py` | `datasets.yaml`, dataset directories, `CustomerRecord`. |
| `identity.py` | The customer's identity declarations: `IDENTITY_SPEC` (source, domain and name keys, their order and normalisers), the `keys_for_*` reducers and `implicit_identity`. The table, `resolve`, validation and the comment-preserving store are in `gitops-engine`. |
| `linear_found.py` | Stub building and unmapped-status errors. |
| `generate.py`, `writers.py` | Merge by identity, projection to Linear rows, YAML and CSV writers. |
| `linear_schema.py`, `linear_client.py`, `linear_adapter.py` | `LINEAR_SCHEMA` (the field policies the engine's planner runs), the mirror row, `content_hash_of`, the GraphQL client, the engine adapter. |
| `deploy.py`, `linear_files.py` | Plan and apply on the engine's pairing and field planner; the ignore and retire file names. |
| `check.py`, `manifest.py`, `snapshot.py` | `check`, manifests, snapshots and `restore`. |
| `changed.py`, `tool_source.py` | `changed` and `tool-source`, which the reusable workflow calls. |
| `cli.py` | The commands above. |

Linear API details the client depends on are in
[`linear-api-notes.md`](linear-api-notes.md).
