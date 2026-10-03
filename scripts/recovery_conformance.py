"""Re-run the upstream classifiers on the shared cases and compare with Python."""

import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_recovery_conformance import CASES, classify  # noqa: E402

command = [
    "node",
    "--import",
    "./reference/register.mjs",
    "reference/recovery-runner.ts",
    "compat/recovery-cases.json",
]
upstream = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
Path("compat/results/recovery-conformance.upstream.json").write_text(
    json.dumps(upstream, indent=2) + "\n"
)
differences = [
    i for i, (case, expected) in enumerate(zip(CASES, upstream)) if classify(case) != expected
]
print(
    f"recovery classifiers: {len(CASES) - len(differences)}/{len(CASES)} match upstream",
    differences or "",
)
sys.exit(int(bool(differences)))
