from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from customer_registry_sync.identity import (
    IDENTITIES_FILE,
    keys_for_record,
    load_identities,
    resolve,
)
from customer_registry_sync.identity_store import IDENTITIES_HEADER, IdentityStore
from customer_registry_sync.schema import CustomerRecord


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / IDENTITIES_FILE
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return p


def test_missing_file_starts_with_the_header(tmp_path):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    assert store.table.entries == {}
    assert store.save() is False                       # nothing added: nothing written
    assert not (tmp_path / IDENTITIES_FILE).exists()
    store.add_identity("acme", names=["Acme"], domains=["acme.com"],
                       sources={"notion": "acme"})
    assert store.save() is True
    text = (tmp_path / IDENTITIES_FILE).read_text()
    assert text.startswith(IDENTITIES_HEADER)
    assert text.startswith("# One entry per company.")
    assert text == (
        IDENTITIES_HEADER
        + "acme:\n"
        "  names: [Acme]\n"
        "  domains: [acme.com]\n"
        "  sources:\n"
        "    notion: acme\n"
    )
    assert load_identities(tmp_path / IDENTITIES_FILE).entries["acme"].domains == ["acme.com"]


def test_add_identity_takes_next_suffix_on_collision(tmp_path):
    p = write(tmp_path, """
        acme:
          names: [Acme]
    """)
    store = IdentityStore(p)
    assert store.add_identity("acme", names=["Acme Two"], domains=[], sources={}) == "acme-2"
    assert store.add_identity("acme", names=["Acme Three"], domains=[], sources={}) == "acme-3"
    assert list(store.table.entries) == ["acme", "acme-2", "acme-3"]
    assert store.save() is True
    assert list(load_identities(p).entries) == ["acme", "acme-2", "acme-3"]


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
    store.add_values("acme", names=["ACME", "Acme Labs"], domains=["ACME.com", "new.com"],
                     sources={"notion": "acme", "linear": "lin-1"})
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
    assert t.entries["acme"].names == ["Acme", "Acme Hand Edit", "Acme Labs"]
    assert t.entries["acme"].domains == ["zeta.com", "acme.com", "new.com"]


def test_add_values_creates_missing_keys_and_extends_flow_lists(tmp_path):
    p = write(tmp_path, """
        acme: {names: [Acme]}
        beta:
          names: [Beta]
          domains: [beta.io]
    """)
    store = IdentityStore(p)
    store.add_values("acme", domains=["acme.com"], sources={"linear": "lin-1"})
    store.add_values("beta", names=["Beta Co"])
    assert store.save() is True
    t = load_identities(p)
    assert t.entries["acme"].domains == ["acme.com"]
    assert t.entries["acme"].sources == {"linear": "lin-1"}
    assert t.entries["beta"].names == ["Beta", "Beta Co"]
    assert t.entries["beta"].domains == ["beta.io"]


def test_never_appends_value_held_by_another_identity(tmp_path):
    p = write(tmp_path, """
        acme:
          names: [Acme]
          domains: [acme.com]
        beta:
          names: [Beta Co]
          domains: [beta.io]
    """)
    original = p.read_text()
    store = IdentityStore(p)
    store.add_values("acme", names=["beta co."], domains=["BETA.io"])
    assert store.reports == [
        "identities: not adding domain 'BETA.io' to 'acme': already on 'beta'",
        "identities: not adding name 'beta co.' to 'acme': already on 'beta'",
    ]
    assert store.save() is False
    assert p.read_text() == original
    assert store.table.entries["acme"].domains == ["acme.com"]


def test_source_value_never_overwritten(tmp_path):
    p = write(tmp_path, """
        acme:
          sources:
            linear: lin-old
    """)
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
    p = write(tmp_path, """
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
    """)
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
    assert load_identities(p).entries["acme"].domains == ["acme.com", "acme.io"]


def test_save_without_changes_writes_nothing(tmp_path):
    p = write(tmp_path, """
        acme:
          names: [Acme]
          domains: [acme.com]
    """)
    os.utime(p, (1_000_000_000, 1_000_000_000))
    before = p.stat().st_mtime_ns
    store = IdentityStore(p)
    store.add_values("acme", names=["acme"], domains=["ACME.COM"])   # already present
    assert store.save() is False
    assert p.stat().st_mtime_ns == before


def test_save_twice_writes_once(tmp_path):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    store.add_identity("acme", names=["Acme"], domains=[], sources={})
    assert store.save() is True
    assert store.save() is False


def test_comment_only_file_keeps_its_comments(tmp_path):
    p = tmp_path / IDENTITIES_FILE
    p.write_text("# nothing yet, add entries below\n")
    store = IdentityStore(p)
    store.add_identity("acme", names=["Acme"], domains=[], sources={})
    assert store.save() is True
    assert p.read_text().startswith("# nothing yet, add entries below\nacme:")


def test_duplicate_canonical_id_is_rejected(tmp_path):
    p = write(tmp_path, """
        acme:
          names: [Acme]
        beta:
          names: [Beta]
        acme:
          domains: [acme.com]
    """)
    with pytest.raises(ValueError, match="duplicate key 'acme'"):
        IdentityStore(p)
    with pytest.raises(ValueError, match="duplicate key 'acme'"):
        load_identities(p)


def test_malformed_file_is_a_value_error(tmp_path):
    p = write(tmp_path, """
        acme:
          aliases: [ACME]
    """)
    with pytest.raises(ValueError, match="aliases"):
        IdentityStore(p)
    p.write_text("acme: [unclosed\n")
    with pytest.raises(ValueError, match=IDENTITIES_FILE):
        IdentityStore(p)


def test_add_values_unknown_identity_or_source_key_raises(tmp_path):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    with pytest.raises(ValueError, match="nope"):
        store.add_values("nope", names=["X"])
    store.add_identity("acme", names=[], domains=[], sources={})
    with pytest.raises(ValueError, match="hubspot"):
        store.add_values("acme", sources={"hubspot": "x"})
    with pytest.raises(ValueError, match="hubspot"):
        store.add_identity("beta", names=[], domains=[], sources={"hubspot": "x"})


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
    p.write_text(textwrap.dedent("""\
        acme:
          names: ["ACME, Inc.", 'Acme: Two', Plain]
          domains:
            - a.com
    """))
    store = IdentityStore(p)
    store.add_values("acme", names=["Fresh"], domains=["c.com"])
    assert store.save() is True
    text = p.read_text()
    assert 'names: ["ACME, Inc.", \'Acme: Two\', Plain, Fresh]' in text
    assert "- c.com" in text
    assert load_identities(p).entries["acme"].names == [
        "ACME, Inc.", "Acme: Two", "Plain", "Fresh"]
