"""Explicit live smoke tests. Reads only selected auth sources; never prints credentials.

No refresh of another client's credential, no browser launch, no external tool effects.
Each provider performs bounded requests with synthetic prompts and an in-memory tool.
"""

import argparse
import asyncio
import base64
from collections import Counter
import json
import os
import re
from pathlib import Path
import subprocess
import time
from pi_python import (
    Agent,
    AgentConfigUpdate,
    RunLimits,
    SystemMessage,
    TextContent,
    Tool,
    ToolResult,
    UserMessage,
)
from pi_python.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OpenAICodexProvider,
    OAuthCredential,
)


def local_provider(name):
    if name == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("credential_missing")
        return DeepSeekProvider(api_key=key), "deepseek-flash"
    if name == "codex":
        data = json.loads((Path.home() / ".codex/auth.json").read_text())["tokens"]
        token = data["access_token"]
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        expires = claims.get("exp", 0)
        if expires <= time.time():
            raise RuntimeError("credential_expired")
        credential = OAuthCredential("openai-codex", token, data.get("refresh_token", ""), expires)
        return OpenAICodexProvider(credentials=credential), "gpt-6-luna"
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("credential_missing")
    data = json.loads(result.stdout)["claudeAiOauth"]
    expires = data["expiresAt"] / 1000
    if expires <= time.time():
        raise RuntimeError("credential_expired")
    credential = OAuthCredential(
        "anthropic", data["accessToken"], data.get("refreshToken", ""), expires
    )
    # Capabilities (adaptive thinking, 128k output) come from the catalog record.
    return AnthropicProvider(credentials=credential), "claude-sonnet-4-6"


async def check(name, mode, transport="sse"):
    record = {
        "provider": name,
        "case": mode,
        "live": True,
        "transport": transport,
        "started_at": time.time(),
    }
    effects = []
    statuses = []
    wire_types = Counter()
    event_types = Counter()
    try:
        provider, model = local_provider(name)
        record["model"] = model

        async def add(args, context):
            effects.append(args)
            return ToolResult.text(str(args["a"] + args["b"]), details={"synthetic": True})

        tools = (
            [
                Tool(
                    "add",
                    "Add two integers; no side effects.",
                    {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                        "additionalProperties": False,
                    },
                    add,
                )
            ]
            if mode == "tool"
            else []
        )
        prompt = (
            "Call add exactly once with a=2 and b=3, then reply with the numeric result only."
            if tools
            else "Compute 17 raised to the power 12345, modulo 97. Use modular arithmetic. Reply with only the final integer."
            if mode == "thinking"
            else "Reply with exactly PI_SMOKE_OK."
        )
        options = {
            "max_tokens": 4096 if mode == "thinking" else 256,
            "transport": transport,
            "session_id": "pi-python-live-fixture",
        }
        agent = Agent(
            provider=provider,
            model=model,
            system_prompt="Follow the requested output format exactly. Return only the final answer, without explanation.",
            tools=tools,
            thinking_level="high" if mode == "thinking" else "off",
            options=options,
            limits=RunLimits(max_model_requests=3, max_tool_calls=1, run_timeout=90),
            on_response=lambda response: statuses.append(response["status"]),
            on_provider_stream_event=lambda event: wire_types.update(
                [event.get("type", "unknown")]
            ),
        )
        agent.subscribe(
            lambda event: event_types.update([event.data.get("delta_type", event.type)])
        )
        result = await agent.prompt(prompt)
        answers = [
            b.text
            for m in result.messages
            if m.role == "assistant"
            for b in m.content
            if isinstance(b, TextContent)
        ]
        expected = (
            "5" if tools else str(pow(17, 12345, 97)) if mode == "thinking" else "PI_SMOKE_OK"
        )
        exact_format = bool(answers) and expected == answers[-1].strip()
        numbers = re.findall(r"(?<![A-Za-z0-9])-?\d+", answers[-1]) if answers else []
        matched = exact_format or (mode == "thinking" and bool(numbers) and numbers[-1] == expected)
        record.update(
            status=result.status,
            passed=result.status == "completed"
            and matched
            and (effects == [{"a": 2, "b": 3}] if tools else not effects),
            answer_matches=matched,
            exact_format=exact_format,
            final_numeric_answer=numbers[-1] if mode == "thinking" and numbers else None,
            thinking_observed=any(
                b.type == "thinking"
                for m in result.messages
                if m.role == "assistant"
                for b in m.content
            ),
            tool_executions=len(effects),
            usage=result.usage,
            event_counts=dict(event_types),
            wire_event_counts=dict(wire_types),
            http_statuses=statuses,
        )
        record["stop_reasons"] = [m.stop_reason for m in result.messages if m.role == "assistant"]
        if mode == "thinking" and record["passed"]:
            replay = await agent.prompt("Now reply with exactly PI_SMOKE_OK.")
            replay_text = "".join(
                b.text
                for m in replay.messages[-1:]
                if m.role == "assistant"
                for b in m.content
                if isinstance(b, TextContent)
            )
            record["signed_history_replay_passed"] = (
                replay.status == "completed" and replay_text.strip() == "PI_SMOKE_OK"
            )
            record["passed"] = (
                record["passed"]
                and record["thinking_observed"]
                and record["signed_history_replay_passed"]
            )
            record["replay_usage"] = replay.usage
        record["http_statuses"] = statuses
        record["network"] = provider.transport.stats
        await provider.aclose()
        # Error categories are enough for persisted reports; no provider bodies or tokens.
        record["error_types"] = [error.split(":", 1)[0] for error in result.errors]
        record["diagnostic"] = (
            result.errors[0][:180]
            if result.errors and all(s not in result.errors[0] for s in ["Bearer", "sk-", "eyJ"])
            else None
        )
    except Exception as exc:
        record.update(status="failed", passed=False, error_types=[type(exc).__name__])
    record["seconds"] = round(time.time() - record["started_at"], 2)
    return record


