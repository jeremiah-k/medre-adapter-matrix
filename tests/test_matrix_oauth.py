"""SDK-independent OAuth discovery, approval, and credential rotation."""

from __future__ import annotations

import asyncio
import stat
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.oauth import (
    API_SCOPE,
    DEVICE_GRANT,
    DEVICE_SCOPE,
    MatrixOAuthClient,
    MatrixOAuthError,
    OAuthResponse,
    https_url,
)
from medre.adapters.matrix.oauth_session import (
    OAuthSessionManager,
    attach_oauth,
    credential_lock,
    manager_for_credentials,
)
from medre_matrix_tests.helpers.oauth import Clock, make_session
from medre.config.adapters.matrix_credentials import (
    load_credentials_json,
    write_credentials_json,
)


class ScriptedHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert self.responses, (method, url)
        return self.responses.pop(0)


def metadata():
    return {
        "issuer": "https://auth.example",
        "grant_types_supported": [DEVICE_GRANT, "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "token_endpoint": "https://auth.example/token",
        "registration_endpoint": "https://auth.example/register",
        "device_authorization_endpoint": "https://auth.example/device",
        "revocation_endpoint": "https://auth.example/revoke",
    }


def login_responses(*, token_overrides=None, whoami_overrides=None, polls=()):
    tokens = {
        "access_token": "ACCESS_SECRET",
        "refresh_token": "REFRESH_SECRET",
        "token_type": "Bearer",
        "expires_in": 100,
    }
    tokens.update(token_overrides or {})
    identity = {"user_id": "@relay:example", "device_id": "MEDRE_device"}
    identity.update(whoami_overrides or {})
    return [
        OAuthResponse(404, {}),
        OAuthResponse(200, metadata()),
        OAuthResponse(201, {"client_id": "public-client"}),
        OAuthResponse(
            200,
            {
                "device_code": "PRIVATE_DEVICE_CODE",
                "user_code": "LOGIN-CODE",
                "verification_uri": "https://auth.example/approve",
                "expires_in": 120,
                "interval": 1,
            },
        ),
        *polls,
        OAuthResponse(200, tokens),
        OAuthResponse(200, identity),
    ]


async def test_device_login_discovers_scopes_and_verifies_account(monkeypatch):
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth.secrets.token_hex", lambda n: "device"
    )
    http, clock, prompts = ScriptedHTTP(login_responses()), Clock(), []
    client = MatrixOAuthClient(http, clock=clock, monotonic=clock, sleep=clock.sleep)
    result = await client.authorize(
        "matrix.example", prompts.append, expected_user="relay"
    )
    assert result.user_id == "@relay:example"
    assert result.device_id == "MEDRE_device"
    assert result.scope == API_SCOPE + " " + DEVICE_SCOPE + "MEDRE_device"
    assert result.expires_at == 1101
    assert prompts[0].url == "https://auth.example/approve"
    assert prompts[0].code == "LOGIN-CODE"
    assert http.requests[4][2]["form"]["device_code"] == "PRIVATE_DEVICE_CODE"
    assert http.requests[-1][2]["token"] == "ACCESS_SECRET"
    assert "SECRET" not in repr(result)
    assert "LOGIN-CODE" not in repr(prompts[0])


async def test_pending_and_slow_down_adjust_polling_without_relogin(monkeypatch):
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth.secrets.token_hex", lambda n: "device"
    )
    http = ScriptedHTTP(
        login_responses(
            polls=[
                OAuthResponse(403, {"error": "authorization_pending"}),
                OAuthResponse(400, {"error": "slow_down"}),
            ]
        )
    )
    clock = Clock()
    result = await MatrixOAuthClient(
        http, clock=clock, monotonic=clock, sleep=clock.sleep
    ).authorize("matrix.example", lambda p: None)
    assert result.expires_at == 1108
    assert sum(url.endswith("/register") for _, url, _ in http.requests) == 1


@pytest.mark.parametrize(
    "whoami",
    [{"user_id": "@other:example"}, {"device_id": "OTHER"}, {"is_guest": True}],
)
async def test_mismatched_approval_is_revoked(monkeypatch, whoami):
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth.secrets.token_hex", lambda n: "device"
    )
    http = ScriptedHTTP(
        login_responses(whoami_overrides=whoami)
        + [OAuthResponse(200, {}), OAuthResponse(200, {})]
    )
    clock = Clock()
    with pytest.raises(MatrixOAuthError, match="differs"):
        await MatrixOAuthClient(
            http, clock=clock, monotonic=clock, sleep=clock.sleep
        ).authorize("matrix.example", lambda p: None, expected_user="@relay:example")
    assert [req[2]["form"]["token_type_hint"] for req in http.requests[-2:]] == [
        "refresh_token",
        "access_token",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.example",
        "https://u:p@auth.example",
        "https://auth.example/?token=secret",
        "https://auth.example/#x",
        "https://auth.example:bad",
        "https://auth.example/\n",
        "https://auth.example\\evil",
        "https://",
    ],
)
def test_oauth_endpoint_validation(url):
    with pytest.raises(MatrixOAuthError):
        https_url(url)


