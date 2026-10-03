import asyncio
import base64
import hashlib
import json
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pi_python import CancelToken, ConfigurationError, ProviderProtocolError
from pi_python.providers import HTTPTransport, OAuthClient, OAuthCredential, RefreshingCredentials


def callback(attempt, **extra):
    return (
        attempt.redirect_uri
        + "?"
        + urlencode({"code": "fixture-code", "state": attempt.state, **extra})
    )


def codex_token():
    part = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"}}
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return "header." + part + ".signature"


def token_response(provider):
    return {
        "access_token": codex_token() if provider == "openai-codex" else "access-fixture",
        "refresh_token": "refresh-fixture",
        "expires_in": 3600,
        "scope": "openid chatgpt.tokens.use.direct",
    }


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "openai-chatgpt"])
def test_pkce_state_and_auth_parameters(provider):
    client = OAuthClient(provider)
    attempt = client.begin(host_id="urn:uuid:00000000-0000-0000-0000-000000000001")
    query = parse_qs(urlsplit(attempt.authorize_url).query)
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [expected] and query["code_challenge_method"] == ["S256"]
    assert query["state"] == [attempt.state]
    assert attempt.verifier not in repr(attempt)
    if provider == "openai-chatgpt":
        assert query["agent_name_hint"] == ["pi-python"] and query["resource"] == [
            "https://api.openai.com/v1"
        ]


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "openai-chatgpt"])
@pytest.mark.parametrize("fault", ["state", "origin", "duplicate", "denied"])
async def test_callback_validation_precedes_token_request(provider, fault):
    client = OAuthClient(provider)
    attempt = client.begin(host_id="00000000-0000-0000-0000-000000000001")
    value = callback(attempt, client_id="issued")
    if fault == "state":
        value = value.replace(attempt.state, "wrong")
    if fault == "origin":
        value = value.replace("http://", "https://")
    if fault == "duplicate":
        value += "&code=second"
    if fault == "denied":
        value += "&error=access_denied"
    with pytest.raises(ConfigurationError):
        await client.exchange(attempt, value)


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex"])
async def test_exchange_and_refresh_use_correct_encoding(provider):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=token_response(provider))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OAuthClient(provider, transport=HTTPTransport(http))
        attempt = client.begin(method="copy_code" if provider == "anthropic" else "browser")
        credential = await client.exchange(
            attempt, "fixture#" + attempt.state if provider == "anthropic" else callback(attempt)
        )
        renewed = await client.refresh(credential)
    assert renewed.access_token == credential.access_token
    assert "refresh-fixture" not in repr(credential)
    body = (
        json.loads(requests[0].content)
        if provider == "anthropic"
        else {k: v[0] for k, v in parse_qs(requests[0].content.decode()).items()}
    )
    assert body["code_verifier"] == attempt.verifier
    assert body["redirect_uri"] == attempt.redirect_uri
    with pytest.raises(ConfigurationError):
        await client.exchange(attempt, callback(attempt))


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_token(key, attempt, **changes):
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "issued-client",
        "sub": "user-1",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "nonce": attempt.nonce,
        **changes,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "key-1"})


def jwt_transport(key, token_supplier):
    requests = []
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "key-1"

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200, json={"jwks_uri": "https://auth.openai.com/.well-known/jwks.json"}
            )
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(
            200, json={**token_response("openai-chatgpt"), "id_token": token_supplier()}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http, requests


async def test_chatgpt_dynamic_registration_and_verified_identity(signing_key):
    attempt = None
    http, requests = jwt_transport(signing_key, lambda: signed_token(signing_key, attempt))
    async with http:
        client = OAuthClient("openai-chatgpt", transport=HTTPTransport(http))
        attempt = client.begin(host_id="00000000-0000-0000-0000-000000000001")
        credential = await client.exchange(attempt, callback(attempt, client_id="issued-client"))
        assert credential.subject == "user-1" and credential.client_id == "issued-client"
        renewed = await client.refresh(credential)
        assert renewed.subject == "user-1" and renewed.host_id == credential.host_id
        again = client.begin(credential=credential)
        query = parse_qs(urlsplit(again.authorize_url).query)
        assert query["client_id"] == ["issued-client"] and "agent_name_hint" not in query
        assert query["id_token_hint"] == [credential.id_token]
        with pytest.raises(ConfigurationError):
            await client.exchange(again, callback(again, client_id="other"))
    first = parse_qs(requests[0].content.decode())
    assert first["client_id"] == ["issued-client"] and first["resource"] == [
        "https://api.openai.com/v1"
    ]


@pytest.mark.parametrize(
    "fault", ["signature", "issuer", "audience", "nonce", "expired", "subject"]
)
async def test_chatgpt_rejects_invalid_identity(signing_key, fault):
    attempt = None
    changes = {
        "issuer": {"iss": "https://attacker.test"},
        "audience": {"aud": "other"},
        "nonce": {"nonce": "wrong"},
        "expired": {"exp": 1},
        "subject": {"sub": "other"},
    }.get(fault, {})
    other = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if fault == "signature"
        else signing_key
    )
    http, _ = jwt_transport(signing_key, lambda: signed_token(other, attempt, **changes))
    async with http:
        client = OAuthClient("openai-chatgpt", transport=HTTPTransport(http))
        attempt = client.begin(host_id="00000000-0000-0000-0000-000000000001")
        if fault == "subject":
            attempt.expected_subject = "user-1"
        with pytest.raises(ConfigurationError):
            await client.exchange(attempt, callback(attempt, client_id="issued-client"))


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"access_token": "a", "refresh_token": "r", "expires_in": True},
        {"access_token": "a", "refresh_token": "r", "expires_in": float("inf")},
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "openid"},
    ],
)
def test_invalid_token_payloads(bad):
    with pytest.raises((ProviderProtocolError, ConfigurationError)):
        OAuthClient("openai-chatgpt")._credential(bad, "client")


