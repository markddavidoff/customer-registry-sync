# customer-registry-sync

Build a customer registry from Notion pages, keep it as YAML files in git, and sync it to Linear customers. Notion is read-only input. Linear customers become code: you change a file, review the diff, and `deploy` applies it.

The flow has two layers. Layer 1 is the dataset built from Notion; Layer 2 is its Linear projection (the desired customers plus a mirror of live Linear). The [files table](#files-under-data_dir) defines both.

```
Notion hub pages
  -> ingest        reads the pages, merges hand-curated inputs, writes the dataset (Layer 1)
  -> generate      projects the dataset into desired Linear customers (Layer 2)
  -> deploy        applies Layer 2 to Linear, one customer at a time, with a check-and-set
  <- sync-inbound  mirrors live Linear back into the repo (the "state" files)
  -> check         read-only gate: reports drift between the mirror and live Linear, plus the plan
```

## Who it's for

A team that keeps one Notion page per customer and wants Linear's customer list to follow it through reviewed pull requests. The pages are often populated by a CRM sync, so some fields carry a CRM label such as "HubSpot"; that is only a field label, and the tool never talks to the CRM.

Prerequisites:

- Customer pages in Notion, grouped under bucket pages as described in [`docs/notion-template.md`](docs/notion-template.md).
- A Linear workspace with **Customers** enabled (a workspace setting). `sync-inbound`, `deploy` and `check` read and write Linear customers.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- The library [`gitops-engine`](https://github.com/markddavidoff/gitops-engine), which holds the plan, drift and check-and-set logic. `uv sync` installs it from git (tag `v0.1.0`) over SSH, so your SSH key needs read access to that repository.
- `NOTION_API_TOKEN` for `ingest`: a Notion integration token with the bucket pages shared with it.
- `LINEAR_API_TOKEN` for `sync-inbound`, `deploy` and `check`. `deploy --apply` needs write scope.

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
```

Every command takes the global `--config <path>` (default `registry-sync.yaml` in the working directory). A missing file, or a missing key, prints one line to stderr and exits 2. `data_dir` is where the dataset lives; `--out-dir` overrides it per run.

## Use

```bash
export NOTION_API_TOKEN=...
export LINEAR_API_TOKEN=...

customer-registry-sync ingest --dry-run         # Notion -> dataset (preview)
customer-registry-sync ingest
customer-registry-sync generate                 # dataset -> desired Linear customers
customer-registry-sync sync-inbound             # live Linear -> state mirror
customer-registry-sync check                    # drift + plan; see exit codes below
customer-registry-sync deploy                   # dry run: prints the plan
customer-registry-sync deploy --apply --prod    # writes to Linear
```

From a checkout, run it with `uv run customer-registry-sync ...`.

- `deploy` is a dry run unless you pass `--apply`. With `--apply`, writing into the configured `data_dir` also requires `--prod`, so a stray `--apply` cannot touch production data by accident.
- `--scope <prefix>` limits `sync-inbound`, `deploy` and `check` to customers whose name starts with the prefix.
- `deploy` never deletes. Linear customers with no desired row are reported as orphans.
- `generate` warns on stderr when the dataset directory is missing or empty (run `ingest` first), and still exits 0.
- Each update is guarded twice. At plan time, a customer aborts only when **both** its live `updatedAt` moved since the recorded `linear_synced.updated_at` **and** its content hash differs from the recorded one; Linear bumps `updatedAt` for fields the sync ignores, so a moved `updatedAt` alone is tolerated. At write time, each update re-reads the customer and aborts if its `updatedAt` changed since planning or the customer is gone. Any abort makes the exit code 3.

### Exit codes

| Code | Meaning | When |
|---|---|---|
| 0 | OK / in sync | The command finished. For `check`: no drift and no aborts. For `deploy`: no aborted customer. |
| 2 | Operational or usage error | Missing or invalid config file or key (any command); `NOTION_API_TOKEN` unset (`ingest`); `LINEAR_API_TOKEN` unset (`sync-inbound`, `deploy`, `check`); `deploy` with no `<data_dir>/linear/` files and no `--scope`; `deploy --apply` against the configured `data_dir` without `--prod`; a failed Linear read in `check`; an unknown command or flag. |
| 3 | Drift or aborts | `check`: the state mirror no longer matches live Linear, or the plan has aborts. `deploy` (dry run or `--apply`): at least one customer aborted. |

An unexpected API error outside `check` (for example a Linear or Notion failure during `deploy` or `ingest`) raises and exits 1 with a traceback.

## Files under `data_dir`

| Path | Written by | Purpose |
|---|---|---|
| `dataset/<slug>.yaml`, `registry.csv`, `real-customers.csv` | `ingest` | **Layer 1, the dataset:** one merged record per customer, built from Notion plus the hand-curated inputs |
| `linear/<slug>.yaml` | `generate` | **Layer 2, desired:** the Linear customer the dataset projects to. A pull request changes these. |
| `linear-state/<slug>.yaml` | `sync-inbound`, `deploy` | **Layer 2, state:** the last-synced mirror of live Linear |
| `self-managed-customers.yaml` | you | On-prem and hybrid deployment registry; merged, never overwritten |
| `classification-overrides.yaml` | you | Per-domain or per-name classification overrides |

[`examples/`](examples/) has a sample of each hand-curated input.

## Notion page layout

`ingest` parses machine-generated pages with a deterministic block walk. [`docs/notion-template.md`](docs/notion-template.md) lists exactly which blocks and fields it reads, so you can build compatible pages. Linear details (schema, concurrency, rate limits) are in [`docs/linear-api-notes.md`](docs/linear-api-notes.md).

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

The live end-to-end tests (`tests/test_e2e_gitops.py`) create and delete throwaway `zzz-gitops-test-*` customers, with `.invalid` domains, in a real Linear workspace. They are skipped unless you pass `--run-live` and set `LINEAR_API_TOKEN`.

## License

MIT. See `LICENSE`.
