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
    with pytest.raises(MatrixOAuthError, match="auth logout"):
        await module.oauth_login(args(homeserver="matrix.example"))
    assert load_credentials_json(path) == {"access_token": "legacy"}
    protocol.authorize.assert_not_awaited()


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TypeError])
async def test_oauth_cli_retains_credentials_for_signing_retry(
    oauth_cli, monkeypatch, error_type
):
    module, protocol, path, saved = oauth_cli
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap

    call = AsyncMock(side_effect=error_type("SDK failure with hidden secret"))
    monkeypatch.setattr(bootstrap, "bootstrap_login_cross_signing", call)
    with pytest.raises(MatrixOAuthError, match="retained") as error:
        await module.oauth_login(args(homeserver="matrix.example", adapter_id="matrix"))
    assert "hidden secret" not in str(error.value)
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

    def factory(*args, **kwargs):
        return manager

    monkeypatch.setattr(module, "manager_for_credentials", factory)
    bridge = MatrixSession(
        MatrixConfig(
            "matrix", saved.homeserver, saved.user_id, access_token=saved.access_token
        )
    )
    first_send = AsyncMock()
    one = SimpleNamespace(send=first_send)
    bridge._client = one
    bridge._attach_managed_auth()
    assert one.send is not first_send
    assert bridge._oauth_manager is manager
    second_send = AsyncMock()
    two = SimpleNamespace(send=second_send)
    bridge._client = two
    monkeypatch.setattr(
        module,
        "manager_for_credentials",
        lambda *args: pytest.fail("reconnect must retain its manager"),
    )
    bridge._attach_managed_auth()
    assert bridge._oauth_manager is manager
    assert two.send is not second_send


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


async def test_signing_approval_recognizes_upload_with_sdk_query_parameters():
    original = AsyncMock(side_effect=[Response(401, challenge()), Response(200, {})])
    clock = Clock()
    send = signing_approval(
        original,
        "https://auth.example",
        clock=clock,
        sleep=clock.sleep,
        display=lambda _: None,
    )
    target = "/_matrix/client/v3/keys/device_signing/upload?access_token=old"
    response = await send("POST", target, json.dumps({"master_key": "key"}))
    assert response.status == 200
    assert original.await_count == 2
    assert all(call.args[1] == target for call in original.await_args_list)
    assert json.loads(original.await_args_list[1].args[2])["auth"] == {
        "session": "uia-session"
    }


async def test_oauth_manager_observes_other_process_rotation_and_logout(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    saved = make_session()
    path = tmp_path / "matrix.json"
    write_credentials_json(saved.credentials(), path)
    manager = OAuthSessionManager(saved, path, SimpleNamespace(clock=lambda: 1000))
    assert await manager.access_token() == saved.access_token
    replaced = replace(saved, access_token="new-rotated-token")
    write_credentials_json(replaced.credentials(), path)
    assert await manager.access_token() == "new-rotated-token"
    path.unlink()
    with pytest.raises(MatrixOAuthError, match="missing or unreadable"):
        await manager.access_token()


async def test_credential_lock_async_waits_then_releases(tmp_path, monkeypatch):
    import asyncio
    from medre.adapters.matrix.oauth_session import (
        credential_lock,
        credential_lock_async,
    )

    path = tmp_path / "matrix.json"
    entered = asyncio.Event()
    contended = asyncio.Event()
    from contextlib import contextmanager
    import medre.adapters.matrix.oauth_session as module

    @contextmanager
    def observed_lock(path):
        with credential_lock(path):
            yield

    # Signal when the waiting task actually attempts to enter the held lock.
    def attempt(path):
        contended.set()
        return observed_lock(path)

    monkeypatch.setattr(module, "credential_lock", attempt)

    async def acquire():
        async with credential_lock_async(path, timeout_seconds=2.0):
            entered.set()

    with credential_lock(path):
        task = asyncio.create_task(acquire())
        await asyncio.wait_for(contended.wait(), timeout=1.0)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=1.0)
    assert entered.is_set()


async def test_configured_sidecar_credential_provenance_survives_token_refresh(
    tmp_path, monkeypatch
):
    from medre.config.adapters.matrix import MatrixConfig
    from medre.adapters.matrix.oauth_session import manager_for_credentials
    from dataclasses import replace

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "medre" / "credentials" / "matrix.json"
    saved = make_session()
    write_credentials_json(saved.credentials(), path)
    config = MatrixConfig("matrix", "", "").validate()
    assert config.oauth_sidecar_token is True
    rotated = replace(saved, access_token="rotated-again")
    write_credentials_json(rotated.credentials(), path)
    assert (
        manager_for_credentials(
            config.homeserver,
            config.user_id,
            config.device_id,
            config.access_token,
            path=path,
            managed_source=config.oauth_sidecar_token,
        )
        is not None
    )
    assert (
        manager_for_credentials(
            config.homeserver,
            config.user_id,
            config.device_id,
            config.access_token,
            path=path,
        )
        is None
    )


