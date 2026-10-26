# Operate the registry sync

This file covers day-to-day operation and recovery. First-time setup is in
[`setup.md`](setup.md). How the registry and the sync work is in
[`../customer-registry.md`](../customer-registry.md).

`<data_dir>` is the `data_dir` in your `registry-sync.yaml` (default `customers`). Commands
run from the repository that holds the registry as `customer-registry-sync <command>`
(or `uv run customer-registry-sync <command>`), with `--config <path>` before the command
when the config is not `registry-sync.yaml` in the working directory. Secrets come from
the environment: `LINEAR_API_TOKEN`, and `NOTION_API_TOKEN` for `ingest`.

- `check`: drift, plan, orphans (a Linear customer in no dataset), identity and
  ignore-list conflicts, stale retire entries, hand-edited or out-of-date generated
  files. Exit 0 ok, 2 error, 3 any of those found. Untracked values and known duplicates
  are reported without failing.
- `sync-inbound`: refresh `<data_dir>/linear-state/` from live Linear.
- `ingest`: build `<data_dir>/datasets/notion/` from Notion (needs `NOTION_API_TOKEN`),
  then `<data_dir>/datasets/linear-found/` from the mirror; appends to
  `<data_dir>/identities.yaml`. `ingest --only linear-found` skips Notion and reads only
  the mirror.
- `generate`: merge the datasets through `<data_dir>/identities.yaml`, then write
  `<data_dir>/linear/`, `<data_dir>/registry.csv` and `<data_dir>/real-customers.csv`.
- `ignore --linear-id <id> --reason "<why>"`: add a Linear customer to
  `<data_dir>/linear-ignore.yaml` (offline; exit 0 added, 4 already listed, 2 refused).
- `deploy`: dry-run plan with field-level changes. Applies run from the workflow, not a
  laptop (see "Applying").
- `restore --from <snapshot>`: put snapshot values back (see Runbook 7).

