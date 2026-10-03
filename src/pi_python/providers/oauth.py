"""Explicit OAuth login and refresh. No ambient credentials or automatic browser access.

Endpoint/client constants follow Pi v1.0.0. ChatGPT ID token validation also follows
OpenAI's public sign-in documentation. Applications own credential persistence.
"""

from __future__ import annotations
import asyncio
import base64
import hashlib
import math
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from ..cancellation import CancelToken
from ..errors import ConfigurationError, ProviderProtocolError
from ..tools import invoke
from .transport import HTTPTransport, ProviderHTTPError, cancellable
from .openai import account_id


@dataclass(frozen=True)
class OAuthCredential:
    provider: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    client_id: str = ""
    scopes: tuple[str, ...] = ()
    account_id: str | None = None
    subject: str | None = None
    id_token: str | None = field(default=None, repr=False)
    host_id: str | None = None


@dataclass
class OAuthAttempt:
    provider: str
    authorize_url: str = field(repr=False)
    redirect_uri: str
    client_id: str
    verifier: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    host_id: str | None = None
    expected_subject: str | None = None
    consumed: bool = False


_SETTINGS = {
    "anthropic": (
        "https://claude.ai/oauth/authorize",
        "https://platform.claude.com/v1/oauth/token",
        "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "http://localhost:53692/callback",
        "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload",
    ),
    "openai-codex": (
        "https://auth.openai.com/oauth/authorize",
        "https://auth.openai.com/oauth/token",
        "app_EMoamEEZ73f0CkXaXp7hrann",
        "http://localhost:1455/auth/callback",
        "openid profile email offline_access",
    ),
    "openai-chatgpt": (
        "https://auth.openai.com/api/accounts/authorize",
        "https://auth.openai.com/api/accounts/oauth/token",
        "dynamic_agent_client",
        "http://127.0.0.1:1455/auth/callback",
        "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
    ),
}
_RESOURCE = "https://api.openai.com/v1"


