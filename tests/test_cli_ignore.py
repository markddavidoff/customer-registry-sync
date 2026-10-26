from customer_registry_sync.ignore import BUILTIN_PREFIX, IGNORE_FILE, ignore_errors, load_ignore
from customer_registry_sync.inbound_sync import reconcile_inbound
from customer_registry_sync.linear_client import parse_customer_node
from customer_registry_sync.writers import write_state

HAND_FILE = """\
# Linear customers the sync never touches. Each entry needs a reason.
customers:
  - linear_id: lin_own
    name: 'Pied Piper'          # informational; matching is by linear_id
    reason: "our own company"
  # next entries are experiments
name_prefixes:
  - prefix: "test-"      # hand-written prefix
    reason: manual experiments
"""


def _node(lid, name, *, domains=()):
    return parse_customer_node(
        {
            "id": lid,
            "name": name,
            "domains": list(domains),
            "revenue": None,
            "externalIds": [],
            "updatedAt": "2026-10-01T00:00:00.000Z",
            "status": {"id": "s", "name": "Active"},
            "tier": None,
        }
    )


def _mirror(tmp_path, *nodes):
    write_state(reconcile_inbound(list(nodes), []), tmp_path)


def _run(run_cli, tmp_path, lid, reason="not a customer"):
    return run_cli(["ignore", "--linear-id", lid, "--reason", reason, "--out-dir", str(tmp_path)])


def test_ignore_appends_with_name_from_mirror(tmp_path, capsys, run_cli):
    _mirror(tmp_path, _node("lin_hooli", "Hooli Inc", domains=["hooli.example.test"]))
    assert _run(run_cli, tmp_path, "lin_hooli", "test account") == 0
    path = tmp_path / IGNORE_FILE
    ignore = load_ignore(path)
    assert [(e.linear_id, e.name, e.reason) for e in ignore.customers] == [
        ("lin_hooli", "Hooli Inc", "test account")
    ]
    assert [p.prefix for p in ignore.prefixes] == [BUILTIN_PREFIX]  # built-in always present
    assert path.read_text().startswith("# Linear customers the sync never touches.")
    assert ignore_errors(path) == []
    assert "lin_hooli" in capsys.readouterr().out


def test_ignore_refuses_id_missing_from_mirror(tmp_path, capsys, run_cli):
    _mirror(tmp_path, _node("lin_hooli", "Hooli Inc"))
    path = tmp_path / IGNORE_FILE
    path.write_text(HAND_FILE)
    assert _run(run_cli, tmp_path, "lin_nope") == 2
    assert "run sync-inbound first" in capsys.readouterr().err
    assert path.read_text() == HAND_FILE
    # no file is created either
    path.unlink()
    assert _run(run_cli, tmp_path, "lin_nope") == 2
    assert not path.exists()


def test_ignore_is_idempotent_and_keeps_reason(tmp_path, run_cli):
    _mirror(tmp_path, _node("lin_hooli", "Hooli Inc"))
    assert _run(run_cli, tmp_path, "lin_hooli", "first reason") == 0
    path = tmp_path / IGNORE_FILE
    before = path.read_text()
    assert _run(run_cli, tmp_path, "lin_hooli", "a different reason") == 4
    assert path.read_text() == before
    assert [e.reason for e in load_ignore(path).customers] == ["first reason"]


def test_ignore_preserves_comments(tmp_path, run_cli):
    _mirror(tmp_path, _node("lin_hooli", "Hooli Inc"))
    path = tmp_path / IGNORE_FILE
    path.write_text(HAND_FILE)
    assert _run(run_cli, tmp_path, "lin_hooli", "test account") == 0
    text = path.read_text()
    for kept in (
        "# Linear customers the sync never touches. Each entry needs a reason.",
        "name: 'Pied Piper'          # informational; matching is by linear_id",
        'reason: "our own company"',
        "# next entries are experiments",
        '- prefix: "test-"      # hand-written prefix',
    ):
        assert kept in text
    ignore = load_ignore(path)
    assert [e.linear_id for e in ignore.customers] == ["lin_own", "lin_hooli"]
    assert [p.prefix for p in ignore.prefixes] == [BUILTIN_PREFIX, "test-"]
    assert ignore_errors(path) == []


def test_ignore_refuses_an_invalid_file_and_an_empty_reason(tmp_path, capsys, run_cli):
    _mirror(tmp_path, _node("lin_hooli", "Hooli Inc"))
    path = tmp_path / IGNORE_FILE
    path.write_text("customers:\n  - linear_id: lin_x\n")  # no reason
    assert _run(run_cli, tmp_path, "lin_hooli") == 2
    assert path.read_text() == "customers:\n  - linear_id: lin_x\n"
    path.unlink()
    assert _run(run_cli, tmp_path, "lin_hooli", "  ") == 2
    assert not path.exists()
    assert "reason" in capsys.readouterr().err
