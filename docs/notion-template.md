# Notion page template

`customer-registry-sync ingest` reads customer pages that a script generates from one
template. The parser (`src/customer_registry_sync/notion_ingest.py`) does a
deterministic block walk, with no LLM, so pages must follow the layout below. This
document lists exactly what the parser reads. Anything else on the page is ignored.

## Page tree

```
hub page                      hub_page_id in the config (kept for reference)
├── Customers                 bucket page  -> classification "real"
│   ├── <customer page>       child page, one per customer
│   └── ...
├── Churned Customers         bucket page  -> "churned"
├── Archive                   bucket page  -> "internal"
├── POCs                      bucket page  -> "prospect"
├── Prospects                 bucket page  -> "prospect"
└── Partner & Supporting Accounts
                              bucket page  -> "partner"
```

- The config's `buckets` map names each bucket page's id. All six names are required.
  The **bucket decides the classification**; a page's own Status line never does.
- The six bucket names and their classifications are this template's fixed values
  (`BUCKET_CLASSIFICATION` in `src/customer_registry_sync/notion_ingest.py`). The config
  loader rejects any other bucket name (`src/customer_registry_sync/config.py`), so name
  your Notion pages to match; the config cannot rename or add buckets.
- The ingest lists the **direct children** of each bucket page (100 per request,
  following pagination). It does not recurse into sub-pages other than the customer
  pages themselves.
- Share the bucket pages (and their sub-pages) with the Notion integration whose token
  you export as `NOTION_API_TOKEN`.

## Bucket page children

| Child block | Handling |
|---|---|
| `child_page` | A customer page. The page **title is the customer name**. |
| `table` (only in `Partner & Supporting Accounts`) | Parsed as a partner table; see below. |
| anything else | Ignored. |

A `child_page` is skipped when its title is:

- empty or whitespace only;
- exactly `Archive` or `Churned Customers` (case-insensitive), because nested bucket
  pages are listed through their own bucket entry;
- containing any of these case-insensitive markers, which flag reference pages rather
  than customers: `methodology`, `health report`, `start here`,
  `engagement summaries`, `document structure`, `onboarding guide`.

The marker list is `NON_CUSTOMER_TITLE_MARKERS` in `notion_ingest.py`. Customize it for
the reference-page titles in your own workspace.

### Partner table

A `table` block that is a direct child of the `Partner & Supporting Accounts` page. Its
children are `table_row` blocks; each row's `table_row.cells` is a list of cells, and
each cell is a list of rich-text segments (joined with a space).

| Column | Meaning |
|---|---|
| 1 `Account` | Customer name (rows with an empty first cell are skipped) |
| 2 `Contact` | Contact name; `""`, `—` and `-` become null |
| 3 `Reason Kept Active` | Stored as extra `reason_kept_active` |
| 4 `Flagged By` | Stored as extra `flagged_by` |
| 5 `Status` | Status text; empty becomes null |

The first row is dropped when its first cell reads `Account` (case-insensitive).

## Customer page blocks

The ingest reads the page's top-level blocks in order. Nested children are not read.

| Block type | Role |
|---|---|
| `heading_1`, `heading_2`, `heading_3` | Section break. A heading whose text contains `contact` (case-insensitive) starts a **contacts section**; any other heading ends it. |
| `bulleted_list_item`, `numbered_list_item`, `paragraph` | Read for fields and contacts. Text is the concatenated `plain_text` of the block's `rich_text` segments, stripped. Empty blocks are skipped. |
| `callout`, `table`, `divider`, `child_page`, anything else | Ignored. |

Each text block carries a `rich_text` array. Segments may be bold, italic or linked;
only `plain_text` matters.

### Fields: `Label: value`

A text block whose text contains `": "` is split at the **first** `": "` into a label
and a value. It becomes a field when the label has **at most three words**. The map is
**first-wins**: when a label repeats on the page, the first occurrence is kept, so put
the authoritative line before any later line that reuses the label (for example a
`Status:` line inside a margin-analysis section).

| Label | Output field | Normalization |
|---|---|---|
| `Domain` | `domains` | Split on commas, whitespace and semicolons; lowercased; kept when it contains a `.` |
| `ARR` | `arr` | First money amount in the value; `K`, `M`, `B` suffixes multiply by 1e3, 1e6, 1e9 (`$250,000.00/yr` gives 250000.0). Only the `ARR` label counts. |
| `Deployment` | `deployment` | Contains `self-managed`, `on-prem` or `air-gap` gives on-prem; contains `saas` gives saas; both, or `hybrid`, gives hybrid; otherwise null |
| `Tier` | `tier` | Text before the first `\|`, trimmed; `None`, `N/A` or empty gives null. The tier names (such as `Tier 1`) are the template's own values, copied as written; each one must match a customer tier name in your Linear workspace, or `deploy --apply` refuses before any write |
| `Status` | `status` | Raw value |
| `HubSpot ID` | `hubspot_id` | First run of four or more digits in the value |
| `Annual Revenue`, `Industry`, `Slack Channel`, `Parent Company` | `extra` | Raw value. `Annual Revenue` is the company's revenue and is never used as `arr`. |

