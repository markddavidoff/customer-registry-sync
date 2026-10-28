"""The ignore list: matching, validation, conflicts, stale entries.

The `ignore` command that appends entries arrives with the CLI port; until then the file is
hand-edited, so these tests write the documented YAML shape."""

import pytest
from gitops_engine.ignore import (
    BUILTIN_PREFIX,
    IgnoreEntry,
    IgnorePrefix,
    append_ignore,
    ignore_errors,
    ignored_lines,
    load_ignore,
    partition_live,
    stale_ignore,
)

from customer_registry_sync.datasets import LINEAR_FOUND, NOTION, DatasetRecord
from customer_registry_sync.generate import merge_datasets
from customer_registry_sync.identity import IDENTITIES_FILE, IdentityTable, load_identities
from customer_registry_sync.identity import IdentityStore
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.linear_files import IGNORE_FILE, IGNORE_FORMAT, ignore_conflicts
from customer_registry_sync.linear_schema import LINEAR_SCHEMA
from customer_registry_sync.schema import CustomerRecord

PRIORITY = [NOTION, LINEAR_FOUND]

# The registry owner's own Linear org is ignored by id; its name is informational.
OWN_ORG_FILE = """\
# Linear customers the sync never touches. Each entry needs a reason.
customers:
  - linear_id: lin_own
    name: Pied Piper       # informational; matching is by linear_id
    reason: our own company
name_prefixes:
  - prefix: "test-"
    reason: manual experiments
"""


def _node(lid, name, *, domains=(), xids=(), status="Active"):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": None,
            "externalIds": list(xids),
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s", "name": status} if status else None,
            "tier": None,
        }
    )


def _ignore(tmp_path, text=OWN_ORG_FILE):
    path = tmp_path / IGNORE_FILE
    path.write_text(text)
    return load_ignore(path, IGNORE_FORMAT)


def _table(tmp_path, *identities):
    store = IdentityStore(tmp_path / IDENTITIES_FILE)
    for cid, names, domains, sources in identities:
        store.add_identity(cid, names=names, domains=domains, sources=sources)
    store.save()
    return load_identities(tmp_path / IDENTITIES_FILE)


def _entries(*items):
    return [DatasetRecord(dataset=d, source_id=s, record=r) for d, s, r in items]


def test_match_by_linear_id_survives_rename(tmp_path):
    ign = _ignore(tmp_path)
    hit = ign.match("lin_own", "Pied Piper (renamed)", scope=None)
    assert hit == IgnoreEntry("lin_own", "Pied Piper", "our own company", 3)
    assert ign.match("lin_other", "Pied Piper", scope=None) is None  # the name is informational


def test_match_by_prefix_and_builtin(tmp_path):
    ign = _ignore(tmp_path)
    assert ign.match("lin_1", "test-sandbox", scope=None) == IgnorePrefix(
        "test-", "manual experiments", 7
    )
    builtin = ign.match("lin_2", f"{BUILTIN_PREFIX}abc-Acme", scope=None)
    assert isinstance(builtin, IgnorePrefix) and builtin.line is None
    assert ign.match("lin_3", "Acme test-", scope=None) is None  # startswith, not contains
    # no file at all: the built-in prefix still applies
    empty = load_ignore(tmp_path / "missing.yaml", IGNORE_FORMAT)
    assert empty.entries == [] and empty.match("x", f"{BUILTIN_PREFIX}x", scope=None)


def test_scoped_e2e_run_may_write_its_prefix(tmp_path):
    ign = _ignore(tmp_path)
    scope = f"{BUILTIN_PREFIX}1a2b3c4d-"
    name = f"{scope}Acme"
    assert ign.match("lin_e2e", name, scope=None) is not None  # unscoped: ignored
    assert ign.match("lin_e2e", name, scope=scope) is None  # its own scoped run: managed
    assert ign.match("lin_own", "Pied Piper", scope=scope) is not None  # ids still match
    live = [_node("lin_e2e", name), _node("lin_own", "Pied Piper")]
    managed, ignored = partition_live(live, ign, LINEAR_SCHEMA, scope=scope)
    assert [lv.id for lv in managed] == ["lin_e2e"] and [lv.id for lv in ignored] == ["lin_own"]
    managed, ignored = partition_live(live, ign, LINEAR_SCHEMA, scope=None)
    assert managed == [] and [lv.id for lv in ignored] == ["lin_e2e", "lin_own"]


