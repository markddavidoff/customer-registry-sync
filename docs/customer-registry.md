# How the registry and the Linear sync work

The registry is a version-controlled list of your customers, kept in step with Linear's
Customers. `customer-registry-sync` reads customer facts from Notion pages and from
Linear, merges them into one record per company, and writes the result to Linear
through a reviewed plan/apply cycle, in the style of Terraform. Git holds what Linear
should contain, for every field the sync writes. Linear changes only when a reviewer
approves an apply of a merged change. When someone edits one of those fields in Linear,
the sync records the edit in git, and a pull request decides whether it stays.

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
| **Overrides file** | `<data_dir>/linear-overrides.yaml`: every decision that differs from upstream, keyed by canonical id. `generate` applies it last; `sync-inbound` and `hide` write to it. See [Ownership and overrides](#ownership-and-overrides). |
| **Pin** | An override value for `name`, `status`, `revenue` or `tier` that wins over upstream until someone deletes it. |
| **Pending entry** | A Linear edit that `sync-inbound` recorded and that waits for a decision. `check` fails and `deploy` aborts the customer's row until it is resolved. |
| **Hidden** | A customer taken out of the sync on both sides, by canonical id or by name prefix. |
| **Deleted** | A customer confirmed deleted in Linear. The sync does not recreate it. |
| **State mirror** | `<data_dir>/linear-state/*.yaml`: a copy of live Linear as last read by `sync-inbound`, with a `linear_synced` block (Linear id, `updatedAt`, content hash) per row. |
| **Plan** | The field-level difference between the desired rows and live Linear: creates, updates, aborts, unmatched live customers and unchanged rows. |
| **Orphan** | A managed Linear customer that is in no dataset: it resolves to no identity, or to one that no dataset record resolves to. `check` fails on it. |
| **Apply** | `deploy --apply`: writes the plan to Linear. |
| **Managed customer** | A live Linear customer that `linear-overrides.yaml` does not hide. |

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
mirror row per customer, copied field for field from Linear. Before it replaces the
mirror, it compares the old mirror with live Linear and records each edit and each
confirmed deletion in `linear-overrides.yaml` (see [Ownership and
overrides](#ownership-and-overrides)).

**`linear-state/` to `datasets/linear-found/`.** `ingest --only linear-found` reads only
the mirror, so it needs no Notion or Linear access. It resolves each managed Linear
customer to an identity and writes a stub for each identity that has no `notion`
record. A hidden customer gets no stub. A stub takes its classification from the Linear status through
`linear_status_classification` in `<data_dir>/datasets.yaml`.

**Datasets to `linear/`.** `generate` resolves every record to an identity and keeps one
record per identity: the one from the dataset listed first in `<data_dir>/datasets.yaml`
(by default `notion`, then `linear-found`). The winner is used whole; `generate` never
mixes fields from two records. The winner gains any extra domains on its identity.
`generate` then projects the winner onto Linear's schema, and applies `linear-overrides.yaml` last, so `linear/` holds the final desired rows:

- classification `real` and `partner` become status `Active`, `prospect` becomes
  `Prospect`, `churned` becomes `Churned`; `internal` and unclassified records get no
  Linear row
- ARR becomes `revenue`, rounded to whole dollars; `tier` passes through
- the canonical id becomes the only external id
- a `notion` row's name carries its status, as in `Example Corp [Active]`, because
  Linear's customer search does not show status; a stub's name is its Linear name with
  any ` [Active]`-style suffix stripped, carries no suffix, and is marked
  `linear_owned: true` (the planner never writes a stub's name)
- a hidden or deleted customer gets no row

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

## Ownership and overrides

The registry owns every field the sync writes: `name`, `status`, `external_ids`,
`domains`, `revenue` and `tier`. The registry is the generated desired row
(`linear/<id>.yaml`) after the overrides file is applied. An empty `revenue` or `tier`
clears Linear's value, and a Linear domain the registry does not list is removed. A
domain another live customer holds is still withheld, and a required field (`status`,
`external_ids`) is never written empty. The one exception is the Linear-owned stub,
whose upstream is Linear: the refresh copies Linear's edits into the stub, and overrides
do not apply to it except `hidden`.

### The overrides file

`<data_dir>/linear-overrides.yaml` holds every decision that differs from upstream,
keyed by canonical id. [`examples/linear-overrides.example.yaml`](../examples/linear-overrides.example.yaml)
shows one entry of each kind.

| Entry key | Meaning |
|---|---|
| `pin` | A value for `name`, `status`, `revenue` or `tier` that wins over upstream. `null` pins an empty value, for `revenue` and `tier` only. A pin holds until someone deletes it, whatever upstream does later, and does not record the upstream value it replaced. A pinned `name` is the base name: the ` [Status]` suffix is still added. `external_ids` can never be pinned. `revenue` must be an integer or `null`, `tier` a string or `null`. |
| `domains: {add, remove}` | Domains added to, or removed from, the upstream set. A removal applies after the identity's extra domains are merged in, so the identity keeps the domain for matching while the desired row drops it. A `remove` must name a domain upstream carries. |
| `pending` | A Linear edit waiting for a decision: the field's name for `name`, `status`, `revenue` or `tier`, and `domains_add` for domains added in Linear. Written by `sync-inbound`. |
| `hidden: {reason}` | The customer is out of the sync on both sides. It gets no desired row, and its live customer is outside the managed set for `check`, `deploy` and stub building. The reason is required. |
| `deleted: {linear_id, confirmed}` | The customer was deleted in Linear. It gets no desired row, so the sync does not recreate it. Written by `sync-inbound`. |
| `reason` | Free text. |
| `hidden_name_prefixes` (top level) | Hides every Linear customer whose name starts with the prefix, at least four characters, each with a reason. The prefix `zzz-gitops-test-` is built in. |

The overrides file is a deploy input, and `check` compares `linear/` with what
`generate` produces from it.

### What happens to a Linear edit

`sync-inbound` compares each managed, non-stub customer in the old mirror with live
Linear, field by field. A field that differs was edited in Linear. `deploy` merges its
own writes into the mirror, so a deploy's write never counts as an edit, and a live
value equal to the desired value is never an edit. For each edited field whose live value
also differs from the desired value:

| Edit in Linear | What `sync-inbound` writes | Effect |
|---|---|---|
| Revenue or tier cleared, or a domain removed | An accepted override: `pin: {field: null}`, or `domains.remove` | Absorbed. `check` passes. |
| Any other change to `name`, `status`, `revenue` or `tier`, or an added domain | A `pending` entry with Linear's value | `check` fails until the entry is resolved. Because the deploy job runs `check` first, one pending entry blocks every deploy. `deploy` also aborts that customer's row, as a second guard. |
| `status` cleared, or `external_ids` changed | Nothing | The next deploy writes the registry value back. |

Names are compared and recorded without the ` [Status]` suffix. An edit that changes
only the suffix, or deletes it, is not an edit: the next deploy writes the suffix back
from `status`. A domain re-cased in Linear is not an edit, and the live spelling stays.
A domain added in Linear to a customer that has a `notion` record becomes a pending
`domains_add` entry; it is not appended to the identity. A stub's Linear edit records
nothing, because the refresh copies it into the stub.

A pending entry whose decision became moot (Linear's value now equals the registry's, or
upstream now carries the added domain) is dropped by the next `sync-inbound`, which
prints a `NOTE` for it.

### Resolving a pending entry

Resolving is a hand edit in a pull request. `check` prints both options beside every
pending entry:

```
PENDING: acme-robotics: revenue = 150000 (edited in Linear)
  keep Linear's value: move it to customers.acme-robotics.pin.revenue
  upstream wins: delete customers.acme-robotics.pending.revenue; the next deploy writes the registry value
```

- **Keep Linear's value.** Move the value from `pending` into `pin` (or, for
  `domains_add`, into `domains.add`). `generate` then writes it into the desired row.
- **Upstream wins.** Delete the `pending` value. The next deploy overwrites Linear.

Run `generate` after editing, because the overrides file changes the desired rows.

### Deleted customers

When a managed, non-stub customer in the old mirror is missing from the live listing,
`sync-inbound` confirms the deletion before it records anything. The customer must be
absent from the full listing, and a direct read by its Linear id must report it not
found. An archived customer (`Customer.archivedAt` set) counts as deleted. Only a
confirmed deletion writes a `deleted` entry. A customer the listing omits but the direct
read finds is left alone with a `NOTE`. A Linear error in either read exits 2 and writes
nothing. To bring a deleted customer back, delete its `deleted` entry; the next deploy
creates it. A stub deleted in Linear drops out of the stubs, as before. Linear's
not-found response for a hard-deleted customer is not yet verified against the live API;
[`linear-api-notes.md`](linear-api-notes.md#deleted-and-archived-customers) and the
[operations runbook](runbooks/operations.md#a-deleted-customer-and-the-unverified-not-found-shape)
say what that means today.

### A stub handing over to Notion

When a `notion` record starts to match a stub's identity, the `notion` row takes the
stub's canonical id. Any revenue, tier or domain the stub carried that the `notion` row
lacks would be cleared by the next deploy, so the run that performs the handover
(`ingest`) keeps them as pins and `domains.add` entries. The refresh PR shows them.

### Moving a v0.2.0 data repository

`adopt-live` migrates a data repository to full ownership. It runs offline against the
committed mirror and needs no token. For each managed, paired, non-stub customer, it
pins the live revenue and tier that the registry lacks and adds the live domains the
desired row lacks. It turns `linear-ignore.yaml` into `hidden` entries and
`hidden_name_prefixes` (appending an identity for an ignored customer that has none),
then deletes the ignore file and the retire file. It refuses, with exit 2 and nothing
written, when `linear-retire.yaml` still has entries (apply and remove them under v0.2.0
first), when the state mirror is missing, or when a curated file cannot be read.
`--dry-run` prints the counts and writes nothing. Until you run it, `check`, `deploy`,
`ingest`, `sync-inbound`, `refresh` and `hide` exit 2 on a data repository that still has
either old file, and plan nothing.

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
| `domains` | Registry-owned: the desired list is the target, and a live domain it lacks is removed | Linear-owned |
| `revenue`, `tier` | Registry-owned: an empty value clears Linear's | Linear-owned |

A removal or clear is always the visible result of a registry change: upstream dropped
the value, or an override pins or removes it. The plan marks each one as acknowledged, so
no separate step names it. The retire file and the `UNTRACKED:` report are gone, because
the sync tracks every value it writes. Maintain revenue and tier in Notion, or pin them
in `linear-overrides.yaml`.

`deploy --apply` runs the plan in this order:

1. Refuse before any write on a planner error, a hidden conflict, or a destructive
   change that is not acknowledged. A customer with a pending entry is not written: the
   plan aborts its row and the exit code is 3.
2. Resolve every status and tier the plan writes to a Linear id; refuse if one is
   missing.
3. Write a snapshot of every live row it will update to
   `<data_dir>/linear-snapshots/<UTC>.yaml`.
4. Create the new customers, sending only non-empty fields.
5. For each update: re-read the customer, abort the row if it is gone, newly hidden,
   or its `updatedAt` differs from the plan-time read; send only the planned fields;
   record the returned `updatedAt` in the snapshot; compare the returned row with the
   target on every managed field.
6. Stop at the first Linear error or verification mismatch. The remaining rows are
   aborted as "not attempted".
7. Merge the written rows into the state mirror. The workflow then runs a full
   `sync-inbound`.

The engine runs this plan and apply: [`gitops-engine`](https://github.com/markddavidoff/gitops-engine)
v0.3 plans by identity and field policy, applies under check-and-set with the snapshot,
verifies each write, and restores. This repository declares what is specific to
customers and Linear; see "What this tool declares on the engine" below.

## What this tool declares on the engine

| Declaration | Module | Content |
|---|---|---|
| Record schema | `linear_schema.py` | `LINEAR_SCHEMA`: the fields in plan order and their policies. `name`: always written. `status`: always written, and `required`, so a Linear-owned stub whose live status is empty falls back to the desired status, and verification of an unplanned status expects the value from before the write. `external_ids`: always written, compared unordered. `domains`: owned outright (`clears`): the registry list is the target and a live domain it lacks is removed; `exclusive`, so a domain another live customer holds is withheld. `revenue`, `tier`: owned outright (`clears`): an empty registry value clears Linear's. A Linear-owned stub takes every value from live and writes only its canonical id. |
| Nouns | `linear_schema.py` | `customer`, `customers`, `Linear`, `registry`, `updatedAt`: the words the engine prints, so output text stays the same as before the move. |
| Identity keys | `identity.py` | `IDENTITY_SPEC`: sources `notion` then `linear`, then the match keys `domains` (lower case) and `names` (case, spaces, punctuation and a trailing ` [Status]` ignored), and the key order inside each `identities.yaml` entry. |
| Destination | `linear_destination.py` | `LinearDestination`, the engine's `Destination` over `LinearClient`: it resolves statuses and tiers to Linear ids before any write and maps the field values onto GraphQL calls. |
| Overrides | `linear_overrides.py` | `OVERRIDES_FORMAT`: the file name `linear-overrides.yaml`, the `customers` key, `linear_id` as a deleted entry's id key, the pinnable fields (`name`, `status`, `revenue`, `tier`) and the list field (`domains`). `pin_customer` pins a name without its status suffix, and `value_type_failures` checks pinned value types. |
| Files | `linear_files.py`, `datasets.py`, `writers.py` | The names the engine takes as parameters: `linear-snapshots/`, `linear-state/`, `linear/`, the dataset names, `linear_status_classification` in `datasets.yaml`. `linear-ignore.yaml` and `linear-retire.yaml` are legacy names that only `adopt-live` and the refusal check still know. |
| Source adapter | `sources/` | `NotionSource`, chosen by `source.kind` in `registry-sync.yaml`. |

## Safety model

Each layer stops a different kind of mistake before it reaches Linear, or makes it
reversible after.

```
edit in git
  |  manifests        generated files must match their recorded hashes
  |  offline checks   identities, datasets.yaml, overrides file, generate output
  v
pull request: check (recommended; see "Workflows")
  |  drift            state mirror must equal live Linear (content hash)
  |  orphans          every managed Linear customer must be in a dataset
  |  pending          every Linear edit must be resolved in linear-overrides.yaml
  v
approval in the deploy environment
  v
apply
  |  snapshot         before the first write
  |  check-and-set    re-read each row; abort if it changed
  |  never-null       the client refuses to send an empty value
  |                   unless the field may clear
  |  verification     compare each returned row with its target
  v
restore             write a snapshot back, skipping rows edited since
```

- **Field ownership.** The registry owns every written field, so an apply can clear a
  revenue or tier and remove a domain. Each such change is a registry diff a reviewer saw:
  upstream dropped the value, or `linear-overrides.yaml` pins or removes it. The apply
  never deletes a customer, and it never writes `status`, `name` or `external_ids` empty.
  It replaces `external_ids` wholesale on every managed customer, stubs included: it sets
  the list to the canonical id alone.
- **Pending entries block writes.** A Linear edit that the registry does not accept is
  recorded as a pending entry. `check` fails on it, which stops the workflow's deploy job
  before any write, and `deploy` aborts that customer's row itself as a second guard.
  Nothing overwrites an unreviewed Linear edit.
- **Deletions are confirmed twice.** `sync-inbound` records a customer as deleted only
  when the listing omits it and a direct read by id reports it not found.
- **Legacy files stop the run.** A data repository that still has `linear-ignore.yaml` or
  `linear-retire.yaml` exits 2 with "run adopt-live" before anything is planned. No
  hidden customer becomes managed, and no clear slips through.
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
  unless the caller passes `retire=True`, and `create_customer` takes no `retire`. The
  engine passes it only for a change that clears or removes a value, and only on a field
  that may clear (`domains`, `revenue`, `tier`). A bug in the planner cannot erase a name,
  status or external id by sending an empty field.
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
    |               state PR: sync-state/<run>-<attempt>
    |               (mirror + snapshot, opened by the GitHub App)
    | merge state PR
    +-------------> main (state paths do not start a deploy)
```

The state PR holds `<data_dir>/linear-state/` and `<data_dir>/linear-snapshots/`, which
are not deploy inputs, plus any change the post-apply `sync-inbound` made elsewhere under
`<data_dir>`: a recorded edit or deletion in `linear-overrides.yaml`, and then the
`linear/` and CSVs that `sync-inbound` regenerates from it. A state PR that changes the
overrides file starts a deploy run when it merges. That run's plan leaves an absorbed
clear, removal or deletion alone, because the regenerated `linear/` already agrees with
Linear; a pending entry aborts its row until someone resolves it. `deploy` refuses a
`linear/` that is out of date with the overrides file, and never writes a row whose
canonical id is `deleted` or `hidden`, so a stale `linear/` cannot write back what
`sync-inbound` recorded.

**Merge a deploy's state PR before the next refresh PR.** The refresh compares the
mirror on `main` with live Linear. Until the state PR merges, that mirror predates the
deploy. A deploy's own write still reads as no edit, because live equals the desired
value. But if someone changed the registry again after the deploy and before the state PR
merged, the refresh sees a mirror value, a live value and a desired value that all
differ, and records a conservative `pending` entry with Linear's value. Delete that entry
by hand (the registry wins), and avoid it by merging the state PR first.

## A change from Linear's side

People also create, edit and delete customers in Linear directly. The sync treats those
changes as follows.

```
someone creates customer X in Linear
  -> next check on any PR: DRIFT added + orphan X  (red)
  -> refresh (daily, or dispatched):
       sync-inbound              X enters linear-state/
       ingest --only linear-found
         X hidden?               nothing happens
         X matches an identity   Linear id appended to it
           with a notion record    no stub; the notion row owns X
         X matches another       known duplicate; never written
           written customer
         otherwise               new identity + stub datasets/linear-found/<id>.yaml
       generate                  linear/<id>.yaml (linear_owned for a stub)
       open refresh PR           sync-refresh/<run>-<attempt>
  -> merge refresh PR -> deploy (approved) stamps X's external id
```

- **An edit to a stub** (name, status, domains, revenue, tier) is Linear's to make.
  `check` does not count it as drift, the apply never reverts it, and the next refresh
  copies it into the stub.
- **An edit to a `notion` customer** shows as `DRIFT changed` until a refresh PR records
  it in the mirror. The refresh also records the edit in `linear-overrides.yaml`: a
  cleared revenue or tier, or a removed domain, becomes an accepted override; any other
  change becomes a pending entry. A pending entry fails `check` and blocks the customer's
  write until a pull request pins the value or deletes the entry (see [Resolving a
  pending entry](#resolving-a-pending-entry)). An edit that leaves Linear equal to the
  registry records nothing.
- **A customer deleted in Linear** shows as `DRIFT deleted`. The refresh confirms the
  deletion with a second read by id, then records a `deleted` entry. The next `generate`
  drops its desired row, so a deploy does not recreate it. A stub just disappears.
  Delete the `deleted` entry to recreate a `notion` customer.
- **A customer with an unmapped status** (one missing from
  `linear_status_classification`) gets a stub with no classification and no desired
  row, and `check` fails until someone maps the status or hides the customer.
- **A hidden customer** (`<data_dir>/linear-overrides.yaml`, a `hidden` entry by
  canonical id or a `hidden_name_prefixes` item) is never stubbed, never given a new
  identity, never an orphan and never written. `check` lists each hidden customer with
  its reason (`HIDDEN:` lines), and fails when a desired record resolves to the same
  identity as a hidden customer. The exception is the `linear-found` stub of that same
  Linear id: `check` only reports it, and the next refresh drops it. The prefix
  `zzz-gitops-test-` is built in (`BUILTIN_PREFIX` in `gitops_engine.ignore`). The live
  e2e tests (`tests/test_e2e_gitops.py`) create their throwaway customers in a real
  Linear workspace under names that start with it, so the sync hides any customer with
  that prefix and never stubs, writes or reports it as an orphan. The built-in prefix is
  always present, so you do not list it, and `check` shows it as a `HIDDEN PREFIX`
  line. A run whose `--scope` starts with the prefix lifts the exemption for itself,
  which is how the e2e tests write their own records. `hide --linear-id <id> --reason
  <why>` adds a `hidden` entry by resolving the customer to its canonical id through the
  identity table (it appends an identity when none matches).

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
| `linear-overrides.yaml` | Curated | `sync-inbound`, `hide`, `adopt-live` and the stub handover write; hand edits | Every decision that differs from upstream, by canonical id: pins, domain adds and removes, pending Linear edits, hidden and deleted customers, hidden name prefixes. Format: [Ownership and overrides](#ownership-and-overrides). |
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
| `generate` | Merges the datasets into `linear/` and both CSVs. | none | 0; 2 refused (hand edits) or an invalid overrides file |
| `sync-inbound [--scope P]` | Rewrites `linear-state/` from live Linear, and records the Linear edits and confirmed deletions since the last mirror in `linear-overrides.yaml`. When that file changes, it then runs `generate` offline from the committed datasets, so `linear/` and both CSVs match the overrides they derive from. Prints `N mirrored; A absorbed, P pending`. | read | 0; 2 no token, refused (hand edits to `linear-state/` or, when the overrides file would change, to `linear/`), an invalid overrides file, a legacy file, or a Linear read error (nothing written); 2 after writing the mirror and overrides when `generate` then fails |
| `check [--scope P]` | Drift, plan, pending entries and every offline check; prints the plan. Needs `LINEAR_API_TOKEN`. | read | 0 in sync; 2 no token, invalid curated file, a legacy file, or a Linear read error; 3 something to fix |
| `deploy [--apply --prod [--break-glass]] [--scope P]` | Prints the plan; with `--apply`, writes it. Refuses, before reading Linear's customers, when `linear/` differs from what `generate` builds now (in `--scope`). Aborts a desired row whose canonical id has a `pending`, `deleted` or `hidden` entry. Needs `LINEAR_API_TOKEN` even as a dry run. | read; write with `--apply` | 0 done; 2 no token, invalid curated file, guard refused, or apply refused before any write; 3 `linear/` out of date, refused plan, aborted rows or a failed create/update; 1 with a traceback on a Linear API error while reading customers or the status and tier maps |
| `restore --from <snapshot> [--rows ID...] --prod [--break-glass]` | Writes snapshot values back with check-and-set. | write | 0 all restored; 2 no token, guard refused, unreadable snapshot, or a write error that stopped it; 3 some rows skipped; 1 with a traceback on a Linear API error while reading the status and tier maps |
| `hide --linear-id ID --reason TEXT` | Adds a `hidden` entry for a mirrored Linear customer to `linear-overrides.yaml`. The id must be in the state mirror, so run `sync-inbound` first. | none | 0 hidden; 4 already hidden (its reason is kept); 2 refused |
| `refresh` | Runs `sync-inbound`, `ingest --only linear-found` and `generate` with `--no-input`, and stops at the first non-zero exit. Never runs `check` and never writes to Linear. The workflow's refresh job calls it. | read | the first failing step's code, else 0 |
| `adopt-live [--dry-run]` | One-time migration of a v0.2.0 data repository to full ownership (see [Moving a v0.2.0 data repository](#moving-a-v020-data-repository)). Offline. | none | 0; 2 refused, nothing written |
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
`linear-overrides.yaml` and `datasets.yaml` under `<data_dir>`, and the config file
itself. The state mirror and the snapshots are not deploy inputs.

`check` fails (exit 3) on: drift, plan-time aborts (a pending entry aborts its row),
planner errors, unacknowledged destructive changes, hidden conflicts, a live customer on
two identities, orphans, a `PENDING` entry, an `OVERRIDE` failure, and the offline errors
(hand-edited or out-of-date generated files, identity conflicts, an invalid
`datasets.yaml`, unmapped stub statuses). An `OVERRIDE` failure is a malformed overrides
file, an override for a canonical id with no identity, a `pin` or `pending` on a stub, a
`pin: null` on `name`, `status` or `external_ids`, a pin of `external_ids`, a pinned value
of the wrong type, or a `domains.remove` for a domain upstream does not carry. It reports
without failing: known duplicates, withheld domains, unused identities, records with no
identity, a pin equal to the upstream value (redundant), the `deleted` entries (what the
sync will not recreate), and the hidden customers. A passing run ends with `IN SYNC`.

## Workflows

The reusable GitHub Actions workflow lives in `gitops-engine`
(`.github/workflows/sync.yml`). This repository ships a short caller to copy into the
repository that holds the registry,
[`examples/caller-workflow.yml`](../examples/caller-workflow.yml). The
[setup runbook](runbooks/setup.md) covers adoption.

The caller triggers it on a push to `main`, a daily schedule and manual dispatch. The
workflow reads the data directory and both environment names from the registry config
(`workflow.deploy_environment` and `workflow.readonly_environment`), so the caller has no
path filter. Its jobs:

| Job | Runs when | Environment | Steps |
|---|---|---|---|
| `plan` | always | none | Installs the tool, then decides the mode. On a push it runs `changed --since <before>` and ends the run when no deploy input changed. Reads the two environment names with `config-get`. |
| `deploy` | a push that changed a deploy input, or a dispatch with `mode` `deploy`, `restore` or `e2e` | `workflow.deploy_environment` (write token; the required reviewer is environment configuration) | `deploy`: `check`, then `deploy --apply --prod --no-input`. `restore`: `restore --from <snapshot> [--rows]`. Both then upload the snapshots, run `sync-inbound`, summarise the run, and open a state PR. `e2e`: runs the live GitOps test from the installed tool's own source and opens no state PR. |
| `refresh` | the daily schedule, or a dispatch with `mode: refresh` | `workflow.readonly_environment` (no approval, read-only token) | `refresh` (`sync-inbound`, `ingest --only linear-found`, `generate`), then a refresh PR on branch `sync-refresh/<run-id>-<attempt>`. It never runs `check` and never writes to Linear. |

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
- The caller installs the tool from its own uv project, with a committed `uv.lock`
  (`project_dir` input, default the repository root). Each job runs `uv sync --locked` and
  `uv run --frozen`, so every job runs the locked tool and dependencies. The lock must use
  https git URLs or an index; the jobs hold no deploy key.
- The two Linear tokens are secrets of the GitHub environments, named
  `DESTINATION_TOKEN` (deploy environment) and `DESTINATION_READONLY_TOKEN` (read-only
  environment). The caller does not pass them; an environment secret takes precedence
  over a passed one, so the write token is released only after the required reviewer
  approves. The workflow exports the token as `LINEAR_API_TOKEN` (the caller's
  `token-env` input) for the steps that call Linear, and for no other step.
- A failing `check` in the `deploy` job stops the job before the apply, so no state PR
  opens; a refresh PR, or an edit to `linear-overrides.yaml` that resolves a pending
  entry, is the way out.
- The state PR opens on branch `sync-state/<run-id>-<attempt>`. Merge it before the next
  refresh PR (see [A change from the contributor's side](#a-change-from-the-contributors-side)).
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
- A Linear API error in `deploy` or `restore` exits 1 with a traceback instead of 2.
  `check`, `sync-inbound` and `refresh` catch it and exit 2.
- Linear's not-found response for a hard-deleted customer is unverified. Until the live
  test `test_get_after_delete_is_not_found` has run, a hard-deleted customer makes
  `sync-inbound` and `refresh` exit 2 (see the [operations
  runbook](runbooks/operations.md#a-deleted-customer-and-the-unverified-not-found-shape)).
- A registry change made over a stale mirror (the deploy's state PR unmerged) records a
  conservative pending entry. Merge the state PR first, or delete the entry.
- After a Linear customer is renamed into a `--scope` prefix, the mirror holds two rows
  for its Linear id until an unscoped `sync-inbound`.
- The refresh job does not close older refresh PRs. Re-running an old refresh run
  replays its original commit and produces a PR that conflicts with `main`.
- The sync never deletes a Linear customer. An unmatched live customer or orphan stays
  in Linear until a person removes or merges it there. A customer deleted in Linear is
  never recreated while its `deleted` entry exists.
- `ingest` reads only pages that exist in Notion. A customer that has no Notion page is
  missing from the `notion` dataset until it has one.

## Code map

| Module | Role |
|---|---|
| `sources/` (`base.py`, `notion.py`), `config.py` | The source adapter interface and the `SOURCES` registry; the Notion adapter (page extraction, cross-bucket merge, curated self-managed and override inputs); the config file and its `source:` section. |
| `datasets.py`, `schema.py` | `datasets.yaml`, dataset directories, `CustomerRecord`. |
| `identity.py` | The customer's identity declarations: `IDENTITY_SPEC` (source, domain and name keys, their order and normalisers), the `keys_for_*` reducers and `implicit_identity`. The table, `resolve`, validation and the comment-preserving store are in `gitops-engine`. |
| `linear_found.py` | Stub building and unmapped-status errors. |
| `generate.py`, `writers.py` | Merge by identity, projection to Linear rows, YAML and CSV writers. |
| `linear_schema.py`, `linear_client.py`, `linear_destination.py` | `LINEAR_SCHEMA` (the field policies the engine's planner runs, the mirror and snapshot rows), `content_hash_of`, the GraphQL client, and `LinearDestination`, the engine's destination for Linear writes. |
| `linear_overrides.py`, `inbound.py`, `migrate.py` | The overrides format bound on the engine's, `pin_customer` and value-type checks; `sync-inbound`'s read and write of the mirror, edits and deletions; `adopt-live`. |
| `deploy.py`, `linear_files.py` | Plan, apply and `restore` bound to the engine's deploy and snapshot code; hidden-row binding, the snapshot file name and the legacy file names. |
| `check.py` | `check`: the engine's `run_check` with the customer dataset hooks bound, plus the offline checks and the override findings. |
| `deploy_plan.py` | The planning path `deploy` and `check` share, and `offline_plan` (the state mirror stands in for live Linear). |
| `changed.py`, `tool_source.py` | `changed` and `tool-source`, which the reusable workflow calls. |
| `cli.py` | The commands above. |

Linear API details the client depends on are in
[`linear-api-notes.md`](linear-api-notes.md).