class OAuthClient:
    def __init__(self, provider: str, *, transport: HTTPTransport | None = None) -> None:
        if provider not in _SETTINGS:
            raise ConfigurationError("Unknown OAuth provider")
        self.provider = provider
        self.transport = transport or HTTPTransport()

    def begin(
        self,
        *,
        method: str = "browser",
        host_id: str | None = None,
        credential: OAuthCredential | None = None,
        redirect_uri: str | None = None,
    ) -> OAuthAttempt:
        authorize, _, clientid, redirect, scope = _SETTINGS[self.provider]
        if method not in {"browser", "copy_code"} or (
            method == "copy_code" and self.provider != "anthropic"
        ):
            raise ConfigurationError("copy_code is supported only for Anthropic")
        if method == "copy_code":
            redirect = "https://platform.claude.com/oauth/code/callback"
        if redirect_uri:
            given, expected = urlsplit(redirect_uri), urlsplit(redirect)
            if (
                self.provider != "openai-chatgpt"
                or (given.scheme, given.hostname, given.path)
                != (expected.scheme, expected.hostname, expected.path)
                or given.query
                or given.fragment
                or given.username
                or given.password
            ):
                raise ConfigurationError("Only ChatGPT loopback port may be overridden")
            redirect = redirect_uri
        verifier = secrets.token_urlsafe(48)
        state = verifier if self.provider == "anthropic" else secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "client_id": clientid,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        expected_subject = None
        if credential:
            if credential.provider != self.provider:
                raise ConfigurationError("Credential belongs to another provider")
            if self.provider == "openai-chatgpt":
                clientid = credential.client_id
                expected_subject = credential.subject
                params["client_id"] = clientid
                if credential.id_token:
                    params["id_token_hint"] = credential.id_token
                host_id = host_id or credential.host_id
        if self.provider == "anthropic":
            params["code"] = "true"
        elif self.provider == "openai-codex":
            params.update(
                {
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "originator": "pi",
                }
            )
        else:
            if not host_id:
                raise ConfigurationError(
                    "Provide a stable host_id (urn:uuid:...) and persist it for this installation"
                )
            try:
                host_id = "urn:uuid:" + str(uuid.UUID(host_id.removeprefix("urn:uuid:")))
            except (ValueError, AttributeError) as exc:
                raise ConfigurationError("Invalid host UUID") from exc
            params.update({"ext_agent_host_id": host_id, "nonce": nonce, "resource": _RESOURCE})
            if clientid == "dynamic_agent_client":
                params["agent_name_hint"] = "pi-python"
        return OAuthAttempt(
            self.provider,
            authorize + "?" + urlencode(params),
            redirect,
            clientid,
            verifier,
            state,
            nonce,
            host_id,
            expected_subject,
        )

    def _callback(self, attempt: OAuthAttempt, callback: str) -> tuple[str, str]:
        if attempt.provider != self.provider or attempt.consumed:
            raise ConfigurationError("OAuth attempt is invalid or already consumed")
        if self.provider == "anthropic" and "#" in callback and "://" not in callback:
            code, state = callback.strip().split("#", 1)
            values = {"code": code, "state": state}
        else:
            actual, expected = urlsplit(callback.strip()), urlsplit(attempt.redirect_uri)
            if (actual.scheme, actual.netloc, actual.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ) or actual.fragment:
                raise ConfigurationError("OAuth callback URI does not match")
            query = parse_qs(actual.query, keep_blank_values=True)
            if any(len(v) != 1 for v in query.values()):
                raise ConfigurationError("Duplicate OAuth callback parameter")
            values = {k: v[0] for k, v in query.items()}
        if not secrets.compare_digest(values.get("state", ""), attempt.state):
            raise ConfigurationError("OAuth state mismatch")
        if values.get("error"):
            attempt.consumed = True
            raise ConfigurationError("OAuth authorization was declined or failed")
        if not values.get("code"):
            raise ConfigurationError("OAuth callback lacks code")
        clientid = attempt.client_id
        if self.provider == "openai-chatgpt":
            returned = values.get("client_id")
            if clientid == "dynamic_agent_client":
                if not returned or returned == "dynamic_agent_client":
                    raise ConfigurationError("Dynamic registration lacks issued client ID")
                clientid = returned
            elif returned and returned != clientid:
                raise ConfigurationError("OAuth client ID changed during reauthorization")
        return values["code"], clientid

    async def exchange(
        self, attempt: OAuthAttempt, callback: str, cancel: CancelToken | None = None
    ) -> OAuthCredential:
        cancel = cancel or CancelToken()
        code, clientid = self._callback(attempt, callback)
        attempt.consumed = True
        body = {
            "grant_type": "authorization_code",
            "client_id": clientid,
            "code": code,
            "code_verifier": attempt.verifier,
            "redirect_uri": attempt.redirect_uri,
        }
        if self.provider == "anthropic":
            body["state"] = attempt.state
        if self.provider == "openai-chatgpt":
            body["resource"] = _RESOURCE
        data = await self._token(body, cancel)
        result = self._credential(data, clientid)
        if self.provider == "openai-chatgpt":
            claims = await self._verify_id_token(
                data.get("id_token"), clientid, attempt.nonce, cancel
            )
            if attempt.expected_subject and claims["sub"] != attempt.expected_subject:
                raise ConfigurationError("Reauthorization returned a different account")
            result = replace(
                result, subject=claims["sub"], id_token=data["id_token"], host_id=attempt.host_id
            )
        return result

    async def _token(self, body: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        return await self.transport.request_json(
            _SETTINGS[self.provider][1], body, {}, cancel, form=self.provider != "anthropic"
        )

    def _credential(self, data: dict[str, Any], clientid: str) -> OAuthCredential:
        for key in ("access_token", "refresh_token"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ProviderProtocolError(f"OAuth response lacks {key}")
        expires = data.get("expires_in")
        if (
            not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or not math.isfinite(expires)
            or expires <= 0
        ):
            raise ProviderProtocolError("Invalid OAuth expires_in")
        scope = data.get("scope", "")
        if not isinstance(scope, str):
            raise ProviderProtocolError("Invalid OAuth scope")
        scopes = tuple(scope.split())
        if self.provider == "openai-chatgpt" and "chatgpt.tokens.use.direct" not in scopes:
            raise ConfigurationError("OAuth grant lacks chatgpt.tokens.use.direct")
        account = None
        if self.provider == "openai-codex":
            account = account_id(data["access_token"])
        return OAuthCredential(
            self.provider,
            data["access_token"],
            data["refresh_token"],
            time.time() + expires,
            clientid,
            scopes,
            account,
        )

    async def _verify_id_token(
        self, token: Any, clientid: str, nonce: str | None, cancel: CancelToken
    ) -> dict[str, Any]:
        import jwt

        if not isinstance(token, str) or not token:
            raise ConfigurationError("ChatGPT sign-in lacks ID token")
        try:
            discovery = await self.transport.get_json(
                "https://auth.openai.com/.well-known/openid-configuration", cancel
            )
            uri = discovery["jwks_uri"]
            if urlsplit(uri).scheme != "https" or urlsplit(uri).hostname != "auth.openai.com":
                raise ConfigurationError("Unexpected OpenAI JWKS origin")
            keys = await self.transport.get_json(uri, cancel)
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ConfigurationError("Unsupported ID token signing algorithm")
            matches = [k for k in keys["keys"] if k.get("kid") == header.get("kid")]
            if len(matches) != 1:
                raise ConfigurationError("ID token signing key is unavailable")
            key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=clientid,
                issuer="https://auth.openai.com",
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            if nonce is not None and not secrets.compare_digest(
                str(claims.get("nonce", "")), nonce
            ):
                raise ConfigurationError("ID token nonce mismatch")
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ConfigurationError("ID token subject missing")
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError("ID token validation failed") from exc

    async def refresh(
        self, credential: OAuthCredential, cancel: CancelToken | None = None
    ) -> OAuthCredential:
        if credential.provider != self.provider:
            raise ConfigurationError("Credential belongs to another provider")
        cancel = cancel or CancelToken()
        body = {
            "grant_type": "refresh_token",
            "client_id": credential.client_id or _SETTINGS[self.provider][2],
            "refresh_token": credential.refresh_token,
        }
        if self.provider == "openai-chatgpt":
            body["resource"] = _RESOURCE
        data = await self._token(body, cancel)
        result = self._credential(data, body["client_id"])
        id_token = credential.id_token
        if self.provider == "openai-chatgpt" and data.get("id_token"):
            claims = await self._verify_id_token(data["id_token"], body["client_id"], None, cancel)
            if claims["sub"] != credential.subject:
                raise ConfigurationError("Refresh returned another account")
            id_token = data["id_token"]
        return replace(
            result, subject=credential.subject, id_token=id_token, host_id=credential.host_id
        )

    async def login(
        self,
        on_auth_url: Callable[[str], Any],
        *,
        on_prompt: Callable[[str], Any] | None = None,
        cancel: CancelToken | None = None,
        timeout: float = 300,
        **begin_options: Any,
    ) -> OAuthCredential:
        """Listen on loopback before notifying the UI. UI opens the URL explicitly.

        Pass on_prompt(url)->full_callback_url for manual flow / busy-port fallback.
        For Anthropic copy_code, return code#state. Nothing is printed or persisted.
        """
        cancel = cancel or CancelToken()
        attempt = self.begin(**begin_options)
        uri = urlsplit(attempt.redirect_uri)
        if begin_options.get("method") == "copy_code":
            if on_prompt is None:
                raise ConfigurationError("copy_code requires on_prompt")
            await invoke(on_auth_url, attempt.authorize_url)
            async with asyncio.timeout(timeout):
                callback = await cancellable(invoke(on_prompt, attempt.redirect_uri), cancel)
                return await self.exchange(attempt, callback, cancel)
        future = asyncio.get_running_loop().create_future()
        handlers = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            handlers.add(task)
            try:
                async with asyncio.timeout(5):
                    raw = await reader.readuntil(b"\r\n\r\n")
                    line = raw.split(b"\r\n", 1)[0].decode("ascii")
                    method, target, _ = line.split(" ", 2)
                    if method != "GET" or not target.startswith("/") or target.startswith("//"):
                        raise ValueError
                    callback = f"{uri.scheme}://{uri.netloc}{target}"
                    self._callback(attempt, callback)
                    if not future.done():
                        future.set_result(callback)
                    response = b"Login received. You may close this window."
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                        + str(len(response)).encode()
                        + b"\r\n\r\n"
                        + response
                    )
                    await writer.drain()
            except (
                ValueError,
                ConfigurationError,
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                if attempt.consumed and not future.done():
                    future.set_exception(
                        ConfigurationError("OAuth authorization was declined or failed")
                    )
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = None
        try:
            try:
                server = await asyncio.start_server(handle, "127.0.0.1", uri.port, limit=16384)
            except OSError:
                if on_prompt is None:
                    raise ConfigurationError(
                        "OAuth loopback port unavailable; pass on_prompt for manual callback"
                    ) from None
            await invoke(on_auth_url, attempt.authorize_url)
            async with asyncio.timeout(timeout):
                callback = await cancellable(
                    future if server else invoke(on_prompt, attempt.redirect_uri), cancel
                )
                return await self.exchange(attempt, callback, cancel)
        finally:
            if server:
                server.close()
                await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            if not future.done():
                future.cancel()

    async def device_login(
        self,
        on_device_code: Callable[[dict[str, Any]], Any],
        *,
        cancel: CancelToken | None = None,
        timeout: float = 900,
    ) -> OAuthCredential:
        if self.provider != "openai-codex":
            raise ConfigurationError("Device login is available for Codex only")
        cancel = cancel or CancelToken()
        base = "https://auth.openai.com/api/accounts/deviceauth/"
        device = await self.transport.request_json(
            base + "usercode", {"client_id": _SETTINGS[self.provider][2]}, {}, cancel
        )
        interval = max(float(device.get("interval", 5)), 1)
        await invoke(
            on_device_code,
            {
                "verification_uri": "https://auth.openai.com/codex/device",
                "user_code": device["user_code"],
                "interval": interval,
                "expires_in": timeout,
            },
        )
        async with asyncio.timeout(timeout):
            while True:
                await cancellable(asyncio.sleep(interval), cancel)
                try:
                    data = await self.transport.request_json(
                        base + "token",
                        {
                            "device_auth_id": device["device_auth_id"],
                            "user_code": device["user_code"],
                        },
                        {},
                        cancel,
                    )
                except ProviderHTTPError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                if data.get("error") in {
                    "authorization_pending",
                    "deviceauth_authorization_pending",
                }:
                    continue
                if data.get("error") == "slow_down":
                    interval += 5
                    continue
                if not data.get("authorization_code") or not data.get("code_verifier"):
                    raise ProviderProtocolError("Invalid device authorization response")
                token = await self._token(
                    {
                        "grant_type": "authorization_code",
                        "client_id": _SETTINGS[self.provider][2],
                        "code": data["authorization_code"],
                        "code_verifier": data["code_verifier"],
                        "redirect_uri": "https://auth.openai.com/deviceauth/callback",
                    },
                    cancel,
                )
                return self._credential(token, _SETTINGS[self.provider][2])


class RefreshingCredentials:
    """Serialize refresh-token rotation; persist receives an immutable new record."""

    def __init__(
        self,
        credential: OAuthCredential,
        *,
        client: OAuthClient | None = None,
        persist: Callable[[OAuthCredential], Any] | None = None,
        margin: float = 300,
    ) -> None:
        self.credential = credential
        self.client = client or OAuthClient(credential.provider)
        self.persist = persist
        self.margin = margin
        self._lock = asyncio.Lock()
        self._dirty = False

    async def get(self, cancel: CancelToken | None = None) -> OAuthCredential:
        cancel = cancel or CancelToken()
        acquisition = asyncio.create_task(self._lock.acquire())
        try:
            await cancellable(acquisition, cancel)
            cancel.raise_if_cancelled()
            if self._dirty:
                await invoke(self.persist, self.credential)
                self._dirty = False
            if self.credential.expires_at <= time.time() + self.margin:
                self.credential = await self.client.refresh(self.credential, cancel)
                self._dirty = True
                await invoke(self.persist, self.credential)
                self._dirty = False
            return self.credential
        finally:
            if (
                acquisition.done()
                and not acquisition.cancelled()
                and acquisition.exception() is None
                and acquisition.result()
            ):
                self._lock.release()
