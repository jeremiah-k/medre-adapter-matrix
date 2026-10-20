"""OAuth CLI resumption, signing approval, and runtime attachment boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.oauth import MatrixOAuthError
from medre.adapters.matrix.oauth_approval import _challenge, signing_approval
from medre.adapters.matrix.oauth_session import OAuthSessionManager
from medre.config.adapters.matrix_credentials import (
    load_credentials_json,
    write_credentials_json,
)
from medre_matrix_tests.helpers.oauth import Clock, make_session


@pytest.fixture
def oauth_cli(tmp_path, monkeypatch):
    import medre.adapters.matrix.oauth_cli as module

    path = tmp_path / "matrix.json"
    saved = make_session()
    protocol = SimpleNamespace(
        authorize=AsyncMock(return_value=saved),
        refresh=AsyncMock(),
        revoke=AsyncMock(),
        clock=lambda: 1000,
    )
    monkeypatch.setattr(module, "get_credentials_path", lambda: path)
    monkeypatch.setattr(module, "MatrixOAuthClient", lambda: protocol)
    return module, protocol, path, saved


def args(**changes):
    values = dict(
        oauth=True,
        homeserver=None,
        user=None,
        password=None,
        password_stdin=False,
        client_id=None,
        adapter_id=None,
        reset_cross_signing=False,
    )
    return SimpleNamespace(**(values | changes))


async def test_oauth_cli_login_saves_and_resumes_same_device(oauth_cli, capsys):
    module, protocol, path, saved = oauth_cli
    await module.oauth_login(args(homeserver="matrix.example"))
    await module.oauth_login(args())
    assert protocol.authorize.await_count == 1
    assert load_credentials_json(path)["device_id"] == saved.device_id
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert saved.device_id in output


@pytest.mark.parametrize("options", [{"password": "secret"}, {"password_stdin": True}])
async def test_oauth_cli_rejects_password_flags_before_network(oauth_cli, options):
    module, protocol, _, _ = oauth_cli
    with pytest.raises(MatrixOAuthError, match="password options"):
        await module.oauth_login(args(**options))
    protocol.authorize.assert_not_awaited()


async def test_oauth_cli_rejects_account_switch_and_legacy_overwrite(oauth_cli):
    module, protocol, path, saved = oauth_cli
    write_credentials_json(saved.credentials(), path)
    with pytest.raises(MatrixOAuthError, match="another user"):
        await module.oauth_login(args(user="@other:example"))
    write_credentials_json({"access_token": "legacy"}, path)
    with pytest.raises(MatrixOAuthError, match="malformed"):
        await module.oauth_login(args(homeserver="matrix.example"))
    assert load_credentials_json(path) == {"access_token": "legacy"}
    protocol.authorize.assert_not_awaited()


async def test_oauth_cli_retains_credentials_for_signing_retry(oauth_cli, monkeypatch):
    module, protocol, path, saved = oauth_cli
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap

    call = AsyncMock(side_effect=RuntimeError("SDK failure with hidden secret"))
    monkeypatch.setattr(bootstrap, "bootstrap_login_cross_signing", call)
    with pytest.raises(MatrixOAuthError, match="retained"):
        await module.oauth_login(args(homeserver="matrix.example", adapter_id="matrix"))
    assert load_credentials_json(path) == saved.credentials()
    assert call.await_args.kwargs["reset"] is False
    protocol.revoke.assert_not_awaited()
    call.side_effect = None
    call.return_value = SimpleNamespace(store_path="/private/store")
    await module.oauth_login(args(adapter_id="matrix"))
    assert protocol.authorize.await_count == 1


async def test_oauth_logout_preserves_crypto_store_and_retains_record_on_failure(
    oauth_cli, tmp_path
):
    module, protocol, path, saved = oauth_cli
    write_credentials_json(saved.credentials(), path)
    store = tmp_path / "store"
    store.mkdir()
    identity = store / "identity.json"
    identity.write_text("private identity")
    protocol.revoke.side_effect = MatrixOAuthError("offline")
    with pytest.raises(MatrixOAuthError):
        await module.logout_credentials()
    assert path.exists()
    protocol.revoke.side_effect = None
    await module.logout_credentials()
    assert not path.exists()
    assert identity.read_text() == "private identity"


def challenge(url="https://auth.example/account?action=reset", session="uia-session"):
    return {
        "session": session,
        "flows": [{"stages": ["m.oauth"]}],
        "params": {"m.oauth": {"url": url}},
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.example/approve",
        "https://evil.example/approve",
        "https://auth.example@evil.example/approve",
        "https://auth.example/approve\n",
        "https://auth.example/approve#bad",
    ],
)
def test_signing_approval_rejects_untrusted_urls(url):
    with pytest.raises(MatrixOAuthError):
        _challenge(challenge(url), "https://auth.example")


class Response:
    def __init__(self, status, body):
        self.status = status
        raw = json.dumps(body).encode()

        class Content:
            async def iter_chunked(self, size):
                yield raw

        self.content = Content()


async def test_signing_approval_uses_same_session_and_never_requests_identity_reset():
    original = AsyncMock(
        side_effect=[
            Response(401, challenge()),
            Response(401, challenge()),
            Response(200, {}),
        ]
    )
    clock, output = Clock(), []
    send = signing_approval(
        original,
        "https://auth.example",
        clock=clock,
        sleep=clock.sleep,
        display=output.append,
    )
    response = await send(
        "POST",
        "/_matrix/client/v3/keys/device_signing/upload",
        json.dumps({"master_key": "existing-or-explicitly-authorized"}),
    )
    assert response.status == 200
    assert original.await_count == 3
    for call in original.await_args_list[1:]:
        assert json.loads(call.args[2])["auth"] == {"session": "uia-session"}
    assert "reset-cross-signing" not in " ".join(output)
    assert clock.value == 1010


async def test_signing_approval_rejects_changed_challenges():
    original = AsyncMock(
        side_effect=[
            Response(401, challenge()),
            Response(401, challenge(session="other")),
        ]
    )
    clock = Clock()
    with pytest.raises(MatrixOAuthError, match="changed"):
        await signing_approval(
            original,
            "https://auth.example",
            clock=clock,
            sleep=clock.sleep,
            display=lambda _: None,
        )("POST", "/_matrix/client/v3/keys/device_signing/upload", "{}")


async def test_signing_wrapper_does_not_handle_other_endpoints():
    response = Response(401, challenge())
    original = AsyncMock(return_value=response)
    assert (
        await signing_approval(original, "https://auth.example")(
            "GET", "/_matrix/client/v3/sync"
        )
        is response
    )
    original.assert_awaited_once()


async def test_runtime_attaches_renewal_to_plaintext_and_e2ee_clients(
    tmp_path, monkeypatch
):
    from medre.adapters.matrix.session import MatrixSession
    from medre.config.adapters.matrix import MatrixConfig
    import medre.adapters.matrix.session as module

    saved = make_session()
    path = tmp_path / "matrix.json"
    write_credentials_json(saved.credentials(), path)
    manager = OAuthSessionManager(saved, path)

    def factory(*args):
        return manager

    monkeypatch.setattr(module, "manager_for_credentials", factory)
    bridge = MatrixSession(
        MatrixConfig(
            "matrix", saved.homeserver, saved.user_id, access_token=saved.access_token
        )
    )
    one = SimpleNamespace(send=AsyncMock())
    bridge._client = one
    bridge._attach_managed_auth()
    assert one.send is not None
    assert bridge._oauth_manager is manager
    two = SimpleNamespace(send=AsyncMock())
    bridge._client = two
    monkeypatch.setattr(
        module,
        "manager_for_credentials",
        lambda *args: pytest.fail("reconnect must retain its manager"),
    )
    bridge._attach_managed_auth()
    assert bridge._oauth_manager is manager


async def test_oauth_resume_accepts_original_delegating_homeserver(oauth_cli):
    module, protocol, path, saved = oauth_cli
    from dataclasses import replace

    delegated = replace(saved, homeserver="https://matrix-client.example")
    write_credentials_json(delegated.credentials(), path)
    protocol.discover = AsyncMock(
        return_value=(delegated.homeserver, {"issuer": saved.issuer})
    )
    await module.oauth_login(args(homeserver="matrix.example"))
    protocol.authorize.assert_not_awaited()
    protocol.discover.assert_awaited_once_with("matrix.example")


async def test_password_login_cannot_replace_oauth_record_that_arrived_after_prompt(
    oauth_cli,
):
    from medre.adapters.matrix.auth import MatrixLoginResult, save_credentials_json
    from medre.adapters.matrix.errors import MatrixConnectionError

    _, _, path, saved = oauth_cli
    write_credentials_json(saved.credentials(), path)
    with pytest.raises(MatrixConnectionError, match="OAuth logout"):
        save_credentials_json(
            MatrixLoginResult(
                saved.homeserver, saved.user_id, "OTHER", "password-token"
            ),
            path,
        )
    assert load_credentials_json(path) == saved.credentials()


async def test_logout_revokes_legacy_credentials_before_switching_to_oauth(
    oauth_cli, monkeypatch
):
    module, protocol, path, _ = oauth_cli
    import medre.adapters.matrix.auth as auth

    logged_out = []
    monkeypatch.setattr(
        auth,
        "matrix_logout",
        lambda homeserver, token: logged_out.append((homeserver, token)),
    )
    write_credentials_json(
        {
            "homeserver": "https://matrix.example",
            "user_id": "@relay:example",
            "access_token": "legacy",
        },
        path,
    )
    await module.logout_credentials()
    assert logged_out == [("https://matrix.example", "legacy")]
    assert not path.exists()
    protocol.revoke.assert_not_awaited()
