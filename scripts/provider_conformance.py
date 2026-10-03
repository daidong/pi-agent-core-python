"""Differential tests against actual pinned pi-ai/agent parsers; no external service."""

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import httpx
from pi_python import (
    CancelToken,
    ModelCatalog,
    ModelInfo,
    ModelRequest,
    ProxyProvider,
    UserMessage,
    ToolDeclaration,
    message_to_dict,
    message_from_dict,
)
from pi_python.proxy import _KEYS
from pi_python.transcript import current_tools
from pi_python.providers import (
    AnthropicProvider,
    OpenAIProvider,
    OpenAICodexProvider,
    OpenAICompletionsProvider,
    HTTPTransport,
)

ROOT = Path(__file__).resolve().parents[1]
REVERSE = {v: k for k, v in _KEYS.items()}


def from_pi(value):
    """Pi wire message (camelCase) to the Python message dictionary form."""
    if isinstance(value, list):
        return [from_pi(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {
        REVERSE.get(k, k): v if k in {"arguments", "parameters", "details", "usage"} else from_pi(v)
        for k, v in value.items()
    }
    for k in ("type", "role", "stop_reason"):
        if k in result:
            result[k] = REVERSE.get(result[k], result[k])
    if "toolName" in result:
        result["name"] = result.pop("toolName")
    if "tools_removed" in result:
        result["tools_removed"] = [t["name"] for t in result["tools_removed"]]
    return result


API = {
    "anthropic": "anthropic-messages",
    "openai-codex": "openai-codex-responses",
    "openai-completions": "openai-completions",
}
# Chat Completions fixtures record these too; other fixtures keep their original set.
CHAT_HEADERS = {"session_id", "x-session-affinity", "x-session-id"}


def model_provider(provider, descriptor):
    """The model's provider name: a Chat Completions model may name its own service."""
    if provider == "openai-completions":
        return (descriptor or {}).get("provider", provider)
    return provider


def catalog_for(provider, descriptor):
    """A catalog holding the fixture's model record, as the upstream runner receives it.

    Without one, both sides use the runner's default record for `test-model`.
    """
    catalog = ModelCatalog()
    if provider != "proxy":
        descriptor = descriptor or {
            "id": "test-model",
            "api": API.get(provider, "openai-responses"),
        }
        model_id = descriptor["id"]
        catalog.register(
            ModelInfo(
                id=model_id,
                provider=model_provider(provider, descriptor),
                api=descriptor.get("api", API.get(provider, "")),
                name=descriptor.get("name", model_id),
                context_window=descriptor.get("contextWindow", 100000),
                max_tokens=descriptor.get("maxTokens", 4096),
                reasoning=descriptor.get("reasoning", True),
                input=tuple(descriptor.get("input", ["text", "image"])),
                thinking_level_map=descriptor.get("thinkingLevelMap", {}),
                cost=descriptor.get("cost", {}),
                compat=descriptor.get("compat", {}),
            )
        )
    return catalog


async def main():
    manifest = json.loads((ROOT / "reference/provider-source-manifest.json").read_text())
    for item in manifest["source_files"]:
        data = (ROOT / "reference/pi" / item["path"]).read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise RuntimeError(f"Upstream source changed: {item['path']}")
    results = []
    for path in sorted((ROOT / "compat/provider-fixtures").glob("*.json")):
        fixture = json.loads(path.read_text())
        chat = fixture["provider"] == "openai-completions"
        if chat:
            # Chat Completions streams bare data lines and ends with [DONE].
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in fixture["events"])
            data += "data: [DONE]\n\n"
        else:
            data = "".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                for e in fixture["events"]
            )
        captured = {}

        def respond(request):
            headers = {
                k: v
                for k, v in request.headers.items()
                if k
                in {
                    "authorization",
                    "x-api-key",
                    "anthropic-version",
                    "anthropic-beta",
                    "content-type",
                    "chatgpt-account-id",
                    "originator",
                    "openai-beta",
                    "session-id",
                    "x-client-request-id",
                    "x-app",
                    "anthropic-dangerous-direct-browser-access",
                    *(CHAT_HEADERS if chat else ()),
                }
            }
            captured.update(url=str(request.url), body=json.loads(request.content), headers=headers)
            return httpx.Response(200, content=data)

        descriptor = fixture.get("model", {})
        model_id = descriptor.get("id", "test-model")
        catalog = catalog_for(fixture["provider"], descriptor)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            transport = HTTPTransport(client, max_retries=0)
            if chat:
                provider = OpenAICompletionsProvider(
                    api_key=fixture.get("api_key") or "fixture",
                    transport=transport,
                    catalog=catalog,
                    base_url=fixture.get("base_url") or "https://fixture.test",
                    name=model_provider("openai-completions", descriptor),
                )
            elif fixture["provider"] == "proxy":
                provider = ProxyProvider(
                    model={"id": "test-model", "provider": "openai", "api": "openai-responses"},
                    proxy_url="https://fixture.test",
                    auth_token="fixture",
                    transport=transport,
                )
            else:
                provider = (
                    AnthropicProvider
                    if fixture["provider"] == "anthropic"
                    else OpenAICodexProvider
                    if fixture["provider"] == "openai-codex"
                    else OpenAIProvider
                )(
                    api_key=fixture.get("api_key")
                    or ("sk-ant-oat-fixture" if fixture.get("oauth") else "fixture"),
                    transport=transport,
                    catalog=catalog,
                    base_url="https://fixture.test/codex"
                    if fixture["provider"] == "openai-codex"
                    else "https://fixture.test",
                )
            context = fixture.get("context", {})
            messages = [message_from_dict(from_pi(m)) for m in context.get("messages", [])] or [
                UserMessage("go", timestamp=0)
            ]
            # Like Agent requests: transcript declarations decide the current tools.
            tools = current_tools(messages) or [
                ToolDeclaration(t["name"], t["description"], t["parameters"])
                for t in context.get("tools", [])
            ]
            names = {
                "maxTokens": "max_tokens",
                "cacheRetention": "cache_retention",
                "sessionId": "session_id",
                "reasoningEffort": "reasoning",
                "toolChoice": "tool_choice",
                "reasoning": "reasoning",
                "thinkingBudgets": "thinking_budgets",
                "thinkingDisplay": "thinking_display",
                "samplingParams": "sampling_params",
            }
            options = {names.get(k, k): v for k, v in fixture.get("options", {}).items()}
            # Provider-specific options on the upstream `stream` entry have no single
            # Python spelling; such fixtures state the Python options explicitly.
            options.update(fixture.get("python_options", {}))
            events = [
                event
                async for event in provider.stream(
                    ModelRequest(messages, tools=tools, model=model_id, options=options),
                    CancelToken(),
                )
            ]
            message = message_to_dict(events[-1].message)
            actual = {"content": message["content"]}
            if message.get("provider_thinking_level"):
                actual["provider_thinking_level"] = message["provider_thinking_level"]
            actual["stop_reason"] = message["stop_reason"]
            rows = []
            for event in events:
                row = {"type": event.type}
                if event.type not in {"start", "done", "error"}:
                    row["index"] = event.index
                if event.type.endswith("_delta"):
                    row["delta"] = event.delta
                if event.content is not None:
                    row["content"] = event.content
                if event.type == "toolcall_end":
                    row["block"] = message_to_dict(event.partial)["content"][event.index]
                if event.reason:
                    row["reason"] = event.reason
                rows.append(row)
            actual["events"] = rows
            if fixture["provider"] != "proxy":
                actual["request"] = captured
            if chat:
                actual["meta"] = {
                    "usage": message["usage"],
                    **{
                        k: message.get(k)
                        for k in ("response_id", "response_model", "raw_stop_reason")
                    },
                }
        upstream = subprocess.run(
            [
                "node",
                "--import",
                "./reference/register.mjs",
                "./reference/provider-runner.ts",
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        expected = (
            json.loads(upstream.stdout) if upstream.returncode == 0 else {"error": upstream.stderr}
        )
        # Signatures are opaque JSON; compare exact serialized strings too.
        result = {
            "fixture": path.name,
            "python": actual,
            "upstream": expected,
            "passed": actual == expected,
            "upstream_exit_code": upstream.returncode,
        }
        results.append(result)
        print(path.name, "passed" if result["passed"] else "FAILED", flush=True)
    report = {
        "source_revision": "a13d35a742c6ef8462812a28fbe1d8c8b7431c32",
        "scope": "final content, signatures, stop reason, canonical event order/payload, complete request body and selected semantic headers; synthetic fixtures, not live service; runtime IDs and SDK fingerprint headers excluded; Chat Completions fixtures also compare token usage, response ID/model and raw finish reason",
        "results": results,
        "status": "passed" if all(r["passed"] for r in results) else "failed",
    }
    (ROOT / "compat/results/provider-conformance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
