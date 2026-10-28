"""Re-run the upstream classifiers on the shared cases and compare with Python."""

import json
import hashlib
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_recovery_conformance import CASES, classify  # noqa: E402

manifest = json.loads(Path("reference/followup/manifest.json").read_text())
for source in manifest["files"]:
    data = (Path("reference/followup") / source["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise RuntimeError(f"Follow-up source digest mismatch: {source['file']}")

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