def test_password_logout_treats_only_unknown_token_401_as_success(monkeypatch):
    import io
    from urllib.error import HTTPError
    import medre.adapters.matrix.auth as auth
    from medre.adapters.matrix.errors import MatrixConnectionError

    def urlopen(req, **kwargs):
        raise HTTPError(
            req.full_url,
            401,
            "unauthorized",
            {},
            io.BytesIO(b'{"errcode":"M_UNKNOWN_TOKEN"}'),
        )

    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)
    auth.matrix_logout("https://matrix.example", "revoked-token")

    def bad_logout(req, **kwargs):
        raise HTTPError(
            req.full_url,
            401,
            "unauthorized",
            {},
            io.BytesIO(b'{"errcode":"M_FORBIDDEN"}'),
        )

    monkeypatch.setattr(auth.urllib.request, "urlopen", bad_logout)
    with pytest.raises(MatrixConnectionError, match="Logout failed"):
        auth.matrix_logout("https://matrix.example", "revoked-token")


async def test_oauth_signing_bootstrap_budget_exceeds_browser_window(
    tmp_path, monkeypatch
):
    import sys
    from types import ModuleType
    from medre.adapters.matrix.auth import MatrixLoginResult
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap

    saved = make_session()
    seen = []

    class Client:
        homeserver = saved.homeserver
        access_token = saved.access_token
        config = SimpleNamespace(custom_headers={})
        send = AsyncMock()
        logged_in = True
        olm = object()
        store = object()

        def restore_login(self, **kwargs):
            self.access_token = kwargs["access_token"]

    class Service:
        def __init__(self, client, *, logger=None, operation_timeout_seconds=120.0):
            seen.append(operation_timeout_seconds)

        async def reconcile(self, **kwargs):
            return "already_signed"

        def diagnostics(self):
            return SimpleNamespace(chain_status="valid")

    fake_nio = ModuleType("nio")
    fake_nio.AsyncClientConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    fake_nio.AsyncClient = lambda **kwargs: Client()
    monkeypatch.setitem(sys.modules, "nio", fake_nio)
    monkeypatch.setattr(bootstrap._compat_mod, "HAS_NIO", True)
    monkeypatch.setattr(bootstrap._compat_mod, "HAS_E2EE", True)
    monkeypatch.setattr(bootstrap, "MatrixCrossSigningService", Service)
    monkeypatch.setattr(bootstrap, "matrix_store_path_for_adapter", lambda a: tmp_path)
    monkeypatch.setattr(bootstrap, "close_matrix_client", AsyncMock())
    monkeypatch.setattr(bootstrap, "attach_oauth", lambda client, manager: None)
    monkeypatch.setattr(
        sys.modules["medre.adapters.matrix.oauth_approval"],
        "signing_approval",
        lambda send, issuer: send,
    )
    manager = SimpleNamespace(
        session=saved, access_token=AsyncMock(return_value=saved.access_token)
    )
    await bootstrap.bootstrap_login_cross_signing(
        MatrixLoginResult(
            saved.homeserver, saved.user_id, saved.device_id, saved.access_token
        ),
        None,
        adapter_id="matrix",
        oauth_manager=manager,
    )
    assert seen == [300.0]


async def test_initial_token_is_loaded_before_enabling_cache(tmp_path, monkeypatch):
    from dataclasses import replace
    import medre.adapters.matrix.oauth_session as module

    saved = make_session()
    path = tmp_path / "matrix.json"
    rotated = replace(saved, access_token="rotated-before-manager-start")
    write_credentials_json(rotated.credentials(), path)
    manager = OAuthSessionManager(saved, path, SimpleNamespace(clock=lambda: 1000))
    loader = module.load_credentials_json
    loads = []

    def tracked_load(path):
        loads.append(path)
        return loader(path)

    monkeypatch.setattr(module, "load_credentials_json", tracked_load)
    assert await manager.access_token() == rotated.access_token
    assert await manager.access_token() == rotated.access_token
    assert loads == [path]


