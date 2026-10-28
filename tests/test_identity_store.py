from __future__ import annotations

import textwrap
from pathlib import Path


from customer_registry_sync.identity import (
    IDENTITIES_FILE,
    keys_for_record,
    load_identities,
    resolve,
)
from customer_registry_sync.identity import IDENTITIES_HEADER, IdentityStore
from customer_registry_sync.schema import CustomerRecord


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / IDENTITIES_FILE
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return p


def test_missing_file_starts_with_the_header(tmp_path):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    assert store.table.entries == {}
    assert store.save() is False  # nothing added: nothing written
    assert not (tmp_path / IDENTITIES_FILE).exists()
    store.add_identity("acme", names=["Acme"], domains=["acme.com"], sources={"notion": "acme"})
    assert store.save() is True
    text = (tmp_path / IDENTITIES_FILE).read_text()
    assert text.startswith(IDENTITIES_HEADER)
    assert text.startswith("# One entry per company.")
    assert text == (
        IDENTITIES_HEADER + "acme:\n"
        "  names: [Acme]\n"
        "  domains: [acme.com]\n"
        "  sources:\n"
        "    notion: acme\n"
    )
    assert load_identities(tmp_path / IDENTITIES_FILE).entries["acme"].values["domains"] == [
        "acme.com"
    ]


def test_add_values_appends_missing_only_and_keeps_hand_edit(tmp_path):
    before = textwrap.dedent("""\
        acme:
          names:
            - Acme
            - Acme Hand Edit
          domains:
            - zeta.com
            - acme.com
          sources:
            notion: acme
    """)
    p = tmp_path / IDENTITIES_FILE
    p.write_text(before)
    store = IdentityStore(p)
    store.add_values(
        "acme",
        names=["ACME", "Acme Labs"],
        domains=["ACME.com", "new.com"],
        sources={"notion": "acme", "linear": "lin-1"},
    )
    assert store.reports == []
    assert store.save() is True
    after = p.read_text()
    assert after == textwrap.dedent("""\
        acme:
          names:
            - Acme
            - Acme Hand Edit
            - Acme Labs
          domains:
            - zeta.com
            - acme.com
            - new.com
          sources:
            notion: acme
            linear: lin-1
    """)
    # nothing but added lines differs from the hand-written file
    assert [ln for ln in before.splitlines() if ln not in after.splitlines()] == []
    t = load_identities(p)
    assert t.entries["acme"].values["names"] == ["Acme", "Acme Hand Edit", "Acme Labs"]
    assert t.entries["acme"].values["domains"] == ["zeta.com", "acme.com", "new.com"]


def test_add_values_creates_missing_keys_and_extends_flow_lists(tmp_path):
    p = write(
        tmp_path,
        """
        acme: {names: [Acme]}
        beta:
          names: [Beta]
          domains: [beta.io]
    """,
    )
    store = IdentityStore(p)
    store.add_values("acme", domains=["acme.com"], sources={"linear": "lin-1"})
    store.add_values("beta", names=["Beta Co"])
    assert store.save() is True
    t = load_identities(p)
    assert t.entries["acme"].values["domains"] == ["acme.com"]
    assert t.entries["acme"].sources == {"linear": "lin-1"}
    assert t.entries["beta"].values["names"] == ["Beta", "Beta Co"]
    assert t.entries["beta"].values["domains"] == ["beta.io"]


def test_never_appends_value_held_by_another_identity(tmp_path):
    p = write(
        tmp_path,
        """
        acme:
          names: [Acme]
          domains: [acme.com]
        beta:
          names: [Beta Co]
          domains: [beta.io]
    """,
    )
    original = p.read_text()
    store = IdentityStore(p)
    store.add_values("acme", names=["beta co."], domains=["BETA.io"])
    assert store.reports == [
        "identities: not adding domain 'BETA.io' to 'acme': already on 'beta'",
        "identities: not adding name 'beta co.' to 'acme': already on 'beta'",
    ]
    assert store.save() is False
    assert p.read_text() == original
    assert store.table.entries["acme"].values["domains"] == ["acme.com"]


def test_source_value_never_overwritten(tmp_path):
    p = write(
        tmp_path,
        """
        acme:
          sources:
            linear: lin-old
    """,
    )
    original = p.read_text()
    store = IdentityStore(p)
    store.add_values("acme", sources={"linear": "lin-new"})
    assert store.reports == [
        "identities: 'acme' keeps source linear='lin-old'; ingest saw 'lin-new'",
    ]
    assert store.save() is False
    assert p.read_text() == original
    assert store.table.entries["acme"].sources == {"linear": "lin-old"}


def test_comments_and_order_survive_a_save(tmp_path):
    p = write(
        tmp_path,
        """
        # Curated by hand: keep the order.
        zeta:
          names: [Zeta]
        acme:
          names: [Acme]
          domains: [acme.com]   # was linear-enrichment.yaml
          sources:
            notion: acme  # slug at migration
        # trailing note about beta
        beta:
          names: [Beta]
    """,
    )
    store = IdentityStore(p)
    store.add_values("acme", domains=["acme.io"])
    store.add_identity("gamma", names=["Gamma"], domains=[], sources={})
    assert store.save() is True
    text = p.read_text()
    assert text.startswith("# Curated by hand: keep the order.\nzeta:")
    assert "# was linear-enrichment.yaml" in text
    assert "# slug at migration" in text
    assert "# trailing note about beta" in text
    assert list(load_identities(p).entries) == ["zeta", "acme", "beta", "gamma"]
    assert load_identities(p).entries["acme"].values["domains"] == ["acme.com", "acme.io"]


def test_identity_sources_use_notion_key(tmp_path):
    """A record round-trips through the store with its source recorded under `notion`."""
    p = tmp_path / IDENTITIES_FILE
    store = IdentityStore(p)
    store.add_identity("acme", names=["Acme"], domains=["acme.com"], sources={"notion": "acme"})
    assert store.save() is True
    assert "    notion: acme\n" in p.read_text()
    assert load_identities(p).entries["acme"].sources == {"notion": "acme"}
    item = keys_for_record(
        CustomerRecord(canonical_name="Other", classification="real"),
        dataset="notion",
        source_id="acme",
    )
    hit = resolve(item, load_identities(p))
    assert hit is not None and (hit.canonical_id, hit.key) == ("acme", "source id")


def test_hand_quoted_values_survive_append(tmp_path):
    p = tmp_path / IDENTITIES_FILE
    p.write_text(
        textwrap.dedent("""\
        acme:
          names: ["ACME, Inc.", 'Acme: Two', Plain]
          domains:
            - a.com
    """)
    )
    store = IdentityStore(p)
    store.add_values("acme", names=["Fresh"], domains=["c.com"])
    assert store.save() is True
    text = p.read_text()
    assert "names: [\"ACME, Inc.\", 'Acme: Two', Plain, Fresh]" in text
    assert "- c.com" in text
    assert load_identities(p).entries["acme"].values["names"] == [
        "ACME, Inc.",
        "Acme: Two",
        "Plain",
        "Fresh",
    ]
