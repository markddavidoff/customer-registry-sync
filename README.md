# customer-registry-sync

Build a customer registry from Notion pages, keep it as YAML files in git, and sync it to Linear customers. Notion is read-only input, and reading it is a manual step: you run `ingest` on your machine, run `generate`, and open a pull request. The GitHub workflow never reads Notion and has no Notion token. Linear customers become code: you change a file, review the diff, and a reviewed deploy applies it. The registry owns every field the sync writes. When someone edits one of those fields in Linear, the sync records the edit in `linear-overrides.yaml` and a pull request decides whether it stays.

The flow has two layers. Layer 1 is the dataset built from Notion and from Linear-only customers; Layer 2 is its Linear projection (the desired customers plus a mirror of live Linear). The [files table](#files-under-data_dir) defines both.

```
Notion hub pages
  -> ingest        (run by hand) reads the pages, merges hand-curated inputs, writes the notion dataset (Layer 1)
  -> generate      merges the datasets by identity into desired Linear customers (Layer 2)
  -> deploy        applies Layer 2 to Linear, one customer at a time, with a check-and-set
  <- sync-inbound  mirrors live Linear back into the repo (the "state" files) and records Linear edits and deletions in linear-overrides.yaml
  -> ingest --only linear-found   turns Linear-only customers into stubs, from the mirror
  -> check         read-only gate: reports drift between the mirror and live Linear, plus the plan
```

## Who it's for

A team that keeps one Notion page per customer and wants Linear's customer list to follow it through reviewed pull requests. The pages are often populated by a CRM sync, so some fields carry a CRM label such as "HubSpot"; that is only a field label, and the tool never talks to the CRM.

Prerequisites:

- Customer pages in Notion, grouped under bucket pages as described in [`docs/notion-template.md`](docs/notion-template.md).
- A Linear workspace with **Customers** enabled (a workspace setting). `sync-inbound`, `deploy` and `check` read and write Linear customers.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- The library [`gitops-engine`](https://github.com/markddavidoff/gitops-engine) v0.3.0, which supplies the generic machinery: dataset files, the identity table, merge, the field planner, the overrides file, edit and deletion detection, check, apply, snapshots and restore, and the reusable workflow. `uv sync` installs it from git (tag `v0.3.0`) over https; no SSH key is involved.
- `NOTION_API_TOKEN` for `ingest`: a Notion integration token with the bucket pages shared with it. Only your own machine needs it; the workflow never reads Notion.
- `LINEAR_API_TOKEN` for `sync-inbound`, `deploy`, `restore` and `check`. `deploy --apply` and `restore` need write scope. `deploy` needs the token even as a dry run, because it reads live Linear to build the plan.

## What this tool declares

The engine holds the generic machinery and knows nothing about customers, Linear or Notion. This repository supplies the parts that are specific to them:

| Declaration | Where | What it says |
|---|---|---|
| Record schema | `linear_schema.py` (`LINEAR_SCHEMA`) | Six planned fields, all owned by the registry. `name`, `status` (`required`) and `external_ids` are always written. `domains` is a list the plan owns (`clears`): a live domain the registry does not list is removed, and it is `exclusive`, so a domain another customer holds is withheld. `revenue` and `tier` also `clear`: an empty registry value clears Linear's. A Linear-owned stub row takes every value from Linear and writes only its canonical id. The schema also carries the nouns the engine prints (`customer`, `Linear`, `registry`, `updatedAt`). |
| Identity keys | `identity.py` (`IDENTITY_SPEC`) | Resolution tries the canonical id, then the source ids (`notion`, then `linear`), then the domain, then the name. Domains compare case-insensitively; names ignore case, spaces, punctuation and a trailing status suffix such as ` [Active]`. |
| Destination | `linear_destination.py`, `linear_client.py` | The engine's `Destination` over the Linear GraphQL client. The client keeps its own never-null guard as a second line behind the engine's. |
| Overrides format | `linear_overrides.py` (`OVERRIDES_FORMAT`) | `linear-overrides.yaml`, keyed by canonical id under `customers`; the pinnable fields `name`, `status`, `revenue` and `tier`; the list field `domains`; and `linear_id` as the key of a deleted entry. Pinned names take the status suffix. |
| File names | `linear_files.py`, `datasets.py` | `linear-snapshots/`, `linear-state/`, the `notion` and `linear-found` datasets, and the `linear_status_classification` table in `datasets.yaml`. |
| Source adapter | `sources/` | `NotionSource`, picked by `source.kind` in `registry-sync.yaml`. |
| CLI | `cli.py` | The commands the engine's reusable workflow calls (its CLI contract), plus `ingest`, `generate`, `hide` and `adopt-live`. |

## Configure

Copy [`examples/registry-sync.example.yaml`](examples/registry-sync.example.yaml) to `registry-sync.yaml` and fill in your Notion page ids:

```yaml
source:
  kind: notion                                # the source adapter; the only kind today
  hub_page_id: <hub page id>
  buckets:
    Customers: <page id>
    Churned Customers: <page id>
    Archive: <page id>
    POCs: <page id>
    Prospects: <page id>
    Partner & Supporting Accounts: <page id>
data_dir: customers                           # optional, relative to this file
workflow:                                     # optional; read by the reusable workflow
  deploy_environment: <GitHub environment with the required reviewer>
  readonly_environment: <GitHub environment for the scheduled refresh>
```

`source:` picks the adapter that reads the upstream system. Adapters ship inside this package (`src/customer_registry_sync/sources/`), so the data repository names one with `kind:` and nothing is loaded from it. An unknown `kind` is an error that names the known kinds. `hub_page_id` or `buckets` at the top level (the v0.1 shape) is an error that points to `source:`; move both under `source:` and add `kind: notion`.

The six bucket names are fixed by the Notion template: `source.buckets` accepts exactly these names, requires all six, and each name decides a customer's classification (see [`docs/notion-template.md`](docs/notion-template.md)). You cannot rename or add buckets in the config.

The config file holds no secret. `DESTINATION_TOKEN` and `DESTINATION_READONLY_TOKEN` do not appear in it: the caller workflow reads them from its GitHub environments (see [Run it in GitHub Actions](#run-it-in-github-actions)).

Every command takes the global `--config <path>` (default `registry-sync.yaml` in the working directory). A missing file, or a missing required key, prints one line to stderr and exits 2. `data_dir` is where the registry files live; a relative path resolves against the config file's directory, and `--out-dir` overrides it per run. `config-get data_dir` prints the absolute path after that resolution. The `workflow:` section holds only the two environment names; the tool runs without it, and `config-get` fails naming a missing key.

## Use

```bash
export NOTION_API_TOKEN=...
export LINEAR_API_TOKEN=...

customer-registry-sync ingest --dry-run         # Notion -> notion dataset (preview)
customer-registry-sync ingest
customer-registry-sync sync-inbound             # live Linear -> state mirror
customer-registry-sync ingest --only linear-found   # mirror -> stubs for Linear-only customers
customer-registry-sync generate                 # datasets -> desired Linear customers
customer-registry-sync check                    # drift + plan; see exit codes below
customer-registry-sync deploy                   # dry run: prints the plan
customer-registry-sync deploy --apply --prod    # writes to Linear
customer-registry-sync restore --from <snapshot> --prod   # puts a pre-apply snapshot back
customer-registry-sync hide --linear-id <id> --reason "<why>"   # take this customer out of the sync
customer-registry-sync adopt-live               # one-time: move a v0.2.0 data repo to full ownership
```

From a checkout, run it with `uv run customer-registry-sync ...`. Four more commands exist for the reusable workflow:

- `refresh` runs `sync-inbound`, `ingest --only linear-found` and `generate` in that order and stops at the first non-zero exit. It reads Linear, never writes to it, and never runs `check`.
- `changed --since <sha>` prints `changed` or `unchanged`: whether a deploy input differs between that commit and HEAD. Any doubt (unknown sha, first push, force-push) prints `changed`. It always exits 0.
- `config-get <key>` prints one config value by dotted key, such as `data_dir` (an absolute path) or `workflow.deploy_environment`. It exits 2, naming the key, when the key is unknown or missing.
- `tool-source` prints where the installed tool came from, as `git <commit> <url>` or `dir - <path>`. It exits 2 when the install source cannot be recovered.

Order matters for three commands:

- `hide` and `ingest --only linear-found` read the state mirror, so run `sync-inbound` first. Without a mirror, `hide` exits 2 and `ingest --only linear-found` exits 2 with nothing written; a plain `ingest` skips the `linear-found` step and says so.
- `check` and `deploy` need `LINEAR_API_TOKEN`, and `deploy` needs it even without `--apply`. Both exit 2 when it is unset.
- `ingest` (the Notion part) is manual. Run it on your machine with `NOTION_API_TOKEN`, run `generate`, and open a PR with the result. The scheduled refresh runs only `sync-inbound`, `ingest --only linear-found` and `generate`.

Behavior to know:

- `deploy` is a dry run unless you pass `--apply`. With `--apply`, writing into the configured `data_dir` also requires `--prod`, so a stray `--apply` cannot touch production data by accident. Outside GitHub Actions, `--prod` also needs `--break-glass`.
- `--scope <prefix>` limits `sync-inbound`, `deploy` and `check` to customers whose name starts with the prefix.
- `deploy` never deletes a Linear customer. Customers with no desired row are reported as orphans. It does clear a revenue or tier the registry leaves empty and remove a domain the registry does not list, and each of those is a change to the registry that a pull request showed: upstream dropped the value, or `linear-overrides.yaml` pins or removes it. A customer with a pending entry in `linear-overrides.yaml` is not written (see [Who owns a field](#who-owns-a-field)).
- `ingest`, `generate` and `sync-inbound` refuse to overwrite a generated file that was edited by hand, unless you confirm on a terminal. `--no-input` makes them refuse instead of asking.
- `generate` warns on stderr when the dataset directory is missing or empty (run `ingest` first), and still exits 0.
- Each update is guarded twice. At plan time, a customer aborts only when **both** its live `updatedAt` moved since the recorded `linear_synced.updated_at` **and** its content hash differs from the recorded one; Linear bumps `updatedAt` for fields the sync ignores, so a moved `updatedAt` alone is tolerated. At write time, each update re-reads the customer and aborts if its `updatedAt` changed since planning or the customer is gone. Any abort makes the exit code 3.
- Every apply writes a snapshot of the rows it will update before its first write. `restore` writes a snapshot back and skips any row edited in Linear since.

### Exit codes

| Code | Meaning | When |
|---|---|---|
| 0 | OK / in sync | The command finished. For `check`: no drift and no failures. For `deploy`: no aborted customer. |
| 2 | Operational or usage error | Missing or invalid config file or key (any command); `NOTION_API_TOKEN` unset (`ingest`); `LINEAR_API_TOKEN` unset (`sync-inbound`, `deploy`, `restore`, `check`); an invalid curated file, `linear-overrides.yaml` included; a leftover `linear-ignore.yaml` or `linear-retire.yaml` (run `adopt-live`); `deploy` with no `<data_dir>/linear/` files and no `--scope`; `deploy --apply` or `restore` against the configured `data_dir` without `--prod`; a failed Linear read in `check`, `sync-inbound` or `refresh` (nothing written); an unknown command or flag. |
| 3 | Drift, failures or aborts | `check`: the state mirror no longer matches live Linear, a pending entry waits for a decision, `linear-overrides.yaml` has a failure, the plan has aborts or errors, or an offline check failed. `deploy` (dry run or `--apply`): a refused plan, or at least one customer aborted or failed. `restore`: some rows were skipped. |
| 4 | Already hidden | `hide`: the customer is already hidden. |

An unexpected API error in `deploy`, `restore` or `ingest` (a Linear or Notion failure) raises and exits 1 with a traceback. `check`, `sync-inbound` and `refresh` exit 2 on a Linear read error instead. The full per-command table is in [`docs/customer-registry.md`](docs/customer-registry.md#commands).

## Files under `data_dir`

| Path | Written by | Purpose |
|---|---|---|
| `datasets/notion/<slug>.yaml` | `ingest` | **Layer 1, the Notion dataset:** one merged record per customer, built from Notion plus the hand-curated inputs |
| `datasets/linear-found/<id>.yaml` | `ingest --only linear-found` | **Layer 1, stubs:** one record per Linear customer that Notion does not know; Linear owns its fields |
| `registry.csv`, `real-customers.csv` | `generate` | Every merged record, and the `real` and `partner` subset |
| `linear/<id>.yaml` | `generate` | **Layer 2, desired:** the Linear customer each identity projects to. A pull request changes these. |
| `linear-state/<id>.yaml` | `sync-inbound`, `deploy` | **Layer 2, state:** the last-synced mirror of live Linear |
| `linear-snapshots/<UTC>.yaml` | `deploy --apply` | Pre-apply copy of each updated row, for `restore` |
| `*/.manifest.yaml` | each generator | File hashes; `check` fails on a hand edit |
| `identities.yaml` | `ingest` appends; you edit | The identity table: which names, domains and ids are one company |
| `datasets.yaml` | you | Dataset priority order and the Linear status to classification table. List `linear-found` here once you use the refresh |
| `linear-overrides.yaml` | `sync-inbound`, `hide`, `adopt-live` and the `notion` handover write; you edit | Every decision that differs from upstream, by canonical id: pins, domain adds and removes, pending Linear edits, hidden and deleted customers, hidden name prefixes |
| `self-managed-customers.yaml` | you | On-prem and hybrid deployment registry; merged, never overwritten |
| `classification-overrides.yaml` | you | Per-domain or per-name classification overrides |

[`examples/`](examples/) has a sample of each hand-curated file. `generate`, `ingest` and `sync-inbound` replace the generated files; never edit those by hand. `linear-overrides.yaml` replaces `linear-ignore.yaml` and `linear-retire.yaml`. v0.3.0 refuses to run `check`, `deploy`, `ingest`, `sync-inbound`, `refresh` or `hide` while either old file exists; run `adopt-live` to migrate (see [Moving a v0.2.0 data repository](#moving-a-v020-data-repository)).

## Who owns a field

The registry owns every field the sync writes: `name`, `status`, `external_ids`, `domains`, `revenue` and `tier`. The registry is the generated desired row (`linear/<id>.yaml`) after `linear-overrides.yaml` is applied. An empty registry `revenue` or `tier` clears Linear's value, and a Linear domain the registry does not list is removed. Only the Linear-owned stubs differ: Linear is their upstream, so the sync copies Linear's values into the stub and writes only its canonical id.

People still edit Linear. `sync-inbound` compares each managed customer in the old mirror with live Linear, field by field, and decides what to record in `linear-overrides.yaml`:

| Edit in Linear | Recorded as | Effect |
|---|---|---|
| Revenue or tier cleared, or a domain removed | An accepted override: `pin: {field: null}`, or `domains.remove` | Absorbed. `check` passes. |
| Any other change to `name`, `status`, `revenue` or `tier`, or an added domain | A `pending` entry holding Linear's value | `check` fails, and `deploy` does not write that customer, until a pull request resolves it. |
| `status` or `external_ids` cleared, or `external_ids` changed | Nothing | The next deploy writes the registry value back. |

Names compare without their ` [Status]` suffix, so an edit that only changes the suffix is not an edit. A domain that Linear re-cased is not an edit either.

To resolve a pending entry, edit `linear-overrides.yaml` in a pull request. To keep Linear's value, move it from `pending` into `pin` (or, for `domains_add`, into `domains.add`). For the registry to win, delete the pending value, and the next deploy overwrites Linear. `check` prints both options beside each pending entry.

A customer deleted in Linear is recorded as `deleted` only after `sync-inbound` reads it again by id and Linear reports it not found. The entry stops the sync from recreating it; delete the entry to bring the customer back. A customer Linear has archived counts as deleted. A `hidden` entry takes a customer out of the sync on both sides; `hide` writes one. [`docs/customer-registry.md`](docs/customer-registry.md#ownership-and-overrides) has the full rules, and [`examples/linear-overrides.example.yaml`](examples/linear-overrides.example.yaml) shows one entry of each kind.

### Moving a v0.2.0 data repository

Full ownership would clear every Linear-only revenue and tier and remove every Linear-only domain, so `adopt-live` runs first. It works offline against the committed mirror and needs no token. It pins the live revenue and tier the registry lacks and adds the live domains the registry lacks, converts `linear-ignore.yaml` into `hidden` entries and `hidden_name_prefixes`, and deletes both old files. It refuses to run while `linear-retire.yaml` has entries (apply and remove those under v0.2.0 first), or while the state mirror is missing. After it runs, run `generate` and then `check`.

## Run it in GitHub Actions

The reusable workflow lives in `gitops-engine` (`.github/workflows/sync.yml`) and calls this tool only through the engine's [CLI contract](https://github.com/markddavidoff/gitops-engine/blob/main/docs/cli-contract.md). This repository ships a caller to copy, [`examples/caller-workflow.yml`](examples/caller-workflow.yml). It names the command (`customer-registry-sync`) and the token variable this tool reads (`LINEAR_API_TOKEN`).

- **Install.** The caller's repository holds a uv project (the workflow's `project_dir` input, default the root) that depends on this tool and has a committed `uv.lock`. Each job runs `uv sync --locked` and then `uv run --frozen`, so a job that holds a token runs only locked code. The lock must use https git URLs or an index, because the jobs hold no deploy key.
- **Secrets.** The two Linear tokens are environment secrets and are not passed by the caller: `DESTINATION_TOKEN` (write scope) lives in the deploy environment, behind its required reviewer, and `DESTINATION_READONLY_TOKEN` lives in the read-only environment. An environment secret takes precedence over a passed one, so the write token is released only after the reviewer approves a deploy. The workflow exports the token as `LINEAR_API_TOKEN` for each call and only for the steps that call Linear. The caller passes the GitHub App's `APP_ID` and `APP_PRIVATE_KEY` by name.
- **Config.** The data directory, the state and snapshot directories and both environment names come from `registry-sync.yaml` through `config-get`.
- **Modes.** A push that changed a deploy input applies the registry behind the approval and opens a state PR with the refreshed mirror and snapshot (and any override the post-apply `sync-inbound` recorded). A daily read-only refresh opens a PR when Linear changed. A dispatch can also run `restore` or `e2e`.

Setup steps: [`docs/runbooks/setup.md`](docs/runbooks/setup.md). Day-to-day operation and recovery: [`docs/runbooks/operations.md`](docs/runbooks/operations.md).

## Documentation

- [`docs/customer-registry.md`](docs/customer-registry.md): how the registry, ownership and overrides, identity resolution, the planner and the safety model work, and the code map.
- [`docs/runbooks/setup.md`](docs/runbooks/setup.md): tokens, GitHub App, environments, locked caller project, caller workflow, rollout.
- [`docs/runbooks/operations.md`](docs/runbooks/operations.md): applying, recovering from a failing `check`, resolving Linear edits, hiding customers, restoring snapshots.
- [`docs/notion-template.md`](docs/notion-template.md): the Notion page layout `ingest` reads, so you can build compatible pages, and the fields of the dataset record it writes (sample: [`examples/dataset-record.example.yaml`](examples/dataset-record.example.yaml)).
- [`docs/linear-api-notes.md`](docs/linear-api-notes.md): Linear schema, concurrency and rate limits.
- [`ROADMAP.md`](ROADMAP.md): what is built and what is open.

## Development

### Development setup

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

To work on `gitops-engine` at the same time, clone it next to this repo and point `[tool.uv.sources]` in `pyproject.toml` at it with `gitops-engine = { path = "../gitops-engine", editable = true }` (do not commit that change). Mechanisms that moved to the engine are tested there, against its toy schema; the tests here cover the customer declarations and the CLI paths.

### Tests

Test fixtures are synthetic. `tests/fixtures/generate_fixtures.py` writes them from invented companies and `example.test` domains, following the block shape the Notion API returns and the field list in `linear_client.py`. A test fails if the committed JSON drifts from the generator.

The live end-to-end tests (`tests/test_e2e_gitops.py`) create and delete throwaway customers, with `.invalid` domains, in a real Linear workspace. The sync always hides customers with the test prefix; [`docs/customer-registry.md`](docs/customer-registry.md#a-change-from-linears-side) explains it. The tests are skipped unless you pass `--run-live` and set `LINEAR_API_TOKEN`.

## License

MIT. See `LICENSE`.