def payload_shape(provider, body):
    """Request structure only: no prompt text, tool output or credentials."""
    if provider == "claude":
        return {
            "tools": [
                t["name"] + (" (deferred)" if t.get("defer_loading") else "")
                for t in body.get("tools", [])
            ],
            "messages": [
                m["role"]
                + (f" effort={m['output_config']['effort']}" if m.get("output_config") else "")
                + (
                    " [" + ",".join(b["type"] for b in m["content"]) + "]"
                    if m["role"] == "system" and m["content"]
                    else ""
                )
                for m in body["messages"]
            ],
            "thinking": body.get("thinking"),
        }
    return {
        "tools": [t["name"] for t in body.get("tools", [])],
        "input": [i.get("type") or i.get("role") for i in body["input"]],
        "reasoning": body.get("reasoning"),
    }


async def check_session(name, transport, model=None):
    """A tool added mid-session, a mid-conversation instruction and an effort change."""
    record = {"provider": name, "case": "session", "live": True, "transport": transport}
    record["started_at"] = time.time()
    effects, payloads, statuses = [], [], []
    try:
        provider, default = local_provider(name)
        model = model or {"claude": "claude-sonnet-5-5", "codex": "gpt-6-luna"}.get(name, default)
        record["model"] = model
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        }

        def arithmetic(label, operation):
            async def execute(args, context):
                effects.append([label, args])
                return ToolResult.text(str(operation(args["a"], args["b"])))

            return Tool(label, f"{label.title()} two integers; no side effects.", schema, execute)

        add = arithmetic("add", lambda a, b: a + b)
        multiply = arithmetic("multiply", lambda a, b: a * b)
        options = {
            "max_tokens": 2048,
            "transport": transport,
            "session_id": "pi-python-session-live",
        }
        agent = Agent(
            provider=provider,
            model=model,
            system_prompt="You are a careful calculator. Use the provided tools for arithmetic.",
            tools=[add],
            thinking_level="low",
            options=options,
            limits=RunLimits(max_model_requests=4, max_tool_calls=2, run_timeout=120),
            on_payload=lambda body: payloads.append(payload_shape(name, body)),
            on_response=lambda response: statuses.append(response["status"]),
        )
        first = await agent.prompt(
            "Call add exactly once with a=2 and b=3, then reply with the numeric result only."
        )
        agent.update_config(AgentConfigUpdate(tools=[add, multiply]))
        if name == "claude":
            # A changed effort is carried by markers; the earlier marker stays in place.
            agent.update_config(AgentConfigUpdate(options={**options, "reasoning": "medium"}))
        second = await agent.prompt(
            [
                SystemMessage("A multiply tool is now available. End every answer with DONE."),
                UserMessage(
                    "Call multiply exactly once with a=4 and b=5, then reply with the result."
                ),
            ]
        )
        answer = "".join(
            b.text for b in second.messages[-1].content if isinstance(b, TextContent)
        ).strip()
        record.update(
            statuses=[first.status, second.status],
            tool_calls=effects,
            answer_has_20="20" in answer,
            answer_ends_with_done=answer.rstrip(".").endswith("DONE"),
            provider_thinking_levels=[
                m.provider_thinking_level
                for m in [*first.messages, *second.messages]
                if m.role == "assistant"
            ],
            request_shapes=payloads,
            http_statuses=statuses,
            network=provider.transport.stats,
            error_types=[e.split(":", 1)[0] for e in first.errors + second.errors],
        )
        shapes = payloads[-1]
        native = (
            any("tool_addition" in m for m in shapes["messages"])
            if name == "claude"
            else "additional_tools" in shapes["input"]
        )
        record["native_change_sent"] = native
        record["passed"] = (
            first.status == second.status == "completed"
            and effects == [["add", {"a": 2, "b": 3}], ["multiply", {"a": 4, "b": 5}]]
            and record["answer_has_20"]
            and native
        )
        if name == "codex" and transport in {"websocket-cached", "auto"}:
            record["passed"] = (
                record["passed"] and record["network"]["websocket_delta_requests"] > 0
            )
        await provider.aclose()
    except Exception as exc:
        record.update(status="failed", passed=False, error_types=[type(exc).__name__])
    record["seconds"] = round(time.time() - record["started_at"], 2)
    return record


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=["deepseek", "claude", "codex"], action="append", required=True
    )
    parser.add_argument(
        "--case", choices=["text", "tool", "thinking", "session"], action="append", default=None
    )
    parser.add_argument("--model", default=None, help="override the model for the session case")
    parser.add_argument(
        "--transport", choices=["sse", "websocket", "websocket-cached", "auto"], default="sse"
    )
    parser.add_argument("--output", default="compat/results/live-providers.json")
    args = parser.parse_args()
    records = []
    for provider in args.provider:
        for case in args.case or ["text", "tool", "thinking"]:
            record = (
                await check_session(provider, args.transport, args.model)
                if case == "session"
                else await check(provider, case, args.transport)
            )
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            Path(args.output).write_text(
                json.dumps(
                    {
                        "scope": "Live synthetic prompts; in-memory tool only; no credentials recorded",
                        "results": records,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
    return int(any(not r["passed"] for r in records))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
