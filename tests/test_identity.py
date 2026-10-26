from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from customer_registry_sync.generate import generate_linear
from customer_registry_sync.identity import (
    IDENTITIES_FILE,
    IdentityTable,
    conflict,
    keys_for_live,
    keys_for_record,
    keys_for_row,
    load_identities,
    norm_name,
    resolve,
    resolve_all,
    table_errors,
    with_implicit,
)
from customer_registry_sync.identity_store import IdentityStore
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_schema import LinearSynced
from customer_registry_sync.schema import CustomerRecord


def ds(**kw) -> CustomerRecord:
    base = {
        "canonical_name": "Acmeco",
        "domains": ["acmeco.example.test"],
        "classification": "real",
        "arr": 250000.0,
        "tier": "Tier 1",
    }
    base.update(kw)
    return CustomerRecord(**base)


def node(**kw) -> dict:
    base = {
        "id": "lin-1",
        "name": "Acmeco",
        "domains": ["acmeco.example.test"],
        "revenue": 300000,
        "externalIds": [],
        "updatedAt": "2026-10-07T00:00:00.000Z",
        "status": {"id": "s", "name": "Active"},
        "tier": None,
    }
    base.update(kw)
    return base


def table(tmp_path: Path, **identities: dict) -> IdentityTable:
    """A table built the way ingest builds one: through `IdentityStore.add_identity`."""
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, spec in identities.items():
        got = store.add_identity(
            cid,
            names=spec.get("names", []),
            domains=spec.get("domains", []),
            sources=spec.get("sources", {}),
        )
        assert got == cid
    return store.table


def table_from_file(tmp_path: Path, text: str) -> IdentityTable:
    """Hand-written file text, for the shapes the store never writes."""
    p = tmp_path / IDENTITIES_FILE
    p.write_text(textwrap.dedent(text))
    return IdentityStore(p).table


def test_missing_file_is_an_empty_table(tmp_path):
    assert load_identities(tmp_path / IDENTITIES_FILE).entries == {}


def test_unknown_field_raises_naming_the_key(tmp_path):
    with pytest.raises(ValueError, match="aliases"):
        table_from_file(
            tmp_path,
            """
            acme:
              names: [Acme]
              aliases: [ACME]
        """,
        )


def test_file_order_is_kept(tmp_path):
    t = table(tmp_path, zeta={"names": ["Zeta"]}, alpha={"names": ["Alpha"]})
    assert list(t.entries) == ["zeta", "alpha"]


def test_norm_name_strips_suffix_then_case_and_punctuation():
    assert norm_name("Hooli Technologies, Inc. [Active]") == "hoolitechnologiesinc"


def test_resolves_by_canonical_id(tmp_path):
    t = table(tmp_path, acmeco={"names": ["Something Else"]})
    [row] = generate_linear([ds(domains=[])])  # external id = slug "acmeco"
    hit = resolve(keys_for_row(row), t)
    assert hit is not None and (hit.canonical_id, hit.key, hit.value) == (
        "acmeco",
        "source id",
        "acmeco",
    )


def test_resolves_by_notion_slug(tmp_path):
    t = table(tmp_path, air={"sources": {"notion": "acmeco-2"}})
    item = keys_for_record(
        ds(domains=[], canonical_name="Other"), dataset="notion", source_id="acmeco-2"
    )
    hit = resolve(item, t)
    assert hit is not None and (hit.canonical_id, hit.key, hit.value) == (
        "air",
        "source id",
        "acmeco-2",
    )


def test_resolves_by_linear_id(tmp_path):
    t = table(tmp_path, air={"sources": {"linear": "lin-1"}})
    lv = parse_customer_node(node(name="Renamed", domains=[]))
    hit = resolve(keys_for_live(lv), t)
    assert hit is not None and (hit.canonical_id, hit.key, hit.value) == (
        "air",
        "source id",
        "lin-1",
    )
    # a linear-found record resolves by its recorded Linear id too
    rec = ds(canonical_name="Renamed", domains=[], linear_customer_id="lin-1")
    hit = resolve(keys_for_record(rec, dataset="linear-found", source_id="renamed"), t)
    assert hit is not None and hit.canonical_id == "air" and hit.key == "source id"


def test_desired_row_linear_id_comes_from_linear_synced():
    [row] = generate_linear([ds()])
    assert keys_for_row(row).linear_id is None
    row.linear_synced = LinearSynced(linear_id="lin-9", updated_at="t", content_hash="h")
    assert keys_for_row(row).linear_id == "lin-9"


