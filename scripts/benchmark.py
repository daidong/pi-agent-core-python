"""Local overhead measurements only; no real model latency, no performance promise."""

import asyncio
import json
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from pi_python import Agent, AssistantMessage, ScriptedProvider


async def python_loop():
    durations = []
    for i in range(210):
        start = time.perf_counter()
        a = Agent(provider=ScriptedProvider([AssistantMessage.text("hello")]))
        await a.prompt("hello")
        if i >= 10:
            durations.append((time.perf_counter() - start) * 1000)
    return durations


def main():
    python = asyncio.run(python_loop())
    node = json.loads(
        subprocess.check_output(
            ["node", "--import", "./reference/register.mjs", "reference/benchmark.ts"], text=True
        )
    )["milliseconds"]
    records = {}
    commands = {
        "python": [
            sys.executable,
            "-c",
            'import asyncio; from pi_python import Agent, ScriptedProvider, AssistantMessage; asyncio.run(Agent(provider=ScriptedProvider([AssistantMessage.text("hello")])).prompt("hello"))',
        ],
        "typescript": [
            "node",
            "--import",
            "./reference/register.mjs",
            "reference/runner.ts",
            "compat/fixtures/C01-text.json",
        ],
    }
    for name, command in commands.items():
        measurements = []
        for _ in range(10):
            start = time.perf_counter()
            result = subprocess.run(
                ["/usr/bin/time", "-l", *command], capture_output=True, text=True, check=True
            )
            measurements.append(
                {
                    "wall_ms": (time.perf_counter() - start) * 1000,
                    "peak_resident_bytes": int(
                        re.search(r"(\d+)\s+maximum resident set size", result.stderr)[1]
                    ),
                }
            )
        samples = python if name == "python" else node
        records[name] = {
            "cold_process_samples": measurements,
            "loop_ms": samples,
            "median_cold_ms": statistics.median(m["wall_ms"] for m in measurements),
            "median_peak_bytes": statistics.median(m["peak_resident_bytes"] for m in measurements),
            "median_loop_ms": statistics.median(samples),
        }
    Path("compat/results/benchmark.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "method": "macOS time -l; 10 cold text-only process runs; 200 in-process fresh Agent text runs after 10 warmups; TypeScript cold runner includes fixture JSON/trace serialization; development environment only",
                "results": records,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
