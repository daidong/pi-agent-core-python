"""Re-run the upstream resource functions on the shared plugin cases and compare."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

command = [
    "node",
    "--import",
    "./reference/register.mjs",
    "reference/plugin-runner.ts",
    "compat/plugin-cases.json",
]
output = subprocess.run(command, capture_output=True, text=True, check=True, cwd=ROOT).stdout
(ROOT / "compat/results/plugins.upstream.json").write_text(
    json.dumps(json.loads(output), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
)
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "tests/test_plugin_conformance.py"], cwd=ROOT
)
sys.exit(result.returncode)
