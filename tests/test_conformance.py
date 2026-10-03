"""The Python side must continue to match the retained, actually executed upstream traces."""

import json
from pathlib import Path
import pytest
from compat.runner import map_errors, run_fixture

FIXTURES = sorted(Path("compat/fixtures").glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_shared_upstream_trace(path):
    fixture = json.loads(path.read_text())
    upstream = json.loads(Path(f"compat/results/{path.stem}.upstream.json").read_text())
    actual = await run_fixture(fixture)
    assert map_errors(actual) == map_errors(upstream)
