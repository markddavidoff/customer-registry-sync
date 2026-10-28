# customer-registry-sync

Build a customer registry from Notion pages, keep it as YAML files in git, and sync it to Linear customers. Notion is read-only input, and reading it is a manual step: you run `ingest` on your machine, run `generate`, and open a pull request. The GitHub workflow never reads Notion and has no Notion token. Linear customers become code: you change a file, review the diff, and a reviewed deploy applies it.

The flow has two layers. Layer 1 is the dataset built from Notion and from Linear-only customers; Layer 2 is its Linear projection (the desired customers plus a mirror of live Linear). The [files table](#files-under-data_dir) defines both.

```
Notion hub pages
  -> ingest        (run by hand) reads the pages, merges hand-curated inputs, writes the notion dataset (Layer 1)
  -> generate      merges the datasets by identity into desired Linear customers (Layer 2)
  -> deploy        applies Layer 2 to Linear, one customer at a time, with a check-and-set
  <- sync-inbound  mirrors live Linear back into the repo (the "state" files)
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
- The library [`gitops-engine`](https://github.com/markddavidoff/gitops-engine), which supplies drift detection and the `check` exit codes. `uv sync` installs it from git (tag `v0.1.0`) over SSH.
- SSH read access to two repositories. This tool's repository and `gitops-engine` are both private today, so installing the tool needs an SSH key that can read both.
- `NOTION_API_TOKEN` for `ingest`: a Notion integration token with the bucket pages shared with it. Only your own machine needs it; the workflow never reads Notion.
- `LINEAR_API_TOKEN` for `sync-inbound`, `deploy`, `restore` and `check`. `deploy --apply` and `restore` need write scope. `deploy` needs the token even as a dry run, because it reads live Linear to build the plan.

## Configure

Copy [`examples/registry-sync.example.yaml`](examples/registry-sync.example.yaml) to `registry-sync.yaml` and fill in your Notion page ids:

```yaml
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

The six bucket names are fixed by the Notion template: the config accepts exactly these names, requires all six, and each name decides a customer's classification (see [`docs/notion-template.md`](docs/notion-template.md)). You cannot rename or add buckets in the config.

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
customer-registry-sync ignore --linear-id <id> --reason "<why>"   # never touch this customer
```

From a checkout, run it with `uv run customer-registry-sync ...`. Three more commands exist for the reusable workflow:

- `changed --since <sha>` prints `changed` or `unchanged`: whether a deploy input differs between that commit and HEAD. Any doubt (unknown sha, first push, force-push) prints `changed`. It always exits 0.
- `config-get <key>` prints one config value by dotted key, such as `data_dir` (an absolute path) or `workflow.deploy_environment`. It exits 2, naming the key, when the key is unknown or missing.
- `tool-source` prints where the installed tool came from, as `git <commit> <url>` or `dir - <path>`. It exits 2 when the install source cannot be recovered.

Order matters for three commands:

- `ignore` and `ingest --only linear-found` read the state mirror, so run `sync-inbound` first. Without a mirror, `ignore` exits 2 and `ingest --only linear-found` exits 2 with nothing written; a plain `ingest` skips the `linear-found` step and says so.
- `check` and `deploy` need `LINEAR_API_TOKEN`, and `deploy` needs it even without `--apply`. Both exit 2 when it is unset.
- `ingest` (the Notion part) is manual. Run it on your machine with `NOTION_API_TOKEN`, run `generate`, and open a PR with the result. The scheduled refresh runs only `sync-inbound`, `ingest --only linear-found` and `generate`.

Behavior to know:

- `deploy` is a dry run unless you pass `--apply`. With `--apply`, writing into the configured `data_dir` also requires `--prod`, so a stray `--apply` cannot touch production data by accident. Outside GitHub Actions, `--prod` also needs `--break-glass`.
- `--scope <prefix>` limits `sync-inbound`, `deploy` and `check` to customers whose name starts with the prefix.
- `deploy` never deletes. Linear customers with no desired row are reported as orphans. A value is removed from a customer only when a reviewed entry in `linear-retire.yaml` names it.
- `ingest`, `generate` and `sync-inbound` refuse to overwrite a generated file that was edited by hand, unless you confirm on a terminal. `--no-input` makes them refuse instead of asking.
- `generate` warns on stderr when the dataset directory is missing or empty (run `ingest` first), and still exits 0.
- Each update is guarded twice. At plan time, a customer aborts only when **both** its live `updatedAt` moved since the recorded `linear_synced.updated_at` **and** its content hash differs from the recorded one; Linear bumps `updatedAt` for fields the sync ignores, so a moved `updatedAt` alone is tolerated. At write time, each update re-reads the customer and aborts if its `updatedAt` changed since planning or the customer is gone. Any abort makes the exit code 3.
- Every apply writes a snapshot of the rows it will update before its first write. `restore` writes a snapshot back and skips any row edited in Linear since.

### Exit codes

| Code | Meaning | When |
|---|---|---|
| 0 | OK / in sync | The command finished. For `check`: no drift and no failures. For `deploy`: no aborted customer. |
| 2 | Operational or usage error | Missing or invalid config file or key (any command); `NOTION_API_TOKEN` unset (`ingest`); `LINEAR_API_TOKEN` unset (`sync-inbound`, `deploy`, `restore`, `check`); an invalid curated file; `deploy` with no `<data_dir>/linear/` files and no `--scope`; `deploy --apply` or `restore` against the configured `data_dir` without `--prod`; a failed Linear read in `check`; an unknown command or flag. |
| 3 | Drift, failures or aborts | `check`: the state mirror no longer matches live Linear, the plan has aborts or errors, or an offline check failed. `deploy` (dry run or `--apply`): a refused plan, or at least one customer aborted or failed. `restore`: some rows were skipped. |
| 4 | Already listed | `ignore`: the Linear id is already in `linear-ignore.yaml`. |

An unexpected API error outside `check` (for example a Linear or Notion failure during `sync-inbound`, `deploy` or `ingest`) raises and exits 1 with a traceback. The full per-command table is in [`docs/customer-registry.md`](docs/customer-registry.md#commands).

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
| `linear-ignore.yaml` | `ignore` appends; you edit | Linear customers the sync never touches, each with a reason |
| `linear-retire.yaml` | you | Values the next apply removes from Linear |
| `self-managed-customers.yaml` | you | On-prem and hybrid deployment registry; merged, never overwritten |
| `classification-overrides.yaml` | you | Per-domain or per-name classification overrides |

[`examples/`](examples/) has a sample of each hand-curated file. `generate`, `ingest` and `sync-inbound` replace the generated files; never edit those by hand. The layout changed from an earlier release that kept one `dataset/` directory: Notion records now live in `datasets/notion/`, and there is no migration, so move or re-run `ingest`.

## Run it in GitHub Actions

The reusable workflow lives in `gitops-engine` (`.github/workflows/sync.yml`); this repository ships a caller to copy, [`examples/caller-workflow.yml`](examples/caller-workflow.yml), which names this tool's CLI and token variable; the workflow installs the tool from the caller's locked uv project, and the two tokens live in the deploy and read-only environments. The workflow applies the registry behind a reviewer's approval, opens a state PR with the refreshed mirror and snapshot, and runs a daily read-only refresh that opens a PR when Linear changed. The caller passes five named secrets; the data directory and both environment names come from `registry-sync.yaml`. This tool's repository and the engine's are private today. A caller in the same owner account can call the workflow once Actions access is granted; a caller in a different owner can call it only if the tool repository is public, which is an owner decision still pending. No release tag exists yet, so pin a commit sha in the caller's `uses:` line and in the `uv` source until one is published. Setup steps: [`docs/runbooks/setup.md`](docs/runbooks/setup.md). Day-to-day operation and recovery: [`docs/runbooks/operations.md`](docs/runbooks/operations.md).

## Documentation

- [`docs/customer-registry.md`](docs/customer-registry.md): how the registry, identity resolution, the planner and the safety model work.
- [`docs/runbooks/setup.md`](docs/runbooks/setup.md): tokens, GitHub App, SSH key, environments, caller workflow, rollout.
- [`docs/runbooks/operations.md`](docs/runbooks/operations.md): applying, recovering from a failing `check`, retiring values, restoring snapshots.
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

To work on `gitops-engine` at the same time, clone it next to this repo and point `[tool.uv.sources]` in `pyproject.toml` at it with `gitops-engine = { path = "../gitops-engine" }` (do not commit that change).

### Tests

Test fixtures are synthetic. `tests/fixtures/generate_fixtures.py` writes them from invented companies and `example.test` domains, following the block shape the Notion API returns and the field list in `linear_client.py`. A test fails if the committed JSON drifts from the generator.

The live end-to-end tests (`tests/test_e2e_gitops.py`) create and delete throwaway customers, with `.invalid` domains, in a real Linear workspace. The sync always ignores customers with the test prefix; [`docs/customer-registry.md`](docs/customer-registry.md#a-change-from-linears-side) explains it. The tests are skipped unless you pass `--run-live` and set `LINEAR_API_TOKEN`.

## License

MIT. See `LICENSE`.
