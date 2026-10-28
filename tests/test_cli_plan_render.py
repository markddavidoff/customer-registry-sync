"""Terraform-style plan rendering: exact lines, built from real plan_deploy output."""

from customer_registry_sync import cli
from customer_registry_sync.linear_files import CuratedFields
from customer_registry_sync.deploy import plan_deploy
from customer_registry_sync.linear_client import LiveCustomer
from customer_registry_sync.linear_schema import LinearCustomer


def desired(**kw) -> LinearCustomer:
    base = {
        "name": "Globex",
        "domains": ["globex.example.test"],
        "revenue": 250000,
        "tier": "Tier 1",
        "status": "Active",
        "external_ids": ["globex"],
    }
    base.update(kw)
    return LinearCustomer(**base)


def livec(**kw) -> LiveCustomer:
    base = {
        "id": "lin-1",
        "name": "Globex",
        "domains": ["globex.example.test"],
        "revenue": 250000,
        "tier": "Tier 1",
        "status": "Active",
        "external_ids": ["globex"],
        "updated_at": "2026-10-07T00:00:00.000Z",
    }
    base.update(kw)
    return LiveCustomer(**base)


def test_render_rename_and_revenue_change():
    plan = plan_deploy([desired(name="Globex Inc", revenue=300000)], [livec()])
    assert cli.render_plan(plan) == [
        "~ Globex Inc",
        "    name: 'Globex' -> 'Globex Inc'",
        "    revenue: 250000 -> 300000",
    ]


def test_render_create():
    plan = plan_deploy(
        [desired(name="New Co", external_ids=["new"], domains=["new.example.test"])], []
    )
    assert cli.render_plan(plan) == ["+ New Co"]


def test_render_retire_domain_remove_marks_header():
    lv = livec(domains=["globex.example.test", "old.example.org"])
    plan = plan_deploy(
        [desired()], [lv], retire={"globex": CuratedFields(domains=["old.example.org"])}
    )
    assert cli.render_plan(plan) == ["~ Globex (retire)", "    domains: - old.example.org"]


def test_render_domain_add():
    plan = plan_deploy([desired(domains=["globex.example.test", "globex.example.org"])], [livec()])
    assert cli.render_plan(plan) == ["~ Globex", "    domains: + globex.example.org"]


def test_render_clear_and_external_ids():
    lv = livec(external_ids=["old-slug"])
    plan = plan_deploy([desired()], [lv], retire={"globex": CuratedFields(tier="Tier 1")})
    assert cli.render_plan(plan) == [
        "~ Globex (retire)",
        "    external_ids: [old-slug] -> [globex]",
        "    tier: 'Tier 1' -> None",
    ]


def test_print_plan_summary_counts_destructive(capsys):
    lv = livec(domains=["globex.example.test", "old.example.org"])
    plan = plan_deploy(
        [desired()], [lv], retire={"globex": CuratedFields(domains=["old.example.org"])}
    )
    cli._print_plan(plan)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "DeployPlan: creates=0 updates=1 aborts=0 orphans=0 unchanged=0 destructive=1"
    )
    assert out[1:] == ["~ Globex (retire)", "    domains: - old.example.org"]