@pytest.mark.parametrize(
    "replacement",
    [
        None,
        {"auth_type": "password"},
        {
            "auth_type": "oauth",
            "homeserver": "https://other.example",
        },
    ],
)
def test_managed_source_cannot_fall_back_to_unmanaged_token(tmp_path, replacement):
    from medre.adapters.matrix.oauth_session import manager_for_credentials

    path = tmp_path / "matrix.json"
    if replacement is not None:
        write_credentials_json(replacement, path)
    saved = make_session()
    with pytest.raises(MatrixOAuthError, match="missing or changed"):
        manager_for_credentials(
            saved.homeserver,
            saved.user_id,
            saved.device_id,
            saved.access_token,
            path=path,
            managed_source=True,
        )


def test_unrelated_explicit_token_ignores_malformed_oauth_sidecar(tmp_path):
    from medre.adapters.matrix.oauth_session import manager_for_credentials

    saved = make_session()
    path = tmp_path / "matrix.json"
    write_credentials_json(
        {
            "auth_type": "oauth",
            "homeserver": saved.homeserver,
            "user_id": saved.user_id,
            "access_token": "unrelated-sidecar-token",
        },
        path,
    )
    assert (
        manager_for_credentials(
            saved.homeserver,
            saved.user_id,
            saved.device_id,
            "explicit-token",
            path=path,
        )
        is None
    )


async def test_credential_lock_wait_is_bounded_and_body_errors_are_not_retried(
    tmp_path,
):
    from medre.adapters.matrix.oauth_session import (
        credential_lock,
        credential_lock_async,
    )

    path = tmp_path / "matrix.json"
    with credential_lock(path):
        with pytest.raises(MatrixOAuthError, match="in use"):
            async with credential_lock_async(path, timeout_seconds=0):
                pytest.fail("Acquired a held credentials lock")
    executions = []
    with pytest.raises(MatrixOAuthError, match="protected operation failed"):
        async with credential_lock_async(path):
            executions.append("started")
            raise MatrixOAuthError("protected operation failed")
    assert executions == ["started"]
    async with credential_lock_async(path):
        pass


def test_explicit_token_override_discards_sidecar_renewal_authority(
    tmp_path, monkeypatch
):
    from dataclasses import fields, replace
    from medre.config.adapters.matrix import MatrixConfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    saved = make_session()
    path = tmp_path / "medre" / "credentials" / "matrix.json"
    write_credentials_json(saved.credentials(), path)
    config = MatrixConfig("matrix", "", "").validate()
    assert replace(config, origin_label="Mesh").oauth_sidecar_token is True
    assert (
        replace(config, access_token="explicit-override").oauth_sidecar_token is False
    )
    # Internal provenance is not a configurable adapter field or a JSON value.
    assert "oauth_sidecar_token" not in {field.name for field in fields(config)}
    assert json.loads(json.dumps(config.access_token)) == saved.access_token


@pytest.mark.parametrize("token", [123, True, {"invalid": "token"}])
def test_oauth_provenance_does_not_coerce_invalid_sidecar_tokens(
    tmp_path, monkeypatch, token
):
    from medre.config.adapters.matrix import MatrixConfig
    from medre.config.adapters.matrix_errors import MatrixConfigError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    record = make_session().credentials()
    record["access_token"] = token
    write_credentials_json(record)
    with pytest.raises(MatrixConfigError, match="access_token"):
        MatrixConfig("matrix", "", "").validate()


@pytest.mark.parametrize("blocked_phase", ["request", "challenge_body"])
async def test_signing_retry_deadline_covers_http_and_body_reads(
    monkeypatch, blocked_phase
):
    import asyncio
    import medre.adapters.matrix.oauth_approval as module

    class BlockedContent:
        async def iter_chunked(self, size):
            await asyncio.Event().wait()
            yield b"unreachable"

    calls = []

    async def original(*args):
        calls.append(args)
        if len(calls) == 1:
            return Response(401, challenge())
        if blocked_phase == "request":
            await asyncio.Event().wait()
        return SimpleNamespace(status=401, content=BlockedContent())

    # Expire at the first suspension inside the retry, without a wall-clock sleep.
    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(timeout=lambda _: asyncio.timeout(0))
    )
    clock = Clock()
    with pytest.raises(MatrixOAuthError, match="timed out"):
        await module.signing_approval(
            original,
            "https://auth.example",
            clock=clock,
            sleep=clock.sleep,
            display=lambda _: None,
        )("POST", "/_matrix/client/v3/keys/device_signing/upload", "{}")
    assert len(calls) == 2