`ingest`, `generate`, `sync-inbound`, `deploy` and `restore` accept `--no-input` (never
prompt). `ingest`, `generate` and `sync-inbound` write generated files and refuse to
overwrite one that was edited by hand: they prompt in a terminal and exit 2 with
`--no-input` or without a terminal. The curated and generated files are listed in
[`../customer-registry.md`](../customer-registry.md#files-in-data_dir).

`<data_dir>/linear-retire.yaml` is keyed by the customer's external id: the
`external_ids[0]` value in `<data_dir>/linear/<file>.yaml`. That id is also the
customer's key in `<data_dir>/identities.yaml`.

## Applying

Applies run from the `registry-sync` GitHub Actions workflow, whose deploy environment
requires a reviewer's approval before anything touches Linear.

1. A push to the default branch that changes a deploy input starts the workflow: any file
   under `<data_dir>/linear/`, `<data_dir>/identities.yaml`,
   `<data_dir>/linear-ignore.yaml`, `<data_dir>/datasets.yaml`,
   `<data_dir>/linear-retire.yaml`, or `registry-sync.yaml`. To start it by hand:
   Actions, `registry-sync`, Run workflow, `mode: deploy`.
2. The reviewer reads the field-level plan in the job log and approves.
3. The job runs `check`, then `deploy --apply --prod --no-input`. Before the first write
   it saves a snapshot to `<data_dir>/linear-snapshots/<UTC>.yaml`; after each write it
   verifies the row Linear returned and aborts the remaining rows on any unplanned
   change.
4. The job refreshes `<data_dir>/linear-state/` and opens a state PR (branch
   `linear-state/<run-id>-<attempt>`) with the new mirror and the snapshot. Merge it. The
   job summary carries the apply report and the exact `restore` command.

A failed apply still opens the state PR, and the run is marked failed.

**Break-glass (laptop).** `deploy --apply --prod` outside GitHub Actions refuses unless
`--break-glass` is passed. Use it only when the workflow is unavailable; it prints a
warning and still writes the snapshot. Afterwards run `sync-inbound` and commit the
state mirror and snapshot in a PR.

```bash
customer-registry-sync deploy --apply --prod --break-glass
```

## Runbook 1: Happy path, in-sync change

1. `ingest` updates the datasets. `generate` writes `<data_dir>/linear/`; its diff is
   the change.
2. Open a PR with the `<data_dir>/linear/` diff.
3. The PR check (if you added one) runs `check`: state equals live (no drift); the plan
   shows the intended creates and updates. **Exit 0 means the PR is green.**
4. Merge. The workflow runs after approval (see "Applying"); the write-time
   check-and-set passes and the writes land.
5. **`sync-inbound` (required, not optional)** refreshes `<data_dir>/linear-state/` so
   state equals live. The apply output lists only created and updated rows and
   drops unchanged rows and orphans, so it does not fully reconcile state. The workflow
   does this and opens the state PR.

## Runbook 2: Out of sync at PR CI

1. A PR is open. Between the last `sync-inbound` and CI, someone edited Linear.
2. `check`: recorded state differs from live (content hash mismatch), so **exit 3 and the
   PR is red**; the log names the drifted rows (changed, added, deleted).
3. **Resolution:** `sync-inbound` refreshes `linear-state/`; `generate` re-projects the
   desired rows; resolve conflicts in the PR; push. CI re-runs and goes green.

## Runbook 3: Out of sync at deploy (post-merge)

1. A PR passed its checks and merged. Linear changes before or while the deploy runs.
2. A change that lands before the deploy job's `check` step fails that step with `DRIFT`
   lines (exit 3), and nothing is applied. A change that lands after `check` is caught by
   the apply: it re-reads each row before writing and **aborts** a row whose `updatedAt`
   moved since the plan read it; **deploy exits 3** with the aborted rows listed. No
   clobber.
3. **Resolution (manual):** `sync-inbound` commits reality into `linear-state/`; open a
   fresh sync PR (Runbook 2's resolution); re-run, merge, deploy.

## Runbook 4: Happy path, no change

1. `generate` leaves `<data_dir>/linear/` with an empty diff.
2. `check`: no drift, plan empty. **Exit 0.** Deploy is a no-op.

## Runbook 5: Recovering from a failing check

`check` is the first step of the workflow's deploy job, so a failing `check` blocks the
apply and the state refresh that would fix it. The fix runs in the workflow's `refresh`
mode or on a laptop, then lands through a PR. `check` ends a failed run with

```
check FAILED: see the ERROR, DRIFT and OFFLINE lines above
```

Find the lines above it and use the matching section. `UNTRACKED:` and `NOTE:` lines do
not fail `check`.

### An orphan: a Linear customer in no dataset

The failure line names the customer and both ways out:

```
ERROR: orphan: Linear customer 'Example Corp' (<linear id>) is in no dataset.
  Add it: dispatch the registry-sync workflow with mode=refresh (or run sync-inbound, ingest --only linear-found, generate) and merge the PR.
  Or ignore it: customer-registry-sync ignore --linear-id <linear id> --reason "<why>"
```

**Add it to the registry.** Dispatch the workflow: Actions, `registry-sync`, Run
workflow, `mode: refresh`. (The same mode runs daily from the schedule.) It runs in the
read-only environment, so it waits for no approval and never writes to Linear. It runs
`sync-inbound`, `ingest --only linear-found` and `generate`, and opens a PR from branch
`linear-refresh/<run-id>-<attempt>` with the new mirror, the new stubs, the appended
identities and the regenerated output. It opens no PR when nothing changed. Merge the
PR. The merge touches `<data_dir>/linear/` or `<data_dir>/identities.yaml`, so the
workflow runs `deploy` behind its approval: that plan stamps the new stubs' external ids.

To do the same by hand, in this order:

```bash
customer-registry-sync sync-inbound
customer-registry-sync ingest --only linear-found
customer-registry-sync generate
```

Commit `<data_dir>/linear-state/`, `<data_dir>/datasets/linear-found/`,
`<data_dir>/identities.yaml`, `<data_dir>/linear/` and both CSVs in one PR. Run
`sync-inbound` first: stubs built from a stale mirror are stale.

**Or ignore it.** For a customer the sync must never manage (your own company, a test
record), run the command from the failure line with a real reason:

```bash
customer-registry-sync ignore --linear-id <linear id> --reason "<why>"
```

It reads the name from the mirror, so run `sync-inbound` first if it says the id is not
in the mirror. Exit 4 means the id is already listed and its reason is kept. Commit
`<data_dir>/linear-ignore.yaml` in a PR. Run `ingest --only linear-found` and `generate`
as well to drop an existing stub for that customer at once; the next refresh would drop
it anyway.

### An unmapped Linear status

The line reads `OFFLINE: unmapped Linear status: '<name>' (<linear id>) has status '<status>'`.
Either add a row for the status to `linear_status_classification` in
`<data_dir>/datasets.yaml` (for example `POC: prospect`), then run
`ingest --only linear-found` and `generate`; or ignore the customer as above. A customer
with no status in Linear shows `has status ''`: set its status in Linear, then dispatch
the refresh (or run `sync-inbound`, `ingest --only linear-found` and `generate`), or
ignore it.

### Known duplicates

Two live Linear customers that resolve to one identity are reported as
`KNOWN DUPLICATE: <canonical id>: '<name>' (<linear id>) is never written; merge it in Linear`.
The sync writes only the one carrying the canonical external id (or, before the first
stamp, the Linear id recorded on the identity) and never writes, stubs or orphans the
other. Merge the duplicate into the survivor in Linear; there is nothing to change in
git. `check` fails only when neither customer carries the canonical id or the recorded
Linear id. In that case, set `sources.linear` on the identity in
`<data_dir>/identities.yaml` to the Linear id of the one to keep, or merge the customers
in Linear first. `check` also fails with
`two or more Linear customers carry the canonical id <id>` when the canonical id is on
more than one of them: merge them in Linear, or remove the id from all but one.

### Identity or ignore-list conflicts

These lines name the files and ids involved:

- A domain, name or source id on two identities, or a record or Linear customer that
  resolves to two identities: edit `<data_dir>/identities.yaml` so each value sits on one
  identity (merging two identities is described in
  [`../customer-registry.md`](../customer-registry.md#identity-resolution)).
- `desired record 'X' shares domain 'd.com' with ignored Linear customer 'Y'`: remove the
  ignore entry or change the record.
- An invalid `<data_dir>/linear-ignore.yaml` or `<data_dir>/datasets.yaml`: fix the listed
  field (a missing `reason`, a prefix shorter than four characters, a dataset name with
  no directory).

### OFFLINE lines

`OFFLINE: hand-edited generated file: <dir>/<file>: run <generator>` and
`OFFLINE: linear out of date: <external id>: run generate` need no Linear access. Run the
named command, or move a hand edit into the curated input behind the file, and commit
the result.

`OFFLINE:` lines also carry every error `check` finds without Linear that no generator
fixes: identity-table conflicts in `<data_dir>/identities.yaml`, an invalid
`<data_dir>/datasets.yaml` or `<data_dir>/linear-ignore.yaml` (see
[Identity or ignore-list conflicts](#identity-or-ignore-list-conflicts)) and unmapped
Linear statuses (see [An unmapped Linear status](#an-unmapped-linear-status)). Rerunning a
generator alone does not clear them: fix the named file first, as the linked section
says.

### Drift and stale retire entries

`DRIFT` lines mean someone edited Linear since the last `sync-inbound`: follow Runbook 2.
`STALE RETIRE` lines: Runbook 6.

## Runbook 6: Retiring a value

The sync removes a Linear value only when a reviewed entry in
`<data_dir>/linear-retire.yaml` names it. Removal takes two steps: a PR with the entry,
then the apply.

1. Find the customer's external id: the `external_ids[0]` value in
   `<data_dir>/linear/<file>.yaml`.
2. Add the entry with the exact current Linear value of each field to remove:

   ```yaml
   <external_id>:
     domains: [acme-robotics.example.test]
     revenue: 300000
     tier: Tier 2
   ```

   List only the fields to remove. A domain matches case-insensitively.
3. Run `check` (`generate` does not read the retire file). The plan marks the row
   `(retire)` and shows each removal (`domains: - acme-robotics.example.test`, or
   `revenue: 300000 -> None`); `check` exits 0 once nothing else is wrong. Open the PR
   and have a reviewer read the removals.
4. Merge. The workflow applies after approval (see "Applying").
5. **After the apply, delete the entry on the bot's state PR.** The value is no longer in
   Linear, so `check` reports the entry as a stale retire (exit 3) until it is gone. That
   includes the bot's state PR, which carries the post-apply mirror and fails `check`
   with `STALE RETIRE ... not live`. A separate follow-up PR off the default branch also
   fails: its base still has the pre-apply mirror, so `check` reports drift. Instead,
   push one commit that deletes the applied entry from `<data_dir>/linear-retire.yaml`
   onto the state PR's branch (`linear-state/<run-id>-<attempt>`), let `check` go green,
   and merge that PR. Merging it touches `<data_dir>/linear-retire.yaml`, so the
   workflow runs `deploy` again. That run plans a no-op apply; the reviewer may approve
   or reject it.

Four `STALE RETIRE` failures to expect, each printed as `STALE RETIRE: <id>: ...`:

- `... domains <x> not live` (or `revenue`/`tier ... not live`): the value is already
  absent, or the recorded value does not equal the live one. Delete the entry, or
  correct it to the live value.
- `... no registry row`: the external id matches no desired row in
  `<data_dir>/linear/`. Fix the id to the row's `external_ids[0]`, or delete the entry.
- `... no live match`: the registry row has no matching Linear customer (no match by
  source id, domain or name through `<data_dir>/identities.yaml`), so there is nothing to
  remove from. Delete the entry, or add the missing name, domain or Linear id to the
  customer's entry in `<data_dir>/identities.yaml`.
- `... conflicts with registry`: the registry (the Notion dataset or the identity table)
  still carries the value. For a domain, the same domain is in the registry or on the
  customer's entry in `<data_dir>/identities.yaml`. For revenue or tier, Notion sets any
  value for that field, even a different one, because the registry owns the field
  whenever it sets one. Apply would clear the value and the next run would set it again.
  Remove the value from its source (for a domain, delete it from the entry in
  `<data_dir>/identities.yaml`) before retiring it.

## Runbook 7: Restore

`restore` puts snapshot values back. Every apply writes a snapshot to
`<data_dir>/linear-snapshots/<UTC>.yaml`; the job summary prints the exact command.
`restore` uses check-and-set: it skips and reports any row edited in Linear since the
snapshot, so it never overwrites newer work.

1. Make sure the snapshot is on the default branch (the apply's state PR must have
   merged). Take its path from the job summary or `<data_dir>/linear-snapshots/`.
2. If the change you are undoing is still in the registry (a `<data_dir>/linear/`,
   `<data_dir>/identities.yaml` or retire change), order matters:
   - **Undoing a retire:** there is nothing to revert. Runbook 6 step 5 already deleted
     the applied entry on the state PR branch. Run `restore` (step 3). Its state PR
     passes `check` with an `UNTRACKED:` line: Linear holds the value again but the
     registry does not carry it. A restored domain is appended to the customer's entry
     in `<data_dir>/identities.yaml` by the next refresh (Runbook 5, or run
     `ingest --only linear-found` and `generate` on the state PR's branch). That merge
     touches `<data_dir>/identities.yaml`, so it starts a workflow run whose plan is a
     no-op; approve or reject it. A restored revenue or tier stays in Linear until the
     Notion page carries it.
   - **Undoing a domain add or a scalar change (revenue, tier, name, status):** run
     `restore` first (step 3) and merge its state PR, then revert the registry change in
     a PR. Reverting first fails twice over. A reverted registry that no longer carries a
     domain still in Linear does not remove it (apply never drops a Linear-only domain),
     and the next refresh appends the domain to the identity again. And merging a scalar
     revert triggers a deploy that redoes the change and bumps `updatedAt`, so `restore`
     then skips every row.

   Do not approve any deploy run that starts between the restore and the registry
   revert; it would re-apply the change.
3. Run the workflow: Actions, `registry-sync`, Run workflow, with

   | Input | Value |
   |---|---|
   | `mode` | `restore` |
   | `snapshot` | `<data_dir>/linear-snapshots/<UTC>.yaml`, relative to the repository root |
   | `rows` | optional: space-separated external ids to restore; empty restores every row in the snapshot |

   A reviewer approves it in the deploy environment. The job refreshes `linear-state/`
   and opens a state PR, as an apply does.
4. Read the exit code: 0 all rows restored; 2 a Linear error stopped the restore
   part-way; 3 some rows were skipped because Linear changed since the snapshot. A
   skipped row cannot be restored from that snapshot: `restore` skips any row whose
   `updated_at` differs from the snapshot's, and an edited row stays different, so
   running it again skips the row again. Set a skipped row's values by hand in Linear. A
   fresh snapshot (an apply writes one) is only a restore point for the next apply; it
   does not help the skipped row.

Break-glass, from a laptop (workflow unavailable only):

```bash
customer-registry-sync restore --from <data_dir>/linear-snapshots/<UTC>.yaml --prod --break-glass
customer-registry-sync restore --from <data_dir>/linear-snapshots/<UTC>.yaml --rows <external_id> --prod --break-glass
```

The first restores every row in the snapshot; the second restores one customer. Run
`sync-inbound` afterwards and commit the state mirror in a PR.

## Shared resolution route: "Linear drifted, pull it back"

1. `sync-inbound` makes `linear-state/` mirror live reality.
2. `generate` re-projects the desired rows from the (unchanged) datasets.
3. If the out-of-band change should be **kept**, reflect it in Notion or, for a domain, in
   `<data_dir>/identities.yaml`, so `generate` stops reverting it. If it should be
   **overwritten**, the plan re-proposes the registry value. A customer in
   `datasets/linear-found/` is Linear-owned: its edits are copied into the stub, never
   reverted.
4. Commit `linear-state/` and any `linear/` changes, open the PR, and let CI re-run.
