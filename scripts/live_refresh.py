"""Live OAuth refresh check on credential files this library created with --login.

Never use another client's credential file: refresh tokens rotate, so refreshing here
would sign that client out. No token, hash or model text is recorded.
"""

import argparse
import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from pi_python import Agent, TextContent
from pi_python.providers import (
    AnthropicProvider,
    OAuthClient,
    OAuthCredential,
    OpenAICodexProvider,
    RefreshingCredentials,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"anthropic": "claude-sonnet-5-5", "openai-codex": "gpt-6-luna"}
PROVIDERS = {"anthropic": AnthropicProvider, "openai-codex": OpenAICodexProvider}


def example_module():
    """The example's own atomic 0600 writer, so its persistence path is what is tested."""
    spec = importlib.util.spec_from_file_location(
        "provider_chat", ROOT / "examples/provider_chat.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def same(a, b):
    """Saved and in-memory records agree on everything that authorizes a request."""
    return (a.access_token, a.refresh_token, a.expires_at) == (
        b.access_token,
        b.refresh_token,
        b.expires_at,
    )


def load(path):
    record = json.loads(path.read_text())
    return record, OAuthCredential(**record["credential"])


async def ask(provider_name, credentials):
    provider = PROVIDERS[provider_name](credentials=credentials)
    try:
        async with Agent(provider=provider, model=MODELS[provider_name]) as agent:
            result = await agent.prompt("Reply with exactly PI_REFRESH_OK.")
    finally:
        await provider.aclose()
    text = "".join(
        b.text for b in result.messages[-1].content if isinstance(b, TextContent)
    ).strip()
    return result.status == "completed" and text == "PI_REFRESH_OK", result.errors[:1]


async def check(provider_name, path, save_private):
    record_out = {"provider": provider_name, "started_at": time.time()}
    record, original = load(path)
    if original.provider != provider_name:
        raise SystemExit(f"{path} holds a {original.provider} credential")
    client = OAuthClient(provider_name)
    calls = 0
    real_refresh = client.refresh

    async def counted(credential, cancel=None):
        nonlocal calls
        calls += 1
        return await real_refresh(credential, cancel)

    client.refresh = counted  # type: ignore[method-assign]

    def persist(value):
        record["credential"] = asdict(value)
        save_private(path, record)

    # 1. Treat the token as expired; three callers race for it at once.
    expired = replace(original, expires_at=time.time())
    shared = RefreshingCredentials(expired, client=client, persist=persist)
    results = await asyncio.gather(*(shared.get() for _ in range(3)))
    fresh = results[0]
    _, saved = load(path)
    record_out.update(
        concurrent_callers=3,
        network_refreshes=calls,
        callers_got_same_token=all(r.access_token == fresh.access_token for r in results),
        access_token_changed=fresh.access_token != original.access_token,
        refresh_token_rotated=fresh.refresh_token != original.refresh_token,
        minutes_valid_after_refresh=round((fresh.expires_at - time.time()) / 60),
        file_matches_memory=same(saved, fresh),
        file_mode=oct(stat.S_IMODE(os.stat(path).st_mode)),
        same_account=fresh.account_id == original.account_id,
    )
    # 2. A token that expires mid-session: the provider refreshes before sending.
    before = calls
    in_session = RefreshingCredentials(
        replace(fresh, expires_at=time.time()), client=client, persist=persist
    )
    ok, errors = await ask(provider_name, in_session)
    record_out.update(
        request_after_refresh=ok,
        refreshed_inside_request=calls == before + 1,
        request_errors=errors,
    )
    # 3. Refresh again from the file alone: proves the saved refresh token is the live one.
    record, from_file = load(path)
    second = RefreshingCredentials(
        replace(from_file, expires_at=time.time()), client=client, persist=persist
    )
    again = await second.get()
    _, saved_again = load(path)
    record_out.update(
        second_refresh_from_file=again.access_token != from_file.access_token,
        second_refresh_saved=same(saved_again, again),
        network_refreshes=calls,
    )
    # 4. The example CLI reuses the saved file without --login.
    cli = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/provider_chat.py"),
            "--provider",
            provider_name,
            "--model",
            MODELS[provider_name],
            "--credential-file",
            str(path),
            "--prompt",
            "Reply with exactly PI_REFRESH_OK.",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    record_out.update(
        example_reuse_exit_code=cli.returncode,
        example_reuse_answer_ok=cli.stdout.strip().endswith("PI_REFRESH_OK"),
    )
    record_out["passed"] = all(
        [
            record_out["network_refreshes"] == 3,
            record_out["refreshed_inside_request"],
            record_out["callers_got_same_token"],
            record_out["access_token_changed"],
            record_out["minutes_valid_after_refresh"] > 5,
            record_out["file_matches_memory"],
            record_out["file_mode"] == "0o600",
            record_out["same_account"],
            record_out["request_after_refresh"],
            record_out["second_refresh_from_file"],
            record_out["second_refresh_saved"],
            record_out["example_reuse_exit_code"] == 0,
        ]
    )
    record_out["seconds"] = round(time.time() - record_out["started_at"], 2)
    return record_out


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anthropic-file", type=Path)
    parser.add_argument("--codex-file", type=Path)
    parser.add_argument("--output", default="compat/results/live-refresh.json")
    args = parser.parse_args()
    save_private = example_module().save_private
    results = []
    for name, path in [("anthropic", args.anthropic_file), ("openai-codex", args.codex_file)]:
        if path is None:
            continue
        try:
            result = await check(name, path.expanduser(), save_private)
        except Exception as exc:
            # Only the exception type: messages from token endpoints can echo request data.
            result = {"provider": name, "passed": False, "error_type": type(exc).__name__}
        results.append(result)
        print(json.dumps(result), flush=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "scope": "Live OAuth refresh on library-owned credential files; no tokens recorded",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    return int(not results or not all(r["passed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
