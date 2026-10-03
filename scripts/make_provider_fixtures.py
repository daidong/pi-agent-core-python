"""Write the session-change provider fixtures (0.4). Synthetic inputs only.

Each fixture pins a model record copied from the bundled catalog, so the pinned upstream
adapter and the Python adapter see the same capabilities. They use the `streamSimple`
entry that pi-agent-core calls, with a reasoning level instead of provider options.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "compat/provider-fixtures"
CATALOG = {
    (m["provider"], m["id"]): m
    for m in json.loads((ROOT / "src/pi_python/data/models.json").read_text())["models"]
}
CODEX_KEY = json.loads((OUT / "codex-text.json").read_text())["api_key"]
TEXT_EVENTS = {
    name: json.loads((OUT / f"{source}-text.json").read_text())["events"]
    for name, source in [("anthropic", "anthropic"), ("openai", "openai"), ("codex", "codex")]
}


def model(provider, model_id):
    record = CATALOG[(provider, model_id)]
    keys = ("id", "name", "api", "reasoning", "input", "maxTokens", "contextWindow", "cost")
    descriptor = {k: record[k] for k in keys}
    for key in ("thinkingLevelMap", "compat"):
        if record.get(key):
            descriptor[key] = record[key]
    return descriptor


def tool(name, description=None):
    return {
        "name": name,
        "description": description or f"{name} tool",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def system(content, t, **fields):
    return {"role": "system", "content": content, "timestamp": t, **fields}


def user(content, t):
    return {"role": "user", "content": content, "timestamp": t}


def assistant(provider, api, model_id, content, stop="stop", t=0, **fields):
    return {
        "role": "assistant",
        "content": content,
        "api": api,
        "provider": provider,
        "model": model_id,
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0},
        "stopReason": stop,
        "timestamp": t,
        **fields,
    }


def result(call_id, name, content, t, error=False):
    return {
        "role": "toolResult",
        "toolCallId": call_id,
        "toolName": name,
        "content": content,
        "isError": error,
        "timestamp": t,
    }


def text(value, **fields):
    return {"type": "text", "text": value, **fields}


def call(call_id, name, arguments=None):
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments or {}}


BASE, LATE = tool("base_tool"), tool("late_tool")
SECTIONS = {"rules": "<rules>\nold rules\n</rules>", "docs": "<docs>\nread docs\n</docs>"}
UPDATE = system(
    "updated guidance",
    5,
    sections={"rules": "<rules>\nnew rules\n</rules>", "docs": None},
    toolsRemoved=[{"name": "base_tool"}],
    toolsAdded=[LATE],
)
ADDITION = system("updated guidance", 5, toolsAdded=[LATE])
CLAUDE = ("anthropic", "anthropic-messages")
REASONING_ITEM = json.dumps(
    {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "think"}],
        "encrypted_content": "enc",
    },
    separators=(",", ":"),
)


def anthropic_fallback_events():
    events = json.loads(json.dumps(TEXT_EVENTS["anthropic"]))
    events[0]["message"]["model"] = "claude-opus-4-8"
    for event in events[1:4]:
        event["index"] = 1
    return [
        events[0],
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "fallback", "model": "claude-opus-4-8"},
        },
        {"type": "content_block_stop", "index": 0},
        *events[1:],
    ]


FIXTURES = {
    # Native system message with tool_removal/tool_addition, plus effort markers.
    "session-anthropic-native-tools": {
        "provider": "anthropic",
        "model": model("anthropic", "claude-opus-5-5"),
        "options": {"reasoning": "medium"},
        "context": {
            "messages": [
                system("base prompt", 0, sections=SECTIONS, toolsAdded=[BASE]),
                user("before", 1),
                assistant(
                    *CLAUDE,
                    "claude-opus-5-5",
                    [
                        {"type": "thinking", "thinking": "plan", "thinkingSignature": "sig-1"},
                        text("calling"),
                        call("toolu_1", "base_tool"),
                    ],
                    "toolUse",
                    2,
                    providerThinkingLevel="low",
                ),
                result("toolu_1", "base_tool", [text("done")], 3),
                UPDATE,
                user("after", 6),
            ]
        },
    },
    # A same-name redefinition cannot be expressed natively: send the current list.
    "session-anthropic-redefinition": {
        "provider": "anthropic",
        "model": model("anthropic", "claude-opus-4-8"),
        "options": {"reasoning": "high", "cacheRetention": "none"},
        "context": {
            "messages": [
                system("base prompt", 0, toolsAdded=[BASE]),
                user("before", 1),
                system(
                    "updated guidance",
                    2,
                    toolsRemoved=[{"name": "base_tool"}],
                    toolsAdded=[tool("base_tool", "changed")],
                ),
                user("go", 3),
            ]
        },
    },
    # No mid-conversation support: fold updates into the prompt; budget thinking.
    "session-anthropic-collapse-budget": {
        "provider": "anthropic",
        "model": model("anthropic", "claude-sonnet-4-5"),
        "options": {"reasoning": "low", "maxTokens": 4000, "cacheRetention": "long"},
        "context": {
            "messages": [
                system("base prompt", 0, sections=SECTIONS, toolsAdded=[BASE]),
                user("before", 1),
                UPDATE,
                user("after", 6),
            ]
        },
    },
    # Thinking off, plain-string users, skipped failed turn, grouped tool results.
    "session-anthropic-thinking-off": {
        "provider": "anthropic",
        "model": model("anthropic", "claude-sonnet-4-5"),
        "options": {
            "temperature": 0.2,
            "toolChoice": "auto",
            "metadata": {"user_id": "u1", "other": 1},
        },
        "context": {
            "messages": [
                system("Be brief", 0, toolsAdded=[tool("echo")]),
                user("first", 1),
                assistant(*CLAUDE, "claude-sonnet-4-5", [text("partial")], "error", 2),
                user("   ", 3),
                user("second", 4),
                assistant(
                    *CLAUDE,
                    "claude-sonnet-4-5",
                    [call("toolu_a", "echo", {"x": 1}), call("toolu_b", "echo", {"x": 2})],
                    "toolUse",
                    5,
                ),
                result("toolu_a", "echo", [text("one"), text("two")], 6),
                result(
                    "toolu_b",
                    "echo",
                    [{"type": "image", "data": "aW1n", "mimeType": "image/png"}],
                    7,
                ),
                user([text("third"), text(" ")], 8),
            ]
        },
    },
    # OAuth tool names, managed effort history from this and another provider.
    "session-anthropic-oauth-effort": {
        "provider": "anthropic",
        "oauth": True,
        "model": model("anthropic", "claude-opus-5-5"),
        "options": {"reasoning": "xhigh"},
        "context": {
            "messages": [
                system("sys", 0, toolsAdded=[tool("read"), tool("custom_tool")]),
                user("one", 1),
                assistant(
                    *CLAUDE, "claude-opus-5-5", [text("a")], "stop", 2, providerThinkingLevel="low"
                ),
                user("two", 3),
                assistant(
                    "other-provider",
                    "anthropic-messages",
                    "claude-opus-5-5",
                    [text("b")],
                    "stop",
                    4,
                    providerThinkingLevel="low",
                ),
                user("three", 5),
                system("", 6, toolsAdded=[tool("grep")]),
            ]
        },
    },
    # Server-side fallback: declared fallback models and a skipped fallback block.
    "session-anthropic-server-fallback": {
        "provider": "anthropic",
        "model": model("anthropic", "claude-fable-5"),
        "options": {"reasoning": "high"},
        "events": anthropic_fallback_events(),
    },
    # Anchored additional_tools; same-provider history from another model.
    "session-openai-additional-tools": {
        "provider": "openai",
        "model": model("openai", "gpt-5.5"),
        "options": {"reasoning": "high", "sessionId": "session-1"},
        "context": {
            "messages": [
                system("base prompt", 0, toolsAdded=[BASE]),
                user("before", 1),
                assistant(
                    "openai",
                    "openai-responses",
                    "gpt-5.4",
                    [
                        {
                            "type": "thinking",
                            "thinking": "old",
                            "thinkingSignature": REASONING_ITEM,
                        },
                        text("older", textSignature='{"v":1,"id":"msg_old"}'),
                        call("call_9|fc_9", "base_tool"),
                    ],
                    "toolUse",
                    2,
                ),
                result("call_9|fc_9", "base_tool", [text("nine")], 3),
                assistant(
                    "openai",
                    "openai-responses",
                    "gpt-5.5",
                    [
                        {
                            "type": "thinking",
                            "thinking": "think",
                            "thinkingSignature": REASONING_ITEM,
                        },
                        text(
                            "calling",
                            textSignature='{"v":1,"id":"msg_1","phase":"commentary"}',
                        ),
                        call("call_1|fc_1", "base_tool", {"q": "ü"}),
                    ],
                    "toolUse",
                    4,
                ),
                result("call_1|fc_1", "base_tool", [text("done")], 5),
                ADDITION,
                user("after", 6),
            ]
        },
    },
    # Codex: tool search loading, instructions from the first prompt, foreign history.
    "session-codex-tool-search": {
        "provider": "openai-codex",
        "api_key": CODEX_KEY,
        "model": model("openai-codex", "gpt-5.5"),
        "options": {"reasoning": "minimal", "sessionId": "codex-session"},
        "events": TEXT_EVENTS["codex"],
        "context": {
            "messages": [
                system("base prompt", 0, toolsAdded=[BASE]),
                user("before", 1),
                assistant(
                    *CLAUDE,
                    "claude-opus-5-5",
                    [
                        {"type": "thinking", "thinking": "reason", "thinkingSignature": "opaque"},
                        text("hello"),
                        call("toolu_abc.def", "base_tool"),
                    ],
                    "toolUse",
                    2,
                ),
                result("toolu_abc.def", "base_tool", [text("ok")], 3),
                ADDITION,
                user("after", 6),
            ]
        },
    },
    # A removal cannot be anchored: complete current tools, updates stay in place.
    "session-openai-removal": {
        "provider": "openai",
        "model": model("openai", "gpt-5.5"),
        "context": {
            "messages": [
                system("base prompt", 0, sections=SECTIONS, toolsAdded=[BASE]),
                user("before", 1),
                UPDATE,
                user("after", 6),
            ]
        },
    },
    # A non-reasoning model without mid-conversation support: system role, collapsed.
    "session-openai-collapse": {
        "provider": "openai",
        "model": model("openai", "gpt-4.1"),
        "options": {"maxTokens": 10, "reasoning": "high"},
        "context": {
            "messages": [
                system("base prompt", 0, sections=SECTIONS, toolsAdded=[BASE]),
                user("before", 1),
                UPDATE,
                user("after", 6),
            ]
        },
    },
    # Explicit prompt-cache mode: none asks for explicit caching, long for a 30m TTL.
    "session-openai-explicit-cache-none": {
        "provider": "openai",
        "model": model("openai", "gpt-5.6-luna"),
        "options": {"cacheRetention": "none", "sessionId": "ignored"},
    },
    "session-openai-explicit-cache-long": {
        "provider": "openai",
        "model": model("openai", "gpt-5.6-luna"),
        "options": {"cacheRetention": "long", "sessionId": "s" * 70, "reasoning": "max"},
    },
}


def codex_call_turn(response_id):
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "add"}
    done = {**item, "arguments": '{"a":2,"b":3}'}
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"a":2,"b":3}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": [done], "usage": {}},
        },
    ]


def codex_text_turn(response_id):
    item = {"type": "message", "id": f"msg_{response_id}", "role": "assistant", "content": []}
    done = {**item, "content": [{"type": "output_text", "text": "5", "annotations": []}]}
    return [
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "5"},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": [done], "usage": {}},
        },
    ]


ADD = {
    "name": "add",
    "description": "Add",
    "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
}
WEBSOCKET_FIXTURES = {
    # Tool loop, then a native tool addition: every later request is a delta.
    "codex-continuation-tool-loop": {
        "turns": [
            [system("Use the tool.", 0, toolsAdded=[ADD]), user("add 2 and 3", 1)],
            [result("call_1|fc_1", "add", [text("5")], 3)],
            [ADDITION, user("again", 6)],
        ],
        "responses": [
            codex_call_turn("resp_1"),
            codex_text_turn("resp_2"),
            codex_text_turn("resp_3"),
        ],
    },
    # The server lost the previous response: one full-context retry on a new socket.
    "codex-continuation-lost-state": {
        "turns": [
            [system("Be brief.", 0), user("one", 1)],
            [user("two", 3)],
            [user("three", 5)],
        ],
        "responses": [
            codex_text_turn("resp_1"),
            [{"type": "error", "error": {"code": "previous_response_not_found"}}],
            codex_text_turn("resp_3"),
            codex_text_turn("resp_4"),
        ],
    },
}


# Chat Completions (openai-completions) fixtures: local servers and hosted services.
# Each model descriptor is declared here, as a user would for a server outside the
# catalog; `provider` inside it is the model's provider name, `base_url` the endpoint.
FREE = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
CHAT = "openai-completions"


def chat_model(model_id, provider, reasoning, inputs, context, max_tokens, **fields):
    return {
        "id": model_id,
        "name": model_id,
        "api": CHAT,
        "provider": provider,
        "reasoning": reasoning,
        "input": inputs,
        "contextWindow": context,
        "maxTokens": max_tokens,
        "cost": FREE,
        **fields,
    }


def chunk(model_id, delta=None, finish=None, *, chunk_id="chatcmpl-1", usage=None, empty=False):
    """One streamed chat.completion.chunk; `empty` is the final usage-only chunk."""
    value = {"id": chunk_id, "object": "chat.completion.chunk", "created": 0, "model": model_id}
    value["choices"] = (
        [] if empty else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    )
    if usage is not None:
        value["usage"] = usage
    return value


def chat_text(model_id, text_value, usage, finish="stop"):
    return [
        chunk(model_id, {"role": "assistant", "content": ""}),
        chunk(model_id, {"content": text_value}),
        chunk(model_id, {}, finish),
        chunk(model_id, usage=usage, empty=True),
    ]


def schema(**properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


ADD_TOOL = {"name": "add", "description": "Add", "parameters": schema(a={"type": "integer"})}
WEATHER = {
    "name": "weather",
    "description": "Weather for a city",
    "parameters": schema(city={"type": "string"}),
}
SHOT = {"name": "screenshot", "description": "Capture the screen", "parameters": schema()}
IMAGE = {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}
REASONING = {"type": "thinking", "thinking": "plan", "thinkingSignature": "reasoning_content"}
OLLAMA = chat_model(
    "qwen3:8b",
    "ollama",
    False,
    ["text"],
    40960,
    8192,
    compat={"supportsDeveloperRole": False, "supportsReasoningEffort": False},
)
VLLM = chat_model(
    "Qwen/Qwen3-8B", "vllm", False, ["text"], 32768, 4096, compat={"supportsDeveloperRole": False}
)
LLAMACPP = chat_model(
    "gpt-oss-20b",
    "llamacpp",
    True,
    ["text"],
    32768,
    16384,
    compat={
        "supportsDeveloperRole": False,
        "thinkingFormat": "chat-template",
        "chatTemplateKwargs": {
            "enable_thinking": {"$var": "thinking.enabled"},
            "reasoning_effort": {"$var": "thinking.effort", "omitWhenOff": True},
            "budget": {"$var": "thinking.budget"},
            "keep": "yes",
        },
        "thinkingTokenBudgetField": "thinking_budget_tokens",
    },
)
QWEN_VLLM = chat_model(
    "Qwen/Qwen3-32B",
    "vllm",
    True,
    ["text"],
    40960,
    8192,
    compat={
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "thinkingFormat": "qwen-chat-template",
        "supportsThinkingTokenBudget": True,
        "vllmPriority": 5,
    },
)
GENERIC = chat_model("test-model", CHAT, True, ["text", "image"], 100000, 4096)
STRICT = chat_model(
    "mistral-small",
    CHAT,
    True,
    ["text"],
    32000,
    4096,
    compat={
        "requiresAssistantAfterToolResult": True,
        "requiresToolResultName": True,
        "requiresThinkingAsText": True,
        "maxTokensField": "max_tokens",
        "supportsStore": False,
        "supportsUsageInStreaming": False,
        "supportsStrictMode": True,
        "supportsFinishReason": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
    },
)
OPENROUTER = chat_model(
    "anthropic/claude-sonnet-4.5",
    "openrouter",
    True,
    ["text", "image"],
    200000,
    64000,
    compat={"openRouterRouting": {"order": ["anthropic"], "allow_fallbacks": False}},
)
DEEPSEEK = chat_model("deepseek-reasoner", "deepseek", True, ["text"], 128000, 32000)
KIMI = chat_model(
    "kimi-k2",
    CHAT,
    True,
    ["text"],
    128000,
    8192,
    compat={
        "supportsMidConvoSystemMessages": True,
        "supportsMidConvoToolAdditions": True,
        "thinkingFormat": "qwen",
        "zaiToolStream": True,
    },
)
OR_DETAILS = json.dumps(
    [
        {
            "type": "reasoning.text",
            "text": "earlier",
            "signature": "sig-0",
            "format": "anthropic-claude-v1",
            "index": 0,
        }
    ],
    separators=(",", ":"),
)


def own(model_record, content, stop="stop", t=0):
    return assistant(model_record["provider"], CHAT, model_record["id"], content, stop, t)


def tool_delta(index, arguments, call_id=None, name=None):
    call = {"index": index, "function": {"arguments": arguments}}
    if call_id:
        call = {"index": index, "id": call_id, "type": "function", "function": {"name": name}}
        call["function"]["arguments"] = arguments
    return {"tool_calls": [call]}


COMPLETIONS_FIXTURES = {
    # Ollama: keyless-style local endpoint, system role, sampling parameters, usage chunk.
    "completions-text": {
        "base_url": "http://localhost:11434/v1",
        "model": OLLAMA,
        "options": {"temperature": 0.2, "samplingParams": {"top_k": 20, "min_p": 0}},
        "context": {"messages": [system("You are terse.", 0), user("Say hi", 1)]},
        "events": chat_text(
            "qwen3:8b", "Hello", {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}
        ),
    },
    # vLLM: two streamed tool calls whose arguments are split across deltas.
    "completions-tool-stream": {
        "base_url": "http://localhost:8000/v1",
        "model": VLLM,
        "options": {"toolChoice": "auto"},
        "context": {
            "messages": [
                system("Use tools.", 0, toolsAdded=[ADD_TOOL, WEATHER]),
                user("add 2 and 3; weather in Paris", 1),
            ]
        },
        "events": [
            chunk("Qwen/Qwen3-8B", {"role": "assistant", "content": ""}),
            chunk("Qwen/Qwen3-8B", {"content": "Calling tools."}),
            chunk("Qwen/Qwen3-8B", tool_delta(0, "", "chatcmpl-tool-a1", "add")),
            chunk("Qwen/Qwen3-8B", tool_delta(0, '{"a": 2')),
            chunk("Qwen/Qwen3-8B", tool_delta(0, ', "b": 3}')),
            chunk("Qwen/Qwen3-8B", tool_delta(1, '{"city": ', "chatcmpl-tool-b2", "weather")),
            chunk("Qwen/Qwen3-8B", tool_delta(1, '"Paris"}')),
            chunk("Qwen/Qwen3-8B", {}, "tool_calls"),
            chunk(
                "Qwen/Qwen3-8B",
                usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
                empty=True,
            ),
        ],
    },
    # llama.cpp: reasoning_content stream and replay, chat-template kwargs, budget field.
    "completions-reasoning-content": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": LLAMACPP,
        "options": {"reasoning": "medium"},
        "context": {
            "messages": [
                system("Think first.", 0),
                user("2+2?", 1),
                own(LLAMACPP, [REASONING, text("4")], t=2),
                user("and 3+1?", 3),
            ]
        },
        "events": [
            chunk("gpt-oss-20b", {"role": "assistant", "reasoning_content": "Let me"}),
            chunk("gpt-oss-20b", {"reasoning_content": " think."}),
            chunk("gpt-oss-20b", {"content": "Answer: 4"}),
            chunk(
                "gpt-oss-20b",
                {},
                "stop",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 9,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            ),
        ],
    },
    # vLLM Qwen3: `reasoning` field, qwen chat template, token budget, priority, length stop.
    "completions-qwen-length": {
        "base_url": "http://localhost:8000/v1",
        "model": QWEN_VLLM,
        "options": {"reasoning": "low", "maxTokens": 3000},
        "context": {"messages": [user("Write a long story", 0)]},
        "events": [
            chunk("Qwen/Qwen3-32B", {"role": "assistant", "reasoning": "Plot"}),
            chunk("Qwen/Qwen3-32B", {"content": "Once upon"}),
            chunk("Qwen/Qwen3-32B", {"content": " a time"}, "length"),
            chunk(
                "Qwen/Qwen3-32B",
                usage={"prompt_tokens": 12, "completion_tokens": 3000, "total_tokens": 3012},
                empty=True,
            ),
        ],
    },
    # Default compat: tools, user image, same-model and foreign replay, tool-result image,
    # a skipped failed turn, Responses call IDs normalized for Chat Completions.
    "completions-request-replay": {
        "model": GENERIC,
        "options": {"reasoning": "high", "sessionId": "sess-1"},
        "context": {
            "messages": [
                system("Be concise.", 0, toolsAdded=[ADD_TOOL, SHOT]),
                user([text("look"), IMAGE], 1),
                own(
                    GENERIC,
                    [REASONING, text("Calling add"), call("call_1", "add", {"a": 2})],
                    "toolUse",
                    2,
                ),
                result("call_1", "add", [text("2")], 3),
                user("now screenshot", 4),
                assistant(
                    "openai",
                    "openai-responses",
                    "gpt-5",
                    [
                        {
                            "type": "thinking",
                            "thinking": "considering",
                            "thinkingSignature": REASONING_ITEM,
                        },
                        text("ok", textSignature='{"v":1,"id":"msg_1"}'),
                        call("call_9|fc_abc+/=", "screenshot"),
                    ],
                    "toolUse",
                    5,
                ),
                result("call_9|fc_abc+/=", "screenshot", [text("here"), IMAGE], 6),
                own(GENERIC, [text("partial")], "error", 7),
                user("describe it", 8),
            ]
        },
        "events": chat_text(
            "test-model",
            "Done.",
            {
                "prompt_tokens": 300,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        ),
    },
    # A strict endpoint: bridge messages, tool result names, thinking as text, max_tokens,
    # no store or usage request, strict tools, and no finish_reason (inferred tool use).
    "completions-compat-strict": {
        "model": STRICT,
        "context": {
            "messages": [
                system("Use tools.", 0, toolsAdded=[ADD_TOOL]),
                user("add one", 1),
                own(
                    STRICT,
                    [REASONING, text("using tool"), call("call_a", "add", {"a": 1})],
                    "toolUse",
                    2,
                ),
                result("call_a", "add", [text("1")], 3),
                user("again", 4),
            ]
        },
        "events": [
            chunk(
                "mistral-small",
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "add", "arguments": '{"a":1}'},
                        }
                    ],
                },
            ),
        ],
    },
    # OpenRouter: detected compat, reasoning object, routing, x-session-id, long cache with
    # Anthropic cache_control, reasoning_details merge and replay, cache read/write usage.
    "completions-openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": OPENROUTER,
        "options": {"reasoning": "medium", "sessionId": "or-session", "cacheRetention": "long"},
        "context": {
            "messages": [
                system("Be helpful.", 0, toolsAdded=[ADD_TOOL]),
                user("q1", 1),
                own(
                    OPENROUTER,
                    [
                        {
                            "type": "thinking",
                            "thinking": "earlier",
                            "thinkingSignature": OR_DETAILS,
                        },
                        text("a1"),
                    ],
                    t=2,
                ),
                user("q2", 3),
            ]
        },
        "events": [
            chunk(
                "anthropic/claude-4.5-sonnet-20250929",
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Thinking",
                    "reasoning_details": [
                        {
                            "type": "reasoning.text",
                            "text": "Thinking",
                            "format": "anthropic-claude-v1",
                            "index": 0,
                        }
                    ],
                },
                chunk_id="gen-abc",
            ),
            chunk(
                "anthropic/claude-4.5-sonnet-20250929",
                {
                    "reasoning": " more",
                    "reasoning_details": [
                        {
                            "type": "reasoning.text",
                            "text": " more",
                            "signature": "sig-1",
                            "index": 0,
                        }
                    ],
                },
                chunk_id="gen-abc",
            ),
            chunk(
                "anthropic/claude-4.5-sonnet-20250929",
                {
                    "reasoning_details": [
                        {
                            "type": "reasoning.encrypted",
                            "data": "enc==",
                            "id": "rd_1",
                            "format": "anthropic-claude-v1",
                            "index": 1,
                        }
                    ]
                },
                chunk_id="gen-abc",
            ),
            chunk(
                "anthropic/claude-4.5-sonnet-20250929", {"content": "Answer"}, chunk_id="gen-abc"
            ),
            chunk("anthropic/claude-4.5-sonnet-20250929", {}, "stop", chunk_id="gen-abc"),
            chunk(
                "anthropic/claude-4.5-sonnet-20250929",
                usage={
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100},
                    "completion_tokens_details": {"reasoning_tokens": 20},
                    "cost": 0.01,
                },
                chunk_id="gen-abc",
                empty=True,
            ),
        ],
    },
    # DeepSeek: detected thinking format, max_tokens, reasoning_content on every replayed
    # assistant message, DeepSeek cache-hit usage fields.
    "completions-deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": DEEPSEEK,
        "options": {"reasoning": "high"},
        "context": {
            "messages": [
                system("Be brief.", 0, toolsAdded=[ADD_TOOL]),
                user("hi", 1),
                own(DEEPSEEK, [text("hello")], t=2),
                user("add 5", 3),
                own(DEEPSEEK, [REASONING, call("call_d", "add", {"a": 5})], "toolUse", 4),
                result("call_d", "add", [text("5")], 5),
                user("thanks", 6),
            ]
        },
        "events": [
            chunk(
                "deepseek-reasoner",
                {"role": "assistant", "reasoning_content": "Simple"},
                chunk_id="ds-1",
            ),
            chunk("deepseek-reasoner", {"content": "You're welcome."}, chunk_id="ds-1"),
            chunk(
                "deepseek-reasoner",
                {},
                "stop",
                chunk_id="ds-1",
                usage={
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "total_tokens": 60,
                    "prompt_cache_hit_tokens": 32,
                    "prompt_cache_miss_tokens": 18,
                    "completion_tokens_details": {"reasoning_tokens": 6},
                },
            ),
        ],
    },
    # Mid-conversation system messages with Kimi-style tool additions, qwen thinking
    # format and z.ai tool streaming flag.
    "completions-midconvo-tools": {
        "model": KIMI,
        "options": {"reasoning": "high"},
        "context": {
            "messages": [
                system("base prompt", 0, toolsAdded=[BASE]),
                user("before", 1),
                ADDITION,
                user("after", 6),
            ]
        },
        "events": chat_text("kimi-k2", "ok", {"prompt_tokens": 9, "completion_tokens": 1}),
    },
}
# Request-shape fixtures for the remaining thinking formats and endpoint detections.
SHORT_USAGE = {"prompt_tokens": 5, "completion_tokens": 1}
FORMAT_FIXTURES = {
    # z.ai detected by name: thinking object; reasoning_effort only when re-enabled.
    "completions-format-zai": (
        chat_model(
            "glm-5",
            "zai",
            True,
            ["text"],
            128000,
            8192,
            thinkingLevelMap={"high": "deep"},
            compat={"supportsReasoningEffort": True},
        ),
        {"reasoning": "high"},
        None,
    ),
    # Together detected by URL: reasoning {enabled} with thinking off.
    "completions-format-together": (
        chat_model("deepseek-ai/DeepSeek-R1", "together-custom", True, ["text"], 64000, 8192),
        {},
        "https://api.together.xyz/v1",
    ),
    # Baseten template arguments; the off level maps to an explicit effort.
    "completions-format-baseten": (
        chat_model(
            "baseten-model",
            CHAT,
            True,
            ["text"],
            64000,
            8192,
            thinkingLevelMap={"off": "none"},
            compat={
                "thinkingFormat": "baseten",
                "chatTemplateArgs": {
                    "enable_thinking": {"$var": "thinking.enabled"},
                    "effort": {"$var": "thinking.effort"},
                },
            },
        ),
        {},
        None,
    ),
    # String thinking for an opt-in level; no cache key or affinity with retention none.
    "completions-format-string-thinking": (
        chat_model(
            "string-model",
            CHAT,
            True,
            ["text"],
            64000,
            8192,
            thinkingLevelMap={"xhigh": "max"},
            compat={"thinkingFormat": "string-thinking", "sendSessionAffinityHeaders": True},
        ),
        {"reasoning": "xhigh", "cacheRetention": "none", "sessionId": "s1"},
        None,
    ),
    # Ant Ling detected by name: reasoning effort only for a mapped level.
    "completions-format-ant-ling": (
        chat_model(
            "ling-1t", "ant-ling", True, ["text"], 64000, 8192, thinkingLevelMap={"low": "low"}
        ),
        {"reasoning": "minimal"},
        None,
    ),
    # api.openai.com: prompt cache key; affinity headers without session_id; off effort.
    "completions-openai-off": (
        chat_model(
            "gpt-4.1-chat",
            "openai",
            True,
            ["text"],
            128000,
            8192,
            thinkingLevelMap={"off": "none"},
            compat={
                "sendSessionAffinityHeaders": True,
                "sessionAffinityFormat": "openai-nosession",
            },
        ),
        {"sessionId": "openai-session"},
        "https://api.openai.com/v1",
    ),
}
for _name, (_model, _options, _url) in FORMAT_FIXTURES.items():
    COMPLETIONS_FIXTURES[_name] = {
        **({"base_url": _url} if _url else {}),
        "model": _model,
        "options": _options,
        "context": {"messages": [system("sys", 0), user("hi", 1)]},
        "events": chat_text(_model["id"], "ok", SHORT_USAGE),
    }
# Moonshot/Kimi: usage on the choice, and Kimi's top-level cached_tokens.
COMPLETIONS_FIXTURES["completions-moonshot-usage"] = {
    "base_url": "https://api.moonshot.ai/v1",
    "model": chat_model("kimi-k2-turbo", "moonshotai", False, ["text"], 256000, 16384),
    "context": {"messages": [user("hi", 0)]},
    "events": [
        chunk("kimi-k2-turbo", {"role": "assistant", "content": "hey"}),
        {
            **chunk("kimi-k2-turbo", {}, "stop"),
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 30, "completion_tokens": 2, "cached_tokens": 24},
                }
            ],
        },
    ],
}


def main():
    for name, fixture in WEBSOCKET_FIXTURES.items():
        data = {
            "model": model("openai-codex", "gpt-5.5"),
            "api_key": CODEX_KEY,
            "options": {"sessionId": "ws-session", "reasoning": "medium"},
            **fixture,
        }
        target = ROOT / "compat/websocket-fixtures" / f"{name}.json"
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(name)
    for name, fixture in FIXTURES.items():
        kind = "codex" if fixture["provider"] == "openai-codex" else fixture["provider"]
        data = {"provider": fixture["provider"], "entry": "simple"}
        data.update({k: v for k, v in fixture.items() if k != "provider"})
        data.setdefault("events", TEXT_EVENTS[kind])
        (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(name)
    for name, fixture in COMPLETIONS_FIXTURES.items():
        data = {"provider": CHAT, "entry": "simple", **fixture}
        (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(name)


if __name__ == "__main__":
    main()
