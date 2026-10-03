"""Explicit API/OAuth example. Invoke --help; never imported by the core."""

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import uuid
import webbrowser

try:
    from pi_python import Agent, TextContent
except ModuleNotFoundError as exc:
    raise SystemExit(
        "pi_python is not installed here; run: "
        "uv run --extra providers python examples/provider_chat.py --help"
    ) from exc
from pi_python.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OpenAIProvider,
    OpenAICodexProvider,
    OAuthClient,
    OAuthCredential,
    RefreshingCredentials,
)


def save_private(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pi-oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(record, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "openai-codex", "openai-chatgpt", "deepseek"],
        required=True,
    )
    parser.add_argument("--model", required=True, help="Model ID enabled for this account")
    parser.add_argument("--prompt", default="用一句话说明你能如何协助编程。")
    parser.add_argument(
        "--credential-file",
        type=Path,
        help="App-owned OAuth file (never reuse another client file)",
    )
    parser.add_argument("--login", action="store_true")
    parser.add_argument(
        "--device",
        action="store_true",
        help="Codex device authorization instead of browser callback",
    )
    parser.add_argument(
        "--thinking",
        choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        default="off",
    )
    parser.add_argument(
        "--transport", choices=["sse", "websocket", "websocket-cached", "auto"], default="sse"
    )
    parser.add_argument("--session-id", help="Required for websocket-cached")
    args = parser.parse_args()
    if args.provider == "deepseek" and (args.login or args.credential_file):
        parser.error("DeepSeek uses DEEPSEEK_API_KEY")
    cls = {
        "anthropic": AnthropicProvider,
        "deepseek": DeepSeekProvider,
        "openai": OpenAIProvider,
        "openai-chatgpt": OpenAIProvider,
        "openai-codex": OpenAICodexProvider,
    }[args.provider]
    if args.credential_file:
        path = args.credential_file.expanduser()
        record = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"host_id": "urn:uuid:" + str(uuid.uuid4())}
        )
        auth_provider = "openai-chatgpt" if args.provider == "openai" else args.provider
        oauth = OAuthClient(auth_provider)
        credential = OAuthCredential(**record["credential"]) if record.get("credential") else None

        def persist(value):
            record["credential"] = asdict(value)
            save_private(path, record)

        if args.login:
            save_private(
                path, record
            )  # Preserve host identity even if registration is interrupted.

            def open_browser(url):
                print("请在浏览器完成登录：", url)
                webbrowser.open(url)

            if args.device:
                credential = await oauth.device_login(
                    lambda notice: print(
                        "打开", notice["verification_uri"], "并输入", notice["user_code"]
                    )
                )
            else:
                credential = await oauth.login(
                    open_browser, host_id=record["host_id"], credential=credential
                )
            persist(credential)
        if credential is None:
            parser.error("No saved credential; pass --login on first use")
        provider = cls(credentials=RefreshingCredentials(credential, client=oauth, persist=persist))
    else:
        if args.login or args.provider in {"openai-codex", "openai-chatgpt"}:
            parser.error("Subscription login requires --credential-file")
        env = {"anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(
            args.provider, "OPENAI_API_KEY"
        )
        key = os.environ.get(env)
        if not key:
            parser.error(f"Set {env} for this example")
        provider = cls(api_key=key)
    async with Agent(
        provider=provider,
        model=args.model,
        thinking_level=args.thinking,
        transport=args.transport,
        session_id=args.session_id,
    ) as agent:
        result = await agent.prompt(args.prompt)
        for message in result.messages:
            if message.role == "assistant":
                for block in message.content:
                    if isinstance(block, TextContent):
                        print(block.text)
        await provider.aclose()
        if result.status != "completed":
            raise SystemExit(f"{result.status}: {result.errors}")


if __name__ == "__main__":
    asyncio.run(main())