async def test_chatgpt_sign_in_without_pyjwt_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "jwt", None)  # makes `import jwt` fail
    with pytest.raises(ImportError, match=r"pip install 'pi-python-core\[oauth\]'"):
        await OAuthClient("openai-chatgpt")._verify_id_token("token", "client", None, CancelToken())


async def test_refresh_single_flight_rotates_and_retries_persistence():
    calls = []
    saved = []
    old = OAuthCredential("anthropic", "expired", "refresh-old", 0)
    new = OAuthCredential("anthropic", "new", "refresh-new", time.time() + 3600)

    class Client:
        async def refresh(self, credential, cancel):
            calls.append(credential.refresh_token)
            await asyncio.sleep(0)
            return new

    async def persist(credential):
        saved.append(credential)
        if len(saved) == 1:
            raise OSError("disk unavailable")

    store = RefreshingCredentials(old, client=Client(), persist=persist)
    with pytest.raises(OSError):
        await store.get()
    results = await asyncio.gather(*(store.get() for _ in range(5)))
    assert results == [new] * 5 and calls == ["refresh-old"] and saved == [new, new]


async def test_local_loopback_login_and_server_cleanup():
    captured = []

    async def handle(request):
        captured.append(request)
        return httpx.Response(200, json=token_response("anthropic"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OAuthClient("anthropic", transport=HTTPTransport(http))

        async def open_browser(url):
            query = parse_qs(urlsplit(url).query)
            uri = query["redirect_uri"][0]
            async with httpx.AsyncClient(trust_env=False) as browser:
                bad = await browser.get(uri, params={"state": "wrong", "code": "bad"})
                assert bad.status_code == 400
                good = await browser.get(
                    uri, params={"state": query["state"][0], "code": "fixture"}
                )
                assert good.status_code == 200

        credential = await client.login(open_browser, timeout=3)
    assert credential.provider == "anthropic" and len(captured) == 1
    # Port was actually closed and can be immediately rebound.
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 53692)
    server.close()
    await server.wait_closed()


async def test_manual_login_busy_port_and_cancel_cleanup():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 53692)
    cancel = CancelToken()

    async def on_auth(url):
        cancel.cancel()

    async with server:
        with pytest.raises(asyncio.CancelledError):
            await OAuthClient("anthropic").login(
                on_auth, on_prompt=lambda uri: "unused", cancel=cancel
            )


async def test_codex_device_login_with_pending_poll(monkeypatch):
    from pi_python.providers import oauth

    original_sleep = asyncio.sleep

    async def immediate(delay):
        await original_sleep(0)

    monkeypatch.setattr(oauth.asyncio, "sleep", immediate)
    polls = []
    notices = []

    async def handler(request):
        if request.url.path.endswith("usercode"):
            return httpx.Response(
                200, json={"device_auth_id": "device", "user_code": "ABC-123", "interval": "1"}
            )
        if request.url.path.endswith("deviceauth/token"):
            polls.append(1)
            if len(polls) == 1:
                return httpx.Response(403)
            return httpx.Response(
                200, json={"authorization_code": "code", "code_verifier": "verifier"}
            )
        body = parse_qs(request.content.decode())
        assert body["redirect_uri"] == ["https://auth.openai.com/deviceauth/callback"]
        return httpx.Response(200, json=token_response("openai-codex"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        credential = await OAuthClient("openai-codex", transport=HTTPTransport(http)).device_login(
            notices.append, timeout=3
        )
    assert credential.account_id == "fixture-account" and len(polls) == 2
    assert notices[0]["user_code"] == "ABC-123"


async def test_cancelled_refresh_waiter_does_not_leak_lock():
    credential = OAuthCredential("anthropic", "access", "refresh", time.time() + 3600)
    store = RefreshingCredentials(credential)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await store.get(cancel)
    assert await asyncio.wait_for(store.get(), 1) == credential


async def test_loopback_denial_fails_immediately():
    client = OAuthClient("anthropic")

    async def open_browser(url):
        query = parse_qs(urlsplit(url).query)
        async with httpx.AsyncClient(trust_env=False) as browser:
            await browser.get(
                query["redirect_uri"][0],
                params={"state": query["state"][0], "error": "access_denied"},
            )

    with pytest.raises(ConfigurationError, match="declined"):
        await asyncio.wait_for(client.login(open_browser), 2)


def test_example_persistence_is_atomic_and_private(tmp_path):
    from examples.provider_chat import save_private

    path = tmp_path / "credentials.json"
    save_private(path, {"token": "first"})
    save_private(path, {"token": "rotated"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "rotated"}
    if sys.platform != "win32":  # Windows has no POSIX permission bits
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]
