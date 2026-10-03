"""Every example runs offline, with no credentials in the environment."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CASES = {
    "in_memory.py": "3 complete records",
    "quickstart.py": "status=completed",
    "subagent.py": "specialist said: The line has 6 words",
    "save_restore.py": "answer after restore: The deadline is Friday.",
    "recovery.py": "context full: compacted",
    "mcp_tools.py": "answer: 19 + 23 = 42",
    "local_model.py": "answer: The sum is 42.",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_example_runs_offline(name):
    if name == "mcp_tools.py" and importlib.util.find_spec("mcp") is None:
        pytest.skip("needs the [mcp] extra")
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    out = subprocess.run(
        [sys.executable, str(EXAMPLES / name)], capture_output=True, text=True, timeout=60, env=env
    )
    assert out.returncode == 0, out.stderr
    assert CASES[name] in out.stdout


def test_every_example_is_covered():
    scripts = {p.name for p in EXAMPLES.glob("*.py")} - {"provider_chat.py"}  # needs credentials
    assert scripts <= set(CASES)