def test_domain_match_ignores_case(tmp_path):
    t = table(tmp_path, acme={"domains": ["acme.com"]})
    lv = parse_customer_node(node(name="Acme Corp", domains=["Acme.COM"]))
    hit = resolve(keys_for_live(lv), t)
    assert hit is not None and hit.canonical_id == "acme" and hit.key == "domain"


def test_name_match_strips_status_suffix_and_punctuation(tmp_path):
    t = table(tmp_path, hooli={"names": ["Hooli Technologies", "Hooli"]})
    lv = parse_customer_node(node(name="Hooli [Active]", domains=[]))
    hit = resolve(keys_for_live(lv), t)
    assert hit is not None and hit.canonical_id == "hooli" and hit.key == "name"
    lv2 = parse_customer_node(node(id="lin-2", name="hooli-technologies", domains=[]))
    assert resolve(keys_for_live(lv2), t).canonical_id == "hooli"


FIRST_KEY = {"a": {"names": ["Alpha"]}, "b": {"domains": ["acmeco.example.test"]}}


def test_first_key_decides(tmp_path):
    t = table(tmp_path, **FIRST_KEY)
    lv = parse_customer_node(node(externalIds=["a"], domains=["acmeco.example.test"]))
    assert resolve(keys_for_live(lv), t).canonical_id == "a"


def test_conflict_names_both_ids_and_keys(tmp_path):
    t = table(tmp_path, **FIRST_KEY)
    lv = parse_customer_node(node(externalIds=["a"], domains=["acmeco.example.test"]))
    hits = resolve_all(keys_for_live(lv), t)
    assert [(h.canonical_id, h.key) for h in hits] == [("a", "source id"), ("b", "domain")]
    msg = conflict(keys_for_live(lv), t)
    assert msg == (
        "Acmeco resolves to two identities: a (source id 'a') and b (domain 'acmeco.example.test')"
    )


def test_no_conflict_when_every_key_agrees(tmp_path):
    t = table(tmp_path, a={"names": ["Acmeco"], "domains": ["acmeco.example.test"]})
    lv = parse_customer_node(node(externalIds=["a"]))
    assert conflict(keys_for_live(lv), t) is None


def test_unlisted_domain_and_spelling_resolve_to_none(tmp_path):
    t = table(tmp_path, hooli={"names": ["Hooli Technologies"], "domains": ["hooli.example.test"]})
    lv = parse_customer_node(node(name="Hooli Technolgies", domains=["hooli.example.org"]))
    assert resolve(keys_for_live(lv), t) is None
    assert resolve_all(keys_for_live(lv), t) == []
    assert conflict(keys_for_live(lv), t) is None


def test_table_errors_duplicate_domain_name_and_source(tmp_path):
    # the store refuses nothing at add time; `table_errors` is what flags the clash
    t = table(
        tmp_path,
        a={
            "names": ["Acme Inc"],
            "domains": ["acme.com"],
            "sources": {"notion": "acme", "linear": "lin-1"},
        },
        b={
            "names": ["ACME, inc."],
            "domains": ["ACME.com"],
            "sources": {"notion": "acme", "linear": "lin-1"},
        },
        c={"names": ["Clean"]},
    )
    assert table_errors(t) == [
        "domain 'acme.com' is on two identities: a, b",
        "name 'Acme Inc' is on two identities: a, b",
        "source notion 'acme' is on two identities: a, b",
        "source linear 'lin-1' is on two identities: a, b",
    ]


def test_table_errors_empty_for_a_clean_table(tmp_path):
    t = table(
        tmp_path, a={"names": ["A"], "domains": ["a.com"]}, b={"names": ["B"], "domains": ["b.com"]}
    )
    assert table_errors(t) == []


def test_with_implicit_adds_only_unresolved_rows(tmp_path):
    t = table(tmp_path, acmeco={"names": ["Acmeco"]})
    rows = generate_linear(
        [
            ds(),  # resolves: kept
            ds(canonical_name="Beta Co", domains=["beta.io"]),  # unresolved
            ds(canonical_name="Gamma", domains=[]),  # unresolved, no domain
        ]
    )
    out = with_implicit(t, rows)
    assert list(out.entries) == ["acmeco", "beta", "gamma"]
    assert out.entries["beta"].names == ["Beta Co"]
    assert out.entries["beta"].domains == ["beta.io"]
    assert out.entries["gamma"].names == ["Gamma"]
    assert list(t.entries) == ["acmeco"]  # input untouched
    for row in rows:
        assert resolve(keys_for_row(row), out) is not None
