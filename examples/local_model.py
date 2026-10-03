"""Use a local model server through its OpenAI-compatible Chat Completions API.

Works with Ollama, vLLM, llama.cpp server, LM Studio, SGLang and hosted services that
speak the same API. Needs only ``pip install pi-python-core``.

    python examples/local_model.py                       # offline, with a stand-in server
    python examples/local_model.py --base-url http://localhost:11434/v1 --model qwen3:8b
    python examples/local_model.py --base-url http://localhost:8000/v1 \\
        --model Qwen/Qwen3-8B --context-window 32768       # vLLM

Tool calling needs a model and server configuration that support it (for vLLM, start the
server with --enable-auto-tool-choice and a --tool-call-parser for the model).
"""

import argparse
import json

from pi_python import Agent, tool
from pi_python.providers import HTTPTransport, OpenAICompletionsProvider


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def stand_in_transport() -> HTTPTransport:
    """Answers like a local server would: first a tool call, then the final text."""
    import httpx

    def chunk(delta, finish=None):
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        return f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'choices': [choice]})}\n\n"

    def handle(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            body = chunk(
                {"role": "assistant", "content": f"The sum is {messages[-1]['content']}."}, "stop"
            )
        else:
            call = {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a": 19, "b": 23}'},
            }
            body = chunk({"role": "assistant", "tool_calls": [call]}) + chunk({}, "tool_calls")
        return httpx.Response(
            200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    return HTTPTransport(httpx.AsyncClient(transport=httpx.MockTransport(handle)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", help="The server's API root, ending in /v1")
    parser.add_argument("--model", default="qwen3:8b", help="The model id the server knows")
    parser.add_argument("--api-key", help="Only if the server requires one")
    parser.add_argument("--context-window", type=int, default=32768)
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    if args.base_url:
        llm = OpenAICompletionsProvider(base_url=args.base_url, name="local", api_key=args.api_key)
    else:
        llm = OpenAICompletionsProvider(
            base_url="http://localhost:11434/v1", name="local", transport=stand_in_transport()
        )
    # A local model is declared once: its id, context window and output limit.
    model = llm.model(args.model, context_window=args.context_window, max_tokens=args.max_tokens)
    agent = Agent(
        provider=llm, model=model, system_prompt="Use the tools for arithmetic.", tools=[add]
    )
    result = agent.prompt_sync("What is 19 + 23?")
    print("status:", result.status, *(result.errors or []))
    print("answer:", "".join(getattr(b, "text", "") for b in result.messages[-1].content))


if __name__ == "__main__":
    main()
