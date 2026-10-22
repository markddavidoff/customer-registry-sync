# Linear Customers API — notes

Reference for the sync engine. Verified against a live Linear workspace with an API
token (GraphQL, `https://api.linear.app/graphql`, `Authorization: <token>` header — no
`Bearer` prefix).

## Access
- **Write scope is required** for `deploy --apply`, which uses `customerCreate` and
  `customerUpdate`. A probe confirmed both, plus `customerDelete`, on a throwaway
  customer (created, updated, deleted, verified gone).
- `deploy` never deletes. `customerDelete` is used only by the live end-to-end tests to
  tear down their throwaway records.
- `customers(first: N)` can return a different row count than a UI or MCP listing
  (archived or scoping differences). The sync treats the GraphQL read as the source of
  truth for "live".

## Schema
- `customerCreate(input: CustomerCreateInput!) { success customer { id updatedAt revenue externalIds status{id name} tier{id name} } }`
- `customerUpdate(id: String!, input: CustomerUpdateInput!) { success customer { ... } }`
- `customerDelete(id: String!) { success }`  (live-test teardown only; also exists: customerUpsert, customerMerge)
- **Input fields** (Create adds `id`): `name:String`, `domains:[String]`, `externalIds:[String]`,
  `statusId:String`, `tierId:String`, `revenue:Int`, `ownerId`, `slackChannelId`, `size:Int`, `logoUrl`, `mainSourceId`.
  - `revenue` is **Int** (dollars), so generate must `int(round(arr))`.
  - `statusId` / `tierId` are **ids**, not names. The client derives name-to-id maps at
    runtime (`status_ids()`, `tier_ids()`); never hard-code them, because ids differ per
    workspace and a new status or tier would silently break a hard-coded map.
  - `externalIds` is a list. The sync carries the canonical slug here.

## Optimistic concurrency

`updatedAt` (ISO-8601, ms precision) **changes on every write** and is the version
token. Linear also bumps it on edits to fields the sync does not manage, so a moved
`updatedAt` alone is not treated as a conflict. `gitops-engine` applies two checks:

- **Plan time.** A customer aborts only when **both** its live `updatedAt` differs from
  the recorded `linear_synced.updated_at` **and** the `content_hash` of its deployable
  fields differs from the recorded hash. A bumped `updatedAt` with unchanged content is
  tolerated.
- **Write time.** Before each update the engine re-reads the customer and aborts that
  update if its `updatedAt` changed since planning or the customer is gone.

Linear has no server-side compare-and-swap, so the write-time check is client-side
read-then-write: a write can still land between the re-read and the update (small
window, accepted).

## Client interface
```
class LinearClient:
    list_customers() -> list[LiveCustomer]        # {id,name,domains,revenue,tier,status,external_ids,updated_at}
    status_ids() -> dict[str,str]                 # name -> id (from a read)
    tier_ids() -> dict[str,str]
    get_customer(id) -> LiveCustomer | None
    create_customer(name, domains, external_ids, status_id, tier_id, revenue) -> LiveCustomer
    update_customer(id, **fields) -> LiveCustomer # caller does the check-and-set before calling
    delete_customer(id) -> bool                   # live-test teardown only; deploy never deletes
```

## Rate limiting (HTTP 429)

`LinearClient._http_gql` retries HTTP 429, sleeping for the `Retry-After` header value when present (else the shared `0.5 * 2**attempt` backoff), within the existing 4-attempt bound; other 4xx raise immediately, 5xx keep the shared backoff. Live 429 behavior has **not been observed**; the retry path is covered by unit tests only.

## Live-API findings

Run against a throwaway `zzz-gitops-test-poc-*` record:

1. `customerCreate` accepts a domain under the reserved `.invalid` top-level domain.
2. A revenue-only `customerUpdate` changes both `content_hash` and `updatedAt`.
3. `customerDelete` hard-deletes: the id is absent from `customers` on the very next read.

The live e2e tests (`tests/test_e2e_gitops.py`, `pytest --run-live`, needs
`LINEAR_API_TOKEN`) therefore give each record a `<name>.invalid` domain, detect drift
by content hash, and tear down by deleting every record with the run's name prefix.
Unit-test fixtures use `example.test` domains instead; neither touches a real domain.
