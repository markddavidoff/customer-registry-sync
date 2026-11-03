"""`refresh --no-input`: sync-inbound, then the found-dataset ingest, then generate."""

import os

import pytest

from customer_registry_sync import cli


def _stub_steps(monkeypatch, codes=(0, 0, 0)):
    """Replace the three handlers with recorders; return the list of what they were given."""
    seen: list[tuple] = []

    def make(name, code):
        def handler(args):
            seen.append((name, args.command, getattr(args, "only", None), args.no_input))
            return code

        return handler

    sync, ingest, generate = codes
    monkeypatch.setattr(cli, "_cmd_sync_inbound", make("sync-inbound", sync))
    monkeypatch.setattr(cli, "_cmd_ingest", make("ingest", ingest))
    monkeypatch.setattr(cli, "_cmd_generate", make("generate", generate))
    return seen


def test_refresh_runs_three_steps_in_order(monkeypatch, run_cli):
    seen = _stub_steps(monkeypatch)
    assert run_cli(["refresh", "--no-input"]) == 0
    assert seen == [
        ("sync-inbound", "sync-inbound", None, True),
        ("ingest", "ingest", "linear-found", True),
        ("generate", "generate", None, True),
    ]


@pytest.mark.parametrize(
    ("codes", "want_code", "want_steps"),
    [
        ((2, 0, 0), 2, ["sync-inbound"]),
        ((0, 3, 0), 3, ["sync-inbound", "ingest"]),
        ((0, 0, 4), 4, ["sync-inbound", "ingest", "generate"]),
    ],
)
def test_refresh_stops_at_the_first_failure_and_returns_its_code(
    monkeypatch, run_cli, codes, want_code, want_steps
):
    seen = _stub_steps(monkeypatch, codes)
    assert run_cli(["refresh", "--no-input"]) == want_code
    assert [s[0] for s in seen] == want_steps


def test_refresh_uses_the_same_config_for_every_step(monkeypatch, make_config, tmp_path):
    """Each step sees the configured data_dir, not a default."""
    data_dir = tmp_path / "data"
    cfg_path = make_config(data_dir=data_dir)
    outs = []

    def handler(args):
        outs.append(cli._out_dir(args))
        return 0

    for name in ("_cmd_sync_inbound", "_cmd_ingest", "_cmd_generate"):
        monkeypatch.setattr(cli, name, handler)
    assert cli.main(["--config", str(cfg_path), "refresh", "--no-input"]) == 0
    assert outs == [data_dir] * 3


def test_refresh_gives_the_token_only_to_sync_inbound(monkeypatch, run_cli):
    """The offline steps (ingest, generate) never see the Linear token."""
    monkeypatch.setenv("LINEAR_API_TOKEN", "t0ken")
    seen: dict[str, str | None] = {}

    def make(name):
        def handler(args):
            seen[name] = os.environ.get("LINEAR_API_TOKEN")
            return 0

        return handler

    monkeypatch.setattr(cli, "_cmd_sync_inbound", make("sync-inbound"))
    monkeypatch.setattr(cli, "_cmd_ingest", make("ingest"))
    monkeypatch.setattr(cli, "_cmd_generate", make("generate"))
    assert run_cli(["refresh", "--no-input"]) == 0
    assert seen == {"sync-inbound": "t0ken", "ingest": None, "generate": None}
    assert os.environ["LINEAR_API_TOKEN"] == "t0ken", "restored for the caller"


def test_refresh_restores_the_token_after_a_failure(monkeypatch, run_cli):
    monkeypatch.setenv("LINEAR_API_TOKEN", "t0ken")
    _stub_steps(monkeypatch, (2, 0, 0))
    assert run_cli(["refresh", "--no-input"]) == 2
    assert os.environ["LINEAR_API_TOKEN"] == "t0ken"


def test_refresh_is_in_the_parser():
    parser, handlers = cli.build_parser()
    assert "refresh" in handlers
    assert parser.parse_args(["refresh", "--no-input"]).no_input is True


def test_refresh_records_a_linear_edit_in_the_data_dir(tmp_path, monkeypatch, run_cli):
    """The refresh PR commits the whole data dir, so it carries linear-overrides.yaml
    beside the mirror it refreshed."""
    from customer_registry_sync.linear_client import LiveCustomer
    from customer_registry_sync.linear_schema import LinearCustomer, LinearSynced
    from customer_registry_sync.writers import write_desired, write_state

    old = LinearCustomer(name="Acme [Active]", revenue=5, status="Active", external_ids=["acme"])
    write_desired([old], tmp_path)
    old.linear_synced = LinearSynced("lin_1", "2026-10-01T00:00:00.000Z", "h")
    write_state([old], tmp_path)
    edited = LiveCustomer(
        id="lin_1",
        name="Acme [Active]",
        domains=[],
        revenue=7,
        tier=None,
        status="Active",
        external_ids=["acme"],
        updated_at="2026-10-02T00:00:00.000Z",
    )

    class _Client:
        def list_customers(self):
            return [edited]

    monkeypatch.setattr(cli, "_linear_client", lambda: _Client())
    monkeypatch.setattr(cli, "_cmd_ingest", lambda args: 0)
    monkeypatch.setattr(cli, "_cmd_generate", lambda args: 0)
    assert run_cli(["refresh", "--no-input"], data_dir=tmp_path) == 0
    import yaml

    data = yaml.safe_load((tmp_path / "linear-overrides.yaml").read_text())
    assert data == {"customers": {"acme": {"pending": {"revenue": 7}}}}