@pytest.mark.parametrize(
    "alteration",
    [
        {"token_endpoint": "https://evil.example/token"},
        {"grant_types_supported": [DEVICE_GRANT]},
        {"grant_types_supported": [[]]},
        {"token_endpoint_auth_methods_supported": None},
    ],
)
async def test_untrusted_or_unsupported_metadata_fails_before_registration(alteration):
    info = metadata() | alteration
    http = ScriptedHTTP([OAuthResponse(404, {}), OAuthResponse(200, info)])
    with pytest.raises(MatrixOAuthError):
        await MatrixOAuthClient(http).discover("matrix.example")
    assert len(http.requests) == 2


async def test_stable_metadata_falls_back_to_advertised_unstable_endpoint():
    http = ScriptedHTTP(
        [
            OAuthResponse(
                200, {"m.homeserver": {"base_url": "https://delegated.example"}}
            ),
            OAuthResponse(404, {}),
            OAuthResponse(200, metadata()),
        ]
    )
    server, _ = await MatrixOAuthClient(http).discover("matrix.example")
    assert server == "https://delegated.example"
    assert "msc2965/auth_metadata" in http.requests[-1][1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", API_SCOPE),
        ("scope", API_SCOPE + " admin"),
        ("expires_in", True),
        ("expires_in", float("inf")),
        ("expires_in", 10**1000),
        ("token_type", "mac"),
        ("refresh_token", ""),
    ],
)
async def test_invalid_issued_credentials_are_rejected_and_revoked(
    monkeypatch, field, value
):
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth.secrets.token_hex", lambda n: "device"
    )
    replies = login_responses(token_overrides={field: value})
    replies.pop()  # Invalid token data must not reach whoami.
    replies += [OAuthResponse(200, {}), OAuthResponse(200, {})]
    http, clock = ScriptedHTTP(replies), Clock()
    with pytest.raises(MatrixOAuthError):
        await MatrixOAuthClient(
            http, clock=clock, monotonic=clock, sleep=clock.sleep
        ).authorize("matrix.example", lambda p: None)
    assert not any(url.endswith("/whoami") for _, url, _ in http.requests)


class RenewingClient(MatrixOAuthClient):
    def __init__(self):
        super().__init__(clock=lambda: 1900.0)
        self.calls = 0
        self.revoked = []

    async def refresh(self, previous):
        self.calls += 1
        return replace(
            previous,
            access_token="ROTATED_ACCESS",
            refresh_token="ROTATED_REFRESH",
            refresh_at=2500.0,
            expires_at=2800.0,
        )

    async def revoke(self, previous):
        self.revoked.append(previous)


async def test_concurrent_refreshes_persist_one_rotation_and_reload_shared_state(
    tmp_path,
):
    path = tmp_path / "matrix.json"
    original = make_session()
    write_credentials_json(original.credentials(), path)
    protocol = RenewingClient()
    manager = OAuthSessionManager(original, path, protocol)
    assert (
        await asyncio.gather(*(manager.access_token() for _ in range(8)))
        == ["ROTATED_ACCESS"] * 8
    )
    assert protocol.calls == 1
    assert load_credentials_json(path)["oauth"]["refresh_token"] == "ROTATED_REFRESH"
    another = OAuthSessionManager(original, path, protocol)
    assert await another.access_token() == "ROTATED_ACCESS"
    assert protocol.calls == 1
    if __import__("os").name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_cancelled_rotation_finishes_persistence_before_unlocking(tmp_path):
    path = tmp_path / "matrix.json"
    original = make_session()
    write_credentials_json(original.credentials(), path)
    entered, release = asyncio.Event(), asyncio.Event()
    protocol = RenewingClient()
    refresh = protocol.refresh

    async def delayed(previous):
        entered.set()
        await release.wait()
        return await refresh(previous)

    protocol.refresh = delayed
    manager = OAuthSessionManager(original, path, protocol)
    task = asyncio.create_task(manager.access_token())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert load_credentials_json(path)["access_token"] == "ROTATED_ACCESS"
    assert await manager.access_token() == "ROTATED_ACCESS"
    assert protocol.calls == 1


async def test_refresh_write_failure_revokes_unpersisted_tokens(tmp_path, monkeypatch):
    import medre.adapters.matrix.oauth_session as module

    path = tmp_path / "matrix.json"
    original = make_session()
    write_credentials_json(original.credentials(), path)
    protocol = RenewingClient()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module, "write_credentials_json", fail)
    with pytest.raises(MatrixOAuthError, match="could not be saved"):
        await OAuthSessionManager(original, path, protocol).access_token()
    assert len(protocol.revoked) == 1
    assert load_credentials_json(path)["access_token"] == "ACCESS_SECRET"


