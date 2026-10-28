"""The engine's CLI contract (gitops-engine docs/cli-contract.md): this tool implements it.

The engine's reusable workflow calls the tool only through these invocations. Each one
must parse here; the config keys are answered by test_config.py.
"""

import pytest

from customer_registry_sync import cli

# Every invocation the engine workflow makes, with its flags as the workflow writes them.
CONTRACT_CALLS = [
    ["config-get", "data_dir"],
    ["config-get", "deploy_environment"],
    ["config-get", "readonly_environment"],
    ["config-get", "state_dir"],
    ["config-get", "snapshot_dir"],
    ["config-get", "e2e_tests"],
    ["changed", "--since", "abc123"],
    ["check"],
    ["deploy", "--apply", "--prod", "--no-input"],
    ["restore", "--from", "snap.yaml", "--prod", "--no-input"],
    ["restore", "--from", "snap.yaml", "--rows", "a", "b", "--prod", "--no-input"],
    ["sync-inbound", "--no-input"],
    ["refresh", "--no-input"],
    ["tool-source"],
]


@pytest.mark.parametrize("argv", CONTRACT_CALLS, ids=lambda a: " ".join(a))
def test_cli_accepts_every_contract_invocation(argv):
    parser, handlers = cli.build_parser()
    args = parser.parse_args(argv)
    assert args.command in handlers
