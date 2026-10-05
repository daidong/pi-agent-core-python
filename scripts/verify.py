"""Record actual commands, versions, exit statuses and output. No inferred passes."""

import json
from pathlib import Path
import platform
import subprocess
import sys
import time

COMMANDS = [
    ["uv", "sync", "--locked", "--extra", "providers", "--extra", "mcp-interactive"],
    ["uv", "run", "ruff", "check", "src", "tests", "examples", "compat", "scripts"],
    ["uv", "run", "ruff", "format", "--check", "src", "tests", "examples", "compat", "scripts"],
    ["uv", "run", "mypy", "src/pi_python"],
    ["uv", "run", "pytest", "-q"],
    ["uv", "run", "python", "examples/in_memory.py"],
    [
        "npm",
        "test",
        "--prefix",
        "reference/pi/packages/agent",
        "--",
        "test/agent-loop.test.ts",
        "test/agent.test.ts",
    ],
    [
        "npm",
        "test",
        "--prefix",
        "reference/pi/packages/ai",
        "--",
        "test/transcript-tool-changes.test.ts",
        "test/validation.test.ts",
        "test/anthropic-mid-conversation-effort.test.ts",
        "test/openai-codex-stream.test.ts",
    ],
    ["uv", "run", "python", "scripts/conformance.py"],
    ["uv", "run", "python", "scripts/provider_conformance.py"],
    ["uv", "run", "python", "scripts/websocket_conformance.py"],
    ["uv", "run", "python", "scripts/recovery_conformance.py"],
    ["uv", "run", "python", "scripts/plugin_conformance.py"],
    ["uv", "run", "python", "examples/plugin_demo.py"],
    ["uv", "run", "python", "examples/mcp_interactive.py"],
    ["uv", "run", "python", "examples/provider_chat.py", "--help"],
    ["uv", "build"],
]


def main():
    records = []
    for command in COMMANDS:
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        record = {
            "command": command,
            "exit_code": result.returncode,
            "seconds": time.monotonic() - started,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        records.append(record)
        print(" ".join(command), "->", result.returncode, flush=True)
        if result.returncode:
            break
    versions = {
        cmd[0]: subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        for cmd in [["node", "--version"], ["npm", "--version"], ["uv", "--version"]]
    }
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "versions": versions,
        "checks": records,
        "status": "passed"
        if len(records) == len(COMMANDS) and all(r["exit_code"] == 0 for r in records)
        else "failed",
    }
    Path("compat/results/verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
