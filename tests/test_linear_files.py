"""The Linear sync's curated files on the engine's loaders."""

from gitops_engine.datasets import DATASETS_FILE

from customer_registry_sync.identity import IDENTITIES_FILE
from customer_registry_sync.linear_files import (
    CURATED_INPUTS,
    IGNORE_FILE,
    LEGACY_FILES,
    RETIRE_FILE,
)
from customer_registry_sync.linear_overrides import OVERRIDES_FILE


def test_curated_inputs_name_the_files_check_and_deploy_read():
    # Names relative to the registry's data_dir; no module reads a hard-coded
    # `customers/` path. Each constant is the one the loading module uses.
    assert CURATED_INPUTS == (IDENTITIES_FILE, OVERRIDES_FILE, DATASETS_FILE)


def test_legacy_files_are_the_ones_overrides_replace():
    assert LEGACY_FILES == (IGNORE_FILE, RETIRE_FILE)
    assert (IGNORE_FILE, RETIRE_FILE) == ("linear-ignore.yaml", "linear-retire.yaml")
