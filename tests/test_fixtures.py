import importlib.util
import re
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"

_spec = importlib.util.spec_from_file_location("generate_fixtures", FIX / "generate_fixtures.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def test_committed_fixtures_match_the_generator():
    """Re-running the generator is a no-op, so the JSON files cannot drift by hand-editing."""
    for name in gen.FIXTURES:
        assert (FIX / f"{name}.json").read_text() == gen.render(name), name


def test_fixtures_use_only_reserved_test_domains():
    for name in gen.FIXTURES:
        text = (FIX / f"{name}.json").read_text()
        assert "notion.so" not in text
        for email in EMAIL.findall(text):
            assert email.endswith(".example.test"), (name, email)
