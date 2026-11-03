# Operate the registry sync

This file covers day-to-day operation and recovery. First-time setup is in
[`setup.md`](setup.md). How the registry and the sync work is in
[`../customer-registry.md`](../customer-registry.md).

`<data_dir>` is the `data_dir` in your `registry-sync.yaml` (default `customers`). Commands
run from the repository that holds the registry as `customer-registry-sync <command>`
(or `uv run customer-registry-sync <command>`), with `--config <path>` before the command
when the config is not `registry-sync.yaml` in the working directory. Secrets come from
the environment: `LINEAR_API_TOKEN`, and `NOTION_API_TOKEN` for `ingest`. `check` and
`deploy` need `LINEAR_API_TOKEN` even for a dry run, and exit 2 without it. `config-get
data_dir` prints the absolute path of `<data_dir>`.

Notion ingest is manual: run `ingest` on your machine, run `generate`, and open a PR. The
workflow never reads Notion. `hide` and `ingest --only linear-found` read the state
mirror, so run `sync-inbound` before either.

- `check`: drift, plan, pending Linear edits, orphans (a Linear customer in no dataset),
  identity and hidden-customer conflicts, overrides-file errors, hand-edited or
  out-of-date generated files. Exit 0 ok, 2 error, 3 any of those found. Known
  duplicates and redundant pins are reported without failing.
- `sync-inbound`: refresh `<data_dir>/linear-state/` from live Linear, and record in
  `<data_dir>/linear-overrides.yaml` the Linear edits and confirmed deletions since the
  last mirror. Exit 2 on a Linear read error, with nothing written.
- `refresh`: `sync-inbound`, `ingest --only linear-found` and `generate`, in that order.
  The workflow's refresh job runs it.
- `ingest`: build `<data_dir>/datasets/notion/` from Notion (needs `NOTION_API_TOKEN`),
  then `<data_dir>/datasets/linear-found/` from the mirror; appends to
  `<data_dir>/identities.yaml`. `ingest --only linear-found` skips Notion and reads only
  the mirror.
- `generate`: merge the datasets through `<data_dir>/identities.yaml`, then write
  `<data_dir>/linear/`, `<data_dir>/registry.csv` and `<data_dir>/real-customers.csv`.
- `hide --linear-id <id> --reason "<why>"`: hide a Linear customer by adding a `hidden`
  entry to `<data_dir>/linear-overrides.yaml` (offline; exit 0 hidden, 4 already hidden,
  2 refused).
- `adopt-live`: one-time migration of a v0.2.0 data repository (see Runbook 8).
- `deploy`: dry-run plan with field-level changes. Applies run from the workflow, not a
  laptop (see "Applying").
- `restore --from <snapshot>`: put snapshot values back (see Runbook 7).

