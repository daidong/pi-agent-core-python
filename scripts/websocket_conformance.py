"""Cached Codex WebSocket frames: Python against the pinned upstream adapter.

Both sides run the same turns against a scripted socket. The comparison covers every
frame sent (with previous_response_id and the input delta), which connection carried
it, and each turn's stop reason. Python omits the `stream` field from WebSocket frames;
that intentional difference is removed before comparing and reported.
"""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

from pi_python import CancelToken, ModelRequest, message_from_dict
from pi_python.providers import HTTPTransport, OpenAICodexProvider
from pi_python.transcript import current_tools
from provider_conformance import catalog_for, from_pi

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "compat/websocket-fixtures"
OPTIONS = {"sessionId": "session_id", "reasoning": "reasoning", "cacheRetention": "cache_retention"}


async def python_run(fixture):
    from websockets.asyncio.server import serve

    sent = []
    connections = 0

    async def handle(socket):
        nonlocal connections
        connections += 1
        connection = connections
        async for raw in socket:
            sent.append({"connection": connection, "body": json.loads(raw)})
            for event in fixture["responses"][len(sent) - 1]:
                await socket.send(json.dumps(event))

    async with serve(handle, "127.0.0.1", 0) as server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        descriptor = fixture["model"]
        async with HTTPTransport(max_retries=0) as transport:
            provider = OpenAICodexProvider(
                api_key=fixture["api_key"],
                base_url=url,
                transport=transport,
                catalog=catalog_for("openai-codex", descriptor),
            )
            options = {OPTIONS[k]: v for k, v in fixture["options"].items()}
            options["transport"] = "websocket-cached"
            messages, stops = [], []
            for turn in fixture["turns"]:
                messages += [message_from_dict(from_pi(m)) for m in turn]
                request = ModelRequest(
                    messages, current_tools(messages), descriptor["id"], dict(options)
                )
                events = [e async for e in provider.stream(request, CancelToken())]
                final = events[-1].message
                stops.append(final.stop_reason)
                messages.append(final)
    return {"sent": sent, "stops": stops}


def without_stream(run):
    return {
        **run,
        "sent": [
            {**frame, "body": {k: v for k, v in frame["body"].items() if k != "stream"}}
            for frame in run["sent"]
        ],
    }


def main():
    results = []
    for path in sorted(FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text())
        python = asyncio.run(python_run(fixture))
        upstream = subprocess.run(
            [
                "node",
                "--import",
                "./reference/register.mjs",
                "./reference/websocket-runner.ts",
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        expected = (
            json.loads(upstream.stdout) if upstream.returncode == 0 else {"error": upstream.stderr}
        )
        stream_field = sorted(
            ({"python" for f in python["sent"] if "stream" in f["body"]})
            | {"upstream" for f in expected.get("sent", []) if "stream" in f["body"]}
        )
        passed = upstream.returncode == 0 and without_stream(python) == without_stream(expected)
        results.append(
            {
                "fixture": path.name,
                "python": python,
                "upstream": expected,
                "stream_field_sent_by": stream_field,
                "passed": passed,
                "upstream_exit_code": upstream.returncode,
            }
        )
        print(path.name, "passed" if passed else "FAILED", flush=True)
    report = {
        "source_revision": "a13d35a742c6ef8462812a28fbe1d8c8b7431c32",
        "scope": "cached Codex WebSocket frames, connection use and stop reasons over several turns; scripted socket, not a live service; the `stream` field is excluded and reported",
        "results": results,
        "status": "passed" if results and all(r["passed"] for r in results) else "failed",
    }
    (ROOT / "compat/results/websocket-conformance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
