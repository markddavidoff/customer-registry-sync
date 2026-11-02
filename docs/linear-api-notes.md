# Linear Customers API: notes

Reference for `src/customer_registry_sync/linear_client.py`: the parts of Linear's
GraphQL API the sync uses, and the behavior it relies on. How the sync uses them:
[`customer-registry.md`](customer-registry.md).

Endpoint `https://api.linear.app/graphql`, with the token in the `Authorization`
header and no `Bearer` prefix.

## Access

- **Write scope is required** for `deploy --apply` and `restore`, which use
  `customerCreate` and `customerUpdate`. A probe confirmed both, plus `customerDelete`,
  on a throwaway customer (created, updated, deleted, verified gone).
- `deploy` never deletes. `customerDelete` is used only by the live end-to-end tests to
  tear down their throwaway records.
- `customers(first: N)` can return a different row count than a UI or MCP listing
  (archived or scoping differences). The sync treats the GraphQL read as the source of
  truth for "live".

## Operations

- Read: `customers(first: 250, after: $cursor)`, paged until `hasNextPage` is false, and
  `customer(id:)` for the write-time re-read and for confirming a deletion. Fields read:
  `id name domains revenue externalIds updatedAt archivedAt status { id name } tier { id name }`.
- `customerStatuses { nodes { id name } }` and `customerTiers { nodes { id name } }`: the
  status and tier name-to-id maps, read at the start of every apply and restore. Reading
  them from the workspace, not from current customers, includes statuses no customer
  holds yet.
- `customerCreate(input: CustomerCreateInput!)` and
  `customerUpdate(id: String!, input: CustomerUpdateInput!)`, both returning
  `success customer { ... }` with the fields above. The sync builds its state row from
  the returned customer.
- `customerDelete(id: String!)` is a hard delete: the id is absent from `customers` on
  the next read. Only the live e2e tests' cleanup calls it, for `zzz-gitops-test-`
  records. `customerUpsert` and `customerMerge` also exist and are not used.

## Input fields

`name: String`, `domains: [String]`, `externalIds: [String]`, `statusId: String`,
`tierId: String`, `revenue: Int`. The API also takes `ownerId`, `slackChannelId`, `size`,
`logoUrl` and `mainSourceId`, which the sync never sends.

- `revenue` is an `Int` in dollars, so `generate` writes `int(round(arr))`.
- `statusId` and `tierId` are ids, not names; the client maps names through the two maps
  above. Ids differ per workspace, so never hard-code them.
- `externalIds` is a list. The sync carries the canonical id here.
- `customerCreate` accepts a domain ending in `.invalid`; the e2e tests use one.

## Behavior the sync relies on

- `updatedAt` (ISO-8601, millisecond precision) changes on every write, including a
  revenue-only update, so it serves as the version token for check-and-set. Linear has no
  server-side compare-and-swap; the client re-reads and compares before each update, so a
  write can still land between the re-read and the update (a small window, accepted).
- Linear also bumps `updatedAt` for edits to fields the sync does not manage, so a moved
  `updatedAt` alone is not a conflict, and drift detection compares a content hash of the
  managed fields instead. At plan time a customer aborts only when both its live
  `updatedAt` and its content hash differ from the recorded `linear_synced` values. At
  write time the apply re-reads the customer and aborts that update if its `updatedAt`
  changed since planning or the customer is gone.
- The client refuses to send a null or empty-list value unless the call passes
  `retire=True`; `create_customer` takes no `retire`.

## Errors and retries

The client retries HTTP 429, sleeping for `Retry-After` when present, and transient 5xx
and network errors with backoff `0.5 * 2**attempt`, up to 4 attempts. Other 4xx and
GraphQL `errors` raise `LinearError` at once. Live 429 behavior has not been observed;
the retry path is covered by unit tests only.

`get_customer` returns `None` only when Linear reports the customer gone: a null node,
an archived node, or a `LinearNotFound` (a `LinearError` that `_is_not_found` picks out
of the `errors` list). Every other error raises, so `sync-inbound` exits 2 and writes
nothing rather than record a deletion it did not confirm.

## Deleted and archived customers

Settled from Linear's published schema,
`https://raw.githubusercontent.com/linear/linear/master/packages/sdk/src/schema.graphql`,
fetched 2026-11-01 without a token (sha256 `99d9d750…62d1dce`), and the developer docs
at `https://linear.app/developers/graphql`. No live call was made.

- **(a) `type Customer` has `archivedAt: DateTime`**, "null if the entity has not been
  archived". It has no `trashed` field. `customerMerge` archives the source customer,
  so a merged-away customer is archived, not deleted.
- **(b) `customers(...)` takes `includeArchived: Boolean`**, "default: false". The docs
  say the same of every paginated query: archived resources are hidden unless
  `includeArchived: true` is passed. The sync passes nothing, so the listing omits
  archived customers, and `get_customer` returns `None` for a node with `archivedAt`
  set: the sync treats an archived customer as deleted.
- **(c) The not-found shape is not documented.** `customer(id: String!)` returns the
  non-null `Customer!`, so an unknown id cannot come back as a null node; it must come
  back as an entry in `errors` (with `data` null). Neither the schema, the GraphQL docs,
  the SDK error docs, nor the SDK's `LinearErrorType` enum (which has no not-found type)
  states that entry's `message` or `extensions`. Until it is observed, `_is_not_found`
  matches no error, so a hard-deleted customer (`customerDelete`) makes `sync-inbound`
  exit 2. The owner-run live test `test_get_after_delete_is_not_found` creates and
  deletes a `zzz-gitops-test-` customer and asserts `get_customer` returns `None`; when
  it fails, its error text gives the shape to match.

## Live-API findings

Run against a throwaway `zzz-gitops-test-poc-*` record:

1. `customerCreate` accepts a domain under the reserved `.invalid` top-level domain.
2. A revenue-only `customerUpdate` changes both the content hash and `updatedAt`.
3. `customerDelete` hard-deletes: the id is absent from `customers` on the very next read.

The live e2e tests (`tests/test_e2e_gitops.py`, `pytest --run-live`, needs
`LINEAR_API_TOKEN`) therefore give each record a `<name>.invalid` domain, detect drift by
content hash, and tear down by deleting every record with the run's name prefix.
Unit-test fixtures use `example.test` domains instead; neither touches a real domain.