def test_file_prefix_still_ignored_under_matching_scope(tmp_path):
    # only the built-in prefix has the scoped exemption
    ign = _ignore(tmp_path)
    scope = "test-sandbox"
    hit = ign.match("lin_t", "test-sandbox-1", scope=scope)
    assert hit == IgnorePrefix("test-", "manual experiments", 7)
    managed, ignored = partition_live(
        [_node("lin_t", "test-sandbox-1")], ign, LINEAR_SCHEMA, scope=scope
    )
    assert managed == [] and [lv.id for lv in ignored] == ["lin_t"]
    assert "IGNORED PREFIX: test- (linear-ignore.yaml:7): manual experiments" in ignored_lines(
        ignored, ign, LINEAR_SCHEMA, scope=scope
    )
    # the built-in prefix keeps applying to a scope that does not start with it
    assert ign.match("lin_z", f"{BUILTIN_PREFIX}x", scope=scope) is not None


def test_validation_missing_reason_short_prefix_duplicate_id(tmp_path):
    path = tmp_path / IGNORE_FILE
    path.write_text(
        "customers:\n"
        "  - linear_id: lin_a\n"
        "    reason: test\n"
        "  - linear_id: lin_b\n"
        "  - linear_id: lin_a\n"
        "    reason: again\n"
        "name_prefixes:\n"
        "  - prefix: zz\n"
        "    reason: too short\n"
    )
    assert ignore_errors(path, IGNORE_FORMAT) == [
        "linear-ignore.yaml:4: missing reason",
        "linear-ignore.yaml:5: linear_id lin_a listed twice",
        "linear-ignore.yaml:8: prefix 'zz' is shorter than 4 characters",
    ]
    assert ignore_errors(tmp_path / "missing.yaml", IGNORE_FORMAT) == []
    _ignore(tmp_path)
    assert ignore_errors(tmp_path / IGNORE_FILE, IGNORE_FORMAT) == []


def test_stale_entry_warns(tmp_path):
    ign = _ignore(tmp_path)
    assert stale_ignore(ign, [_node("lin_other", "Acme")], LINEAR_SCHEMA) == [
        "stale ignore entry: linear-ignore.yaml:3: lin_own is not in Linear"
    ]
    assert stale_ignore(ign, [_node("lin_own", "Pied Piper")], LINEAR_SCHEMA) == []


def test_conflict_message_exact(tmp_path):
    ign = _ignore(tmp_path)
    entries = _entries(
        (
            NOTION,
            "pied-piper-consulting",
            CustomerRecord(
                canonical_name="Pied Piper Consulting",
                domains=["piedpiper.example.test"],
                classification="real",
            ),
        )
    )
    desired = merge_datasets(entries, IdentityTable(), PRIORITY).rows
    own = _node("lin_own", "Pied Piper", domains=["piedpiper.example.test"])
    failures, reports = ignore_conflicts([own], ign, entries, desired, IdentityTable(), scope=None)
    assert failures == [
        (
            "desired record 'Pied Piper Consulting' shares domain 'piedpiper.example.test' with "
            "ignored Linear customer 'Pied Piper' (linear-ignore.yaml:3). "
            "Remove that entry or change Pied Piper Consulting."
        )
    ]
    assert reports == []
    # the built-in prefix names its location as built-in
    e2e = _node("lin_e2e", f"{BUILTIN_PREFIX}x", domains=["piedpiper.example.test"])
    failures, _ = ignore_conflicts([e2e], ign, entries, desired, IdentityTable(), scope=None)
    assert failures == [
        (
            f"desired record 'Pied Piper Consulting' shares domain 'piedpiper.example.test' "
            f"with ignored Linear customer '{BUILTIN_PREFIX}x' (built-in). "
            f"Remove that entry or change Pied Piper Consulting."
        )
    ]
    # an ignored customer resolving to no desired row is no conflict
    other = _node("lin_own", "Pied Piper", domains=["unrelated.example.test"])
    assert ignore_conflicts([other], ign, entries, desired, IdentityTable(), scope=None) == (
        [],
        [],
    )


