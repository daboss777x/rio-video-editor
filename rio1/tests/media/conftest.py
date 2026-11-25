"""Shared fixtures for media tests: the synthetic screen-recording clip."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "helpers" / "media"))

from make_fixture import make_fixture  # noqa: E402


@pytest.fixture(scope="session")
def fixture_paths():
    return make_fixture()


@pytest.fixture(scope="session")
def truth(fixture_paths):
    return json.loads(Path(fixture_paths["truth"]).read_text())
