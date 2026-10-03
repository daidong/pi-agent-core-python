"""Run both implementations, retain actual outputs, fail on any unexplained difference."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compat.runner import map_errors, run_fixture


async def main():
    records = []
    for path in sorted(Path("compat/fixtures").glob("*.json")):
        f = json.loads(path.read_text())
        command = ["node", "--import", "./reference/register.mjs", "reference/runner.ts", str(path)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        record = {
            "fixture": f["id"],
            "command": command,
            "reference_exit_code": completed.returncode,
        }
        if completed.returncode:
            record.update(status="failed", error=completed.stderr)
        else:
            upstream = json.loads(completed.stdout)
            python = await asyncio.wait_for(run_fixture(f), 10)
            Path(f"compat/results/{f['id']}.upstream.json").write_text(
                json.dumps(upstream, indent=2) + "\n"
            )
            Path(f"compat/results/{f['id']}.python.json").write_text(
                json.dumps(python, indent=2) + "\n"
            )
            left, right = map_errors(upstream), map_errors(python)
            differences = [k for k in left if left[k] != right[k]]
            record.update(status="passed" if not differences else "failed", differences=differences)
        records.append(record)
        print(f"{f['id']}: {record['status']} {record.get('differences', record.get('error', ''))}")
    Path("compat/results/conformance.json").write_text(json.dumps(records, indent=2) + "\n")
    return int(any(r["status"] != "passed" for r in records))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