def test_ignoring_a_stubbed_customer_is_not_a_conflict(tmp_path):
    # A customer was stubbed from Linear, then someone ignored it.
    table = _table(
        tmp_path,
        ("umbrella", ["Umbrella Analytics"], ["umbrella.example.test"], {"linear": "lin_umb"}),
    )
    ign = _ignore(tmp_path, "customers:\n  - linear_id: lin_umb\n    reason: a test org\n")
    stub = CustomerRecord(
        canonical_name="Umbrella Analytics",
        domains=["umbrella.example.test"],
        classification="real",
        linear_customer_id="lin_umb",
        sources=["linear:lin_umb"],
    )
    entries = _entries((LINEAR_FOUND, "umbrella", stub))
    desired = merge_datasets(entries, table, PRIORITY).rows
    assert [d.linear_owned for d in desired] == [True]
    umb = _node(
        "lin_umb", "Umbrella Analytics", domains=["umbrella.example.test"], xids=["umbrella"]
    )
    _, ignored = partition_live([umb], ign, LINEAR_SCHEMA, scope=None)
    failures, reports = ignore_conflicts(ignored, ign, entries, desired, table, scope=None)
    assert failures == []
    assert reports == [
        (
            "stub 'Umbrella Analytics' is for ignored Linear customer 'Umbrella Analytics': "
            "the next refresh drops it"
        )
    ]
    # a different Linear customer on that stub's identity is still a conflict
    other = _node("lin_umb2", "Umbrella Analytics EU", domains=["umbrella.example.test"])
    ign2 = _ignore(tmp_path, "customers:\n  - linear_id: lin_umb2\n    reason: dup\n")
    failures, _ = ignore_conflicts([other], ign2, entries, desired, table, scope=None)
    assert len(failures) == 1 and "'Umbrella Analytics EU' (linear-ignore.yaml:2)" in failures[0]


def test_ignored_lines_list_customers_and_builtin(tmp_path):
    ign = _ignore(tmp_path)
    _, ignored = partition_live(
        [_node("lin_own", "Pied Piper"), _node("lin_1", "Acme")], ign, LINEAR_SCHEMA, scope=None
    )
    assert ignored_lines(ignored, ign, LINEAR_SCHEMA, scope=None) == [
        "IGNORED: 'Pied Piper' (lin_own): our own company",
        f"IGNORED PREFIX: {BUILTIN_PREFIX} (built-in)",
        "IGNORED PREFIX: test- (linear-ignore.yaml:7): manual experiments",
    ]


# ---- append_ignore: the writer the `ignore` command will call ----
# SRC covers it only through the CLI tests (ported with the CLI); these call it directly.


def test_append_ignore_creates_file_with_header_and_builtin_prefix(tmp_path):
    path = tmp_path / IGNORE_FILE
    assert (
        append_ignore(path, IGNORE_FORMAT, live_id="lin_x", name="Acme", reason=" a test org ")
        is True
    )
    text = path.read_text()
    assert text.startswith("# Linear customers the sync never touches.")
    assert ignore_errors(path, IGNORE_FORMAT) == []
    ign = load_ignore(path, IGNORE_FORMAT)
    assert ign.entries == [IgnoreEntry("lin_x", "Acme", "a test org", ign.entries[0].line)]
    # the file's own builtin prefix entry is the built-in, not a second file prefix
    assert [p.line is None for p in ign.prefixes] == [True]


def test_append_ignore_is_idempotent_and_keeps_comments(tmp_path):
    path = tmp_path / IGNORE_FILE
    path.write_text(OWN_ORG_FILE)
    assert (
        append_ignore(path, IGNORE_FORMAT, live_id="lin_own", name="Pied Piper", reason="again")
        is False
    )
    assert path.read_text() == OWN_ORG_FILE
    assert (
        append_ignore(path, IGNORE_FORMAT, live_id="lin_new", name="Hooli", reason="a test org")
        is True
    )
    text = path.read_text()
    assert "# informational; matching is by linear_id" in text
    assert [e.live_id for e in load_ignore(path, IGNORE_FORMAT).entries] == ["lin_own", "lin_new"]


def test_append_ignore_rejects_empty_id_or_reason_and_bad_shape(tmp_path):
    path = tmp_path / IGNORE_FILE
    for kwargs in (
        {"live_id": " ", "name": "x", "reason": "r"},
        {"live_id": "lin_x", "name": "x", "reason": " "},
    ):
        with pytest.raises(ValueError, match="must not be empty"):
            append_ignore(path, IGNORE_FORMAT, **kwargs)
    assert not path.exists()
    path.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="top level must be a mapping"):
        append_ignore(path, IGNORE_FORMAT, live_id="lin_x", name="x", reason="r")