def test_credential_lock_refuses_competing_writers(tmp_path):
    path = tmp_path / "matrix.json"
    with credential_lock(path):
        with pytest.raises(MatrixOAuthError, match="in use"):
            with credential_lock(path):
                pytest.fail("second writer acquired the credential lock")


async def test_sdk_transport_replaces_query_and_header_tokens_per_attempt():
    original = AsyncMock(return_value="ok")
    manager = SimpleNamespace(
        session=make_session(), access_token=AsyncMock(side_effect=["one", "two"])
    )
    client = SimpleNamespace(
        homeserver="https://matrix.example",
        config=SimpleNamespace(custom_headers={}),
        send=original,
    )
    attach_oauth(client, manager)
    for _ in range(2):
        assert (
            await client.send(
                "GET",
                "/_matrix/client/v3/sync?access_token=old&x=y",
                headers={"authorization": "Bearer old", "X-Test": "yes"},
            )
            == "ok"
        )
    for call, token in zip(original.await_args_list, ("one", "two"), strict=True):
        assert call.args[1] == "/_matrix/client/v3/sync?x=y"
        assert call.kwargs["headers"] == {
            "Authorization": "Bearer " + token,
            "X-Test": "yes",
        }


@pytest.mark.parametrize(
    "path", ["https://evil.example", "//evil.example", "relative", "/\\evil.example"]
)
async def test_sdk_transport_rejects_foreign_paths_before_token_access(path):
    original = AsyncMock()
    manager = SimpleNamespace(session=make_session(), access_token=AsyncMock())
    client = SimpleNamespace(
        homeserver="https://matrix.example",
        config=SimpleNamespace(custom_headers={}),
        send=original,
    )
    attach_oauth(client, manager)
    with pytest.raises(MatrixOAuthError):
        await client.send("GET", path)
    manager.access_token.assert_not_awaited()
    original.assert_not_awaited()


def test_manager_only_selects_the_sidecar_that_supplied_credentials(tmp_path):
    path = tmp_path / "matrix.json"
    saved = make_session()
    write_credentials_json(saved.credentials(), path)
    assert (
        manager_for_credentials(
            saved.homeserver,
            saved.user_id,
            saved.device_id,
            "explicit-other-token",
            path=path,
        )
        is None
    )
    assert (
        manager_for_credentials(
            saved.homeserver,
            saved.user_id,
            saved.device_id,
            saved.access_token,
            path=path,
        )
        is not None
    )


async def test_refresh_reuses_refresh_token_when_server_does_not_rotate_it():
    previous = make_session()
    http = ScriptedHTTP(
        [
            OAuthResponse(
                200,
                {
                    "access_token": "NEW_ACCESS",
                    "expires_in": 100,
                    "token_type": "Bearer",
                },
            )
        ]
    )
    refreshed = await MatrixOAuthClient(http, clock=lambda: 1000).refresh(previous)
    assert refreshed.refresh_token == previous.refresh_token
    assert refreshed.access_token == "NEW_ACCESS"
    assert http.requests[0][2]["form"]["grant_type"] == "refresh_token"
    assert http.requests[0][2]["form"]["refresh_token"] == previous.refresh_token
    assert "NEW_ACCESS" not in repr(http.responses)


@pytest.mark.matrix_sdk
@pytest.mark.parametrize(
    "status,contents",
    [
        (302, b'{"access_token":"HIDDEN"}'),
        (200, b"[]"),
        (200, b"x" * 65537),
        (404, b"<html>not found</html>"),
    ],
)
async def test_oauth_http_bounds_bodies_and_refuses_redirects(
    monkeypatch, status, contents
):
    import aiohttp
    from medre.adapters.matrix.oauth import OAuthHTTP

    observed = {}

    class Content:
        async def iter_chunked(self, size):
            for start in range(0, len(contents), size):
                yield contents[start : start + size]

    class Response:
        content = Content()

        async def __aenter__(self):
            self.status = status
            return self

        async def __aexit__(self, *args):
            pass

    class HTTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            observed.update(kwargs)
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", HTTP)
    if status == 404:
        result = await OAuthHTTP().request("GET", "https://auth.example/metadata")
        assert result.status == 404
    else:
        with pytest.raises(MatrixOAuthError) as raised:
            await OAuthHTTP().request("POST", "https://auth.example/token")
        assert "HIDDEN" not in str(raised.value)
    assert observed["allow_redirects"] is False


def test_saved_oauth_envelope_matches_published_schema():
    import json
    from pathlib import Path
    import jsonschema
    from medre.adapters.matrix.oauth import OAuthSession

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/matrix-oauth-credentials.schema.json"
        ).read_text()
    )
    saved = make_session()
    jsonschema.validate(saved.credentials(), schema)
    assert OAuthSession.parse(saved.credentials()) == saved