`ingest`, `generate`, `sync-inbound`, `deploy` and `restore` accept `--no-input` (never
prompt). `ingest`, `generate` and `sync-inbound` write generated files and refuse to
overwrite one that was edited by hand: they prompt in a terminal and exit 2 with
`--no-input` or without a terminal. The curated and generated files are listed in
[`../customer-registry.md`](../customer-registry.md#files-in-data_dir).

`<data_dir>/linear-overrides.yaml` is keyed by the customer's canonical id: the
`external_ids[0]` value in `<data_dir>/linear/<file>.yaml`. That id is also the
customer's key in `<data_dir>/identities.yaml`. A data repository that still has
`linear-ignore.yaml` or `linear-retire.yaml` exits 2 on `check`, `deploy`, `ingest`,
`sync-inbound`, `refresh` and `hide` until `adopt-live` has run (Runbook 8).

## Applying

Applies run from the `registry-sync` GitHub Actions workflow, whose deploy environment
requires a reviewer's approval before anything touches Linear.

1. A push to the default branch that changes a deploy input starts the workflow: any file
   under `<data_dir>/linear/`, `<data_dir>/identities.yaml`,
   `<data_dir>/linear-overrides.yaml`, `<data_dir>/datasets.yaml`, or
   `registry-sync.yaml`. To start it by hand:
   Actions, `registry-sync`, Run workflow, `mode: deploy`.
2. The reviewer reads the field-level plan in the job log and approves.
3. The job runs `check`, then `deploy --apply --prod --no-input`. Before the first write
   it saves a snapshot to `<data_dir>/linear-snapshots/<UTC>.yaml`; after each write it
   verifies the row Linear returned and aborts the remaining rows on any unplanned
   change.
4. The job refreshes `<data_dir>/linear-state/` and opens a state PR (branch
   `sync-state/<run-id>-<attempt>`) with the new mirror and the snapshot. Merge it, and
   merge it before any refresh PR (see "Merge order" below). The job summary carries the
   apply report and the exact `restore` command.

A failed apply still opens the state PR, and the run is marked failed.

**Merge order.** Merge a deploy's state PR before the refresh PR. The refresh compares
the mirror on the default branch with live Linear. If the registry changed again after
the deploy and the state PR is still open, the mirror, Linear and the desired row all
differ, and `sync-inbound` records a conservative `pending` entry holding Linear's value
(the deploy's own write). That entry is spurious. Delete it by hand: the registry value
wins. Merging the state PR first avoids it.

The state PR stages all of `<data_dir>` (and the state and snapshot directories, if they
lie elsewhere). The `sync-inbound` that the deploy job runs after the apply can also
record an edit or a deletion in `<data_dir>/linear-overrides.yaml`. When it does, it
regenerates `<data_dir>/linear/` and the CSVs from the committed datasets, and all of it
lands in the state PR. Merging a PR that changes the overrides file starts a deploy run
(the file is a deploy input). Its plan does not write back an absorbed clear or removal
and does not recreate a deleted customer, because the regenerated `linear/` already
agrees with Linear. A pending entry aborts its row, so resolve it (see
[A pending Linear edit](#a-pending-linear-edit)) before approving. If `deploy` prints `linear out of date: <id>: run generate`
and `REFUSED`, the `linear/` on the branch lags the overrides file: run `generate`,
commit the result, and re-run.

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
3. **Resolution:** `sync-inbound` refreshes `linear-state/` and records the edit in
   `linear-overrides.yaml` (an accepted pin or removal, or a pending entry to resolve, see
   [A pending Linear edit](#a-pending-linear-edit)); `generate` re-projects the desired
   rows; resolve conflicts in the PR; push. CI re-runs and goes green.

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
check FAILED: see the ERROR, DRIFT, PENDING, OVERRIDE and OFFLINE lines above
```

Find the lines above it and use the matching section. `NOTE:` and `HIDDEN:` lines do
not fail `check`.

### An orphan: a Linear customer in no dataset

The failure line names the customer and both ways out. Check the exact wording in your
`check` output:

```
ERROR: orphan: Linear customer 'Example Corp' (<linear id>) is in no dataset.
  Add it: dispatch the registry-sync workflow with mode=refresh (or run sync-inbound, ingest --only linear-found, generate) and merge the PR.
  Or hide it: customer-registry-sync hide --linear-id <linear id> --reason "<why>"
```

**Add it to the registry.** Dispatch the workflow: Actions, `registry-sync`, Run
workflow, `mode: refresh`. (The same mode runs daily from the schedule.) It runs in the
read-only environment, so it waits for no approval and never writes to Linear. It runs
`sync-inbound`, `ingest --only linear-found` and `generate`, and opens a PR from branch
`sync-refresh/<run-id>-<attempt>` with the new mirror, the new stubs, the appended
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

**Or hide it.** For a customer the sync must never manage (your own company, a test
record), run the command from the failure line with a real reason:

```bash
customer-registry-sync hide --linear-id <linear id> --reason "<why>"
```

It resolves the customer to its canonical id through the identity table and appends a
new identity when none matches. It reads the name from the mirror, so run `sync-inbound`
first if it says the id is not in the mirror. Exit 4 means the customer is already hidden
and its reason is kept. Commit `<data_dir>/linear-overrides.yaml` (and
`<data_dir>/identities.yaml`, if `hide` appended an identity) in a PR. Run
`ingest --only linear-found` and `generate` as well to drop an existing stub for that
customer at once; the next refresh would drop it anyway. To hide every customer whose
name starts with a prefix, add an item to `hidden_name_prefixes` in the same file.

### An unmapped Linear status

The line reads `OFFLINE: unmapped Linear status: '<name>' (<linear id>) has status '<status>'`.
Either add a row for the status to `linear_status_classification` in
`<data_dir>/datasets.yaml` (for example `POC: prospect`), then run
`ingest --only linear-found` and `generate`; or hide the customer as above. A customer
with no status in Linear shows `has status ''`: set its status in Linear, then dispatch
the refresh (or run `sync-inbound`, `ingest --only linear-found` and `generate`), or
hide it.

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

### Identity conflicts

These lines name the files and ids involved:

- A domain, name or source id on two identities, or a record or Linear customer that
  resolves to two identities: edit `<data_dir>/identities.yaml` so each value sits on one
  identity (merging two identities is described in
  [`../customer-registry.md`](../customer-registry.md#identity-resolution)).
- An invalid `<data_dir>/datasets.yaml`: fix the listed field (a dataset name with no
  directory).

### A pending Linear edit

The line names the customer, the field and Linear's value, then both ways out:

```
PENDING: acme-robotics: revenue = 150000 (edited in Linear)
  keep Linear's value: move it to customers.acme-robotics.pin.revenue
  upstream wins: delete customers.acme-robotics.pending.revenue; the next deploy writes the registry value
```

`sync-inbound` wrote the entry into `<data_dir>/linear-overrides.yaml` after someone
edited that field in Linear. `check` fails, and `deploy` aborts that customer's row, until
the entry is gone. Decide in a PR:

1. **Keep Linear's value.** Under the customer's key, move the value from `pending` into
   `pin` (for `domains_add`, into `domains.add`). Run `generate`, so `linear/` holds the
   value, and commit both files.
2. **Registry wins.** Delete the value from `pending`. The next deploy overwrites Linear.
   Run no `generate`; the desired row is unchanged.

Delete a `pending:` key that has no values left. The refresh drops a pending entry on its
own when Linear's value comes to equal the registry's. To record the decision for a later
reader, add a `reason:` under the customer.

### A deleted customer, and the unverified not-found shape

`sync-inbound` records `deleted: {linear_id, confirmed}` for a customer that is missing
from Linear's listing and that a direct read by id reports not found. An archived
customer (`Customer.archivedAt` set) counts as deleted. The sync then never recreates
it. To bring it back, delete the `deleted` entry and merge: the next deploy creates the
customer with a new Linear id.

Linear's not-found response is not verified. Linear's schema returns a non-null
`Customer!` from `customer(id:)`, so an unknown id should come back as an error, and the
client's not-found test is written from the public docs. The owner-run live test
`test_get_after_delete_is_not_found` (in `tests/test_e2e_gitops.py`; run it with
`--run-live` against a throwaway workspace) creates and deletes a throwaway customer and
asserts the client returns `None`. Until the owner has run it, **a hard-deleted customer
(deleted with `customerDelete`, not archived) makes `sync-inbound` and `refresh` exit 2**,
with an error naming the failed read, and nothing is written. Archive the customer
instead, or run the live test; if it fails, its error text gives the shape the client must
match. The refresh job stays red until one of those happens.

### Hidden conflicts and overrides errors

- `desired record 'X' shares domain 'd.com' with hidden Linear customer 'Y'` (or a hidden
  prefix): remove the `hidden` entry or prefix, or change the record.
- `OVERRIDE:` lines name `linear-overrides.yaml` and a line. The common ones: an unknown
  key; a `hidden` entry with no reason; a `domains.remove` for a domain upstream does not
  carry; a `pin` or `pending` on a Linear-owned stub (stubs accept only `hidden`);
  `pin: null` on `name`, `status` or `external_ids`; a pin of `external_ids`; a
  `revenue` that is not an integer; an entry for a canonical id with no identity. Fix the
  named line.
- A pin equal to the upstream value is reported as `NOTE: ... (redundant)` and does not
  fail. Delete it when you tidy the file.

### OFFLINE lines

`OFFLINE: hand-edited generated file: <dir>/<file>: run <generator>` and
`OFFLINE: linear out of date: <external id>: run generate` need no Linear access. Run the
named command, or move a hand edit into the curated input behind the file, and commit
the result.

`OFFLINE:` lines also carry every error `check` finds without Linear that no generator
fixes: identity-table conflicts in `<data_dir>/identities.yaml`, an invalid
`<data_dir>/datasets.yaml` (see [Identity conflicts](#identity-conflicts)) and unmapped
Linear statuses (see [An unmapped Linear status](#an-unmapped-linear-status)). Rerunning a
generator alone does not clear them: fix the named file first, as the linked section
says.

### Drift

`DRIFT` lines mean someone edited Linear since the last `sync-inbound`: follow Runbook 2.
The refresh then records the edit as an accepted override or a pending entry (see
[A pending Linear edit](#a-pending-linear-edit)).

## Runbook 6: Removing or clearing a value

The registry owns every written field, so the sync removes a Linear domain or clears a
revenue or tier whenever the desired row lacks it. No separate acknowledgement exists.
The removal is the visible diff of the pull request. There are two ways to make the
desired row lack a value:

1. **Drop it upstream.** Remove the domain, ARR or tier from the Notion page, run
   `ingest`, then `generate`. A domain that sits on the customer's entry in
   `<data_dir>/identities.yaml` is merged back into every desired row, so use the next
   way for it.
2. **Override it.** Add an entry under the customer's canonical id in
   `<data_dir>/linear-overrides.yaml`:

   ```yaml
   customers:
     acme-robotics:
       domains:
         remove: [old.acme-robotics.example.test]   # must be a domain upstream carries
       pin:
         tier: null        # clear the tier whatever Notion says
         revenue: null     # likewise (optional fields only)
   ```

   A domain compares case-insensitively. Run `generate`.

Then run `check`. The plan shows each removal (`domains: - old.acme-robotics.example.test`,
or `revenue: 300000 -> None`) and exits 0 once nothing else is wrong. Open the PR and
have a reviewer read the removals. After the merge the workflow applies behind its
approval (see "Applying").

Two follow-ups keep `check` green later:

- A `domains.remove` must name a domain upstream carries. If upstream later drops the
  domain, `check` fails with `domains.remove <domain>: upstream does not carry it`. Delete
  the `remove` entry then.
- A `pin` outlasts upstream. If Notion later gains a revenue, `pin: {revenue: null}` still
  wins until someone deletes the pin. When upstream comes to equal a pin, `check` reports
  the pin as redundant (a `NOTE`, no failure).

## Runbook 7: Restore

`restore` puts snapshot values back. Every apply writes a snapshot to
`<data_dir>/linear-snapshots/<UTC>.yaml`; the job summary prints the exact command.
`restore` uses check-and-set: it skips and reports any row edited in Linear since the
snapshot, so it never overwrites newer work.

1. Make sure the snapshot is on the default branch (the apply's state PR must have
   merged). Take its path from the job summary or `<data_dir>/linear-snapshots/`.
2. If the change you are undoing is still in the registry (a `<data_dir>/linear/`,
   `<data_dir>/identities.yaml` or overrides change), order matters. Run `restore` first
   (step 3) and merge its state PR, then revert the registry change in a PR. Reverting
   first fails: merging a revert triggers a deploy that redoes the change and bumps
   `updatedAt`, so `restore` then skips every row. After a restore, Linear holds values
   the registry no longer wants, so the `sync-inbound` the restore job runs records them
   as Linear edits in the restore's state PR: a cleared value or removed domain is
   absorbed as a pin or `domains.remove` (with `linear/` regenerated to match), and
   anything else becomes a pending entry. Reverting the registry change makes the
   registry agree with Linear again; in the revert PR, delete any pin, removal or pending
   entry the restore's state PR recorded for the same value, then run `generate`.

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

## Runbook 8: Move a v0.2.0 data repository to full ownership

Run this once, from a clone of the data repository, on a branch. It needs no token.

1. Apply and remove any `<data_dir>/linear-retire.yaml` entries under v0.2.0, then empty
   the file. `adopt-live` refuses while the file has entries.
2. Make sure `<data_dir>/linear-state/` is current and merged (a merged refresh PR or
   state PR). `adopt-live` reads the committed mirror and refuses without one.
3. Preview, then run it:

   ```bash
   customer-registry-sync adopt-live --dry-run
   customer-registry-sync adopt-live
   ```

   It prints `adopt-live: N pins, M domain adds, H hidden, P hidden prefixes, I identities
   added`. It pins the live revenue and tier the registry lacks, adds the live domains the
   desired row lacks, turns `linear-ignore.yaml` into `hidden` entries and prefixes,
   appends an identity for any ignored customer that has none, and deletes the ignore and
   retire files.
4. Run `generate`, then `check` with a read-only token. The plan should show no clears and
   no removals. Commit `<data_dir>/linear-overrides.yaml`, `<data_dir>/identities.yaml`,
   the deleted files and `<data_dir>/linear/` in one PR, and have a reviewer read the
   pins.
5. Merge the PR. The workflow's deploy plan should be a no-op that the reviewer can
   approve.

## Shared resolution route: "Linear drifted, pull it back"

1. `sync-inbound` makes `linear-state/` mirror live reality and records each edit in
   `<data_dir>/linear-overrides.yaml`: a cleared value or removed domain as an accepted
   pin or `domains.remove`, anything else as a pending entry.
2. `generate` re-projects the desired rows from the datasets and the overrides.
3. If the out-of-band change should be **kept**, move its pending value into `pin` (or
   `domains.add`), or fix the source in Notion. If it should be **overwritten**, delete
   the pending value, and the plan re-proposes the registry value. A customer in
   `datasets/linear-found/` is Linear-owned: its edits are copied into the stub, never
   reverted.
4. Commit `linear-state/`, `linear-overrides.yaml` and any `linear/` changes, open the PR,
   and let CI re-run.
