"""The Linear sync's curated files on the engine's loaders."""

from pathlib import Path

from gitops_engine.datasets import DATASETS_FILE
from gitops_engine.ignore import BUILTIN_PREFIX

from customer_registry_sync import linear_files
from customer_registry_sync.deploy_plan import LEGACY_GUARDED

from customer_registry_sync.identity import IDENTITIES_FILE
from customer_registry_sync.linear_files import (
    CURATED_INPUTS,
    IGNORE_FILE,
    LEGACY_FILES,
    RETIRE_FILE,
    no_hidden,
)
from customer_registry_sync.linear_overrides import OVERRIDES_FILE


def test_curated_inputs_name_the_files_check_and_deploy_read():
    # Names relative to the registry's data_dir; no module reads a hard-coded
    # `customers/` path. Each constant is the one the loading module uses.
    assert CURATED_INPUTS == (IDENTITIES_FILE, OVERRIDES_FILE, DATASETS_FILE)


def test_legacy_files_are_the_ones_overrides_replace():
    assert LEGACY_FILES == (IGNORE_FILE, RETIRE_FILE)
    assert (IGNORE_FILE, RETIRE_FILE) == ("linear-ignore.yaml", "linear-retire.yaml")


def test_every_legacy_file_is_guarded():
    # `check`, `deploy`, `ingest`, `sync-inbound` and `refresh` refuse each one
    assert LEGACY_GUARDED == LEGACY_FILES


def test_no_hidden_is_the_builtin_prefix_only():
    hidden = no_hidden()
    assert hidden.file == OVERRIDES_FILE and hidden.ids == {}
    assert [(p.prefix, p.line) for p in hidden.prefixes] == [(BUILTIN_PREFIX, None)]


def test_nothing_reads_the_ignore_file_but_its_legacy_name():
    # Only `IGNORE_FORMAT` and `migrate` (both for `adopt-live`) name the engine's ignore
    # module; no other module imports from it.
    src = Path(linear_files.__file__).parent
    users = sorted(p.name for p in src.rglob("*.py") if "gitops_engine.ignore" in p.read_text())
    assert users == ["linear_files.py", "migrate.py"]
    assert (
        "from gitops_engine.ignore import IgnoreFormat\n" in (src / "linear_files.py").read_text()
    )