Unknown or absent labels leave the output field null. The parser never invents values.

Labels that name a CRM, such as `HubSpot ID` (and the `[CRM — last synced ...]` line in
the example below), come from pages populated by a CRM sync. They are only field labels:
the tool never talks to the CRM, and every CRM-labelled field is optional.

### Contact

The contact is the first usable line inside a contacts section (see headings above):

1. Lines starting with `[`, or starting with `last synced`, `source:`, `maintained by`
   or `note:` (case-insensitive), are metadata and skipped.
2. If the line contains an email address, the contact is that address.
3. Otherwise the contact is the text before the first ` -` or ` (`, for example
   `Robin (Robin A) - Technical lead` gives `Robin`.

## Minimal customer page

```
heading_2   Company Overview
bulleted    Domain: acmerobotics.example.test
bulleted    Industry: Industrial Automation
bulleted    Annual Revenue: ~$50M
bulleted    HubSpot ID: 48201937465
heading_2   Account Relationship
bulleted    ARR: $250,000.00/yr
bulleted    Deployment: Self-Managed (primary) + SaaS components
bulleted    Tier: Tier 1 | Revenue: $150,000
bulleted    Status: Active customer
heading_2   Key Contacts
paragraph   [CRM — last synced 2026-05-08]
bulleted    Robin - Primary technical lead (robin@acmerobotics.example.test)
```

This page parses to: domains `["acmerobotics.example.test"]`, arr 250000.0, deployment
`hybrid`, tier `Tier 1`, status `Active customer`, hubspot_id `48201937465`, contact
`robin@acmerobotics.example.test`.

## What `ingest` writes: the dataset record

`ingest` merges the pages into one record per company and writes it to
`<data_dir>/datasets/notion/<slug>.yaml`. The slug is the first label of the record's
first domain (`acme-robotics.example.test` gives `acme-robotics`), else a slug of the
name, with `-2`, `-3` appended on a clash (`slug_for` in
`src/customer_registry_sync/writers.py`). The file is a `CustomerRecord`
(`src/customer_registry_sync/schema.py`). A sample that loads is
[`examples/dataset-record.example.yaml`](../examples/dataset-record.example.yaml). The files are generated; do not edit them
by hand.

| Field | Type | Where it comes from |
|---|---|---|
| `canonical_name` | string, **required** | The page title (the `Account` cell for a partner-table row) |
| `domains` | list of strings, default empty | `Domain` label, unioned across the pages of one company |
| `buckets` | list of strings, default empty | The bucket page each source page sat under |
| `classification` | `real`, `prospect`, `churned`, `partner`, `internal` or null | The bucket (highest rank when a company sits in several), then `classification-overrides.yaml` |
| `deployment` | `saas`, `on-prem`, `hybrid` or null | `Deployment` label; a curated `self-managed-customers.yaml` entry wins |
| `arr` | number or null | `ARR` label only |
| `tier` | string or null | `Tier` label, text before the first `\|` |
| `status` | string or null | `Status` label, raw |
| `owner` | string or null | Optional. No page label fills it today, so `ingest` leaves it null |
| `contact` | string or null | First usable line in the contacts section |
| `prod_org_id` | string or null | Optional. No page label fills it today, so `ingest` leaves it null; the schema, the merge and `registry.csv` carry it if a source sets it |
| `hubspot_id` | string or null | Optional. `HubSpot ID` label: the first run of four or more digits |
| `linear_customer_id` | string or null | Null in the `notion` dataset; set on `linear-found` stubs |
| `sources` | list of strings, default empty | `notion:<bucket>` for each page, plus `self-managed-customers.yaml` when a curated entry contributed |
| `notes` | string or null | The `notes` of a curated `self-managed-customers.yaml` entry, plus an `override: classification=...` line when `classification-overrides.yaml` applied; joined with `; ` |

Every key except `canonical_name` may be omitted. The loader rejects an unknown key, a
`deployment` outside the three values, and a `classification` outside the five
(`CustomerRecord.from_dict` and `__post_init__`). `hubspot_id` and `prod_org_id` are
identifiers a CRM sync might put on a page; the tool never calls the CRM and reads no
field of it except the page's own `HubSpot ID` line.

## Test fixtures

The fixtures under `tests/fixtures/` follow the block shape the Notion API returns from
`blocks.children.list` (`object`, `id`, `parent`, timestamps, `created_by`,
`last_edited_by`, `has_children`, `in_trash`, `type`, the type's node, `archived`).
`tests/fixtures/generate_fixtures.py` regenerates them deterministically from invented
data, and a test fails if the committed files drift from the generator.
