"""Signing imports match the existing account and never replace local state."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.identity import MatrixCrossSigningService
from medre.adapters.matrix.signing_recovery import (
    inspect_signing_file,
    load_signing_backup,
    restore_signing_backup,
)
from test_matrix_cross_signing_identity import FakeClient, FakeIdentity


@pytest.mark.parametrize(
    "server,device_signed", [(True, True), (True, False), (False, False)]
)
async def test_import_verifier_queries_without_mutating(server, device_signed):
    """Only matching existing account identities can authorize an import."""
    identity = FakeIdentity("@bot:example.com")
    client = FakeClient(
        identity=identity, server_identity=server, device_signed=device_signed
    )
    service = MatrixCrossSigningService(client)
    if server:
        await service.verify_backup_identity(identity)
    else:
        with pytest.raises(MatrixConnectionError, match="no existing"):
            await service.verify_backup_identity(identity)
    assert client.ensure_calls == []
    assert client.repair_calls == 0


@pytest.fixture
def real_backup(tmp_path):
    """Use actual SDK serialization and signing without creating a device store."""
    from nio.crypto.cross_signing import CrossSigningIdentity

    identity = CrossSigningIdentity.generate("@bot:example.com")
    source = tmp_path / "backup.json"
    identity.save(source)
    return identity, source


def verifier_for(identity):
    """Return an authenticated read-only server view for the real account keys."""

    async def send(method, path, data, headers):
        assert method == "POST" and path == "/_matrix/client/v3/keys/query"
        return SimpleNamespace(
            status=200,
            json=AsyncMock(
                return_value={
                    "master_keys": {identity.user_id: identity.master_key_payload()},
                    "self_signing_keys": {
                        identity.user_id: identity.self_signing_key_payload()
                    },
                    "device_keys": {},
                    "failures": {},
                }
            ),
        )

    return MatrixCrossSigningService(
        SimpleNamespace(
            user_id=identity.user_id,
            device_id="DEVICE",
            access_token="test-token",
            send=send,
        )
    )


@pytest.mark.matrix_sdk
async def test_import_is_private_and_preserves_source_and_existing_files(
    real_backup, tmp_path
):
    """A real SDK backup can be imported, reopened, and never overwritten."""
    identity, source = real_backup
    before = source.read_bytes()
    store = tmp_path / "medre-store"
    assert inspect_signing_file(store, identity.user_id).state == "missing"
    target = await restore_signing_backup(
        source, store, identity.user_id, verifier_for(identity)
    )
    assert (
        load_signing_backup(target, identity.user_id).master_public_key
        == identity.master_public_key
    )
    assert source.read_bytes() == before
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    assert inspect_signing_file(store, identity.user_id).state == "present"
    with pytest.raises(MatrixConnectionError, match="already exists"):
        await restore_signing_backup(
            source, store, identity.user_id, verifier_for(identity)
        )
    assert target.read_bytes() == before
    assert sorted(p.name for p in store.iterdir()) == [target.name]


@pytest.mark.matrix_sdk
async def test_other_account_or_server_key_leaves_no_state(real_backup, tmp_path):
    """Neither a foreign-account backup nor conflicting server identity is admitted."""
    from nio.crypto.cross_signing import CrossSigningIdentity

    identity, source = real_backup
    store = tmp_path / "untouched"
    for user, verifier in [
        ("@other:example.com", verifier_for(identity)),
        (
            identity.user_id,
            verifier_for(CrossSigningIdentity.generate(identity.user_id)),
        ),
    ]:
        with pytest.raises(MatrixConnectionError):
            await restore_signing_backup(source, store, user, verifier)
        assert not store.exists()


@pytest.mark.matrix_sdk
async def test_racing_import_cannot_overwrite(real_backup, tmp_path, monkeypatch):
    """Even a destination created during server verification wins publication."""
    from nio.crypto.cross_signing import cross_signing_sidecar_path

    identity, source = real_backup
    store = tmp_path / "store"
    target = Path(cross_signing_sidecar_path(str(store), identity.user_id))
    verifier = verifier_for(identity)

    async def raced(_identity):
        store.mkdir()
        target.write_text("existing-private-state")

    monkeypatch.setattr(verifier, "verify_backup_identity", raced)
    with pytest.raises(MatrixConnectionError, match="Existing files were not replaced"):
        await restore_signing_backup(source, store, identity.user_id, verifier)
    assert target.read_text() == "existing-private-state"


@pytest.mark.matrix_sdk
async def test_cancelled_query_and_invalid_backup_leave_no_import(
    real_backup, tmp_path, monkeypatch
):
    """Cancellation and malformed input leave the destination absent."""
    identity, source = real_backup
    store = tmp_path / "store"
    verifier = verifier_for(identity)
    monkeypatch.setattr(
        verifier,
        "verify_backup_identity",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await restore_signing_backup(source, store, identity.user_id, verifier)
    assert not store.exists()
    source.write_text("malformed PRIVATE_SENTINEL")
    with pytest.raises(MatrixConnectionError) as failure:
        await restore_signing_backup(source, store, identity.user_id, verifier)
    assert "PRIVATE_SENTINEL" not in str(failure.value)
    assert not store.exists()


def test_status_uses_selected_store_without_network(tmp_path, monkeypatch, capsys):
    """Operator output names the exact path and explains why login is insufficient."""
    from medre.adapters.matrix.signing_cli import signing_status
    from medre.adapters.matrix.signing_recovery import SigningFileStatus
    from medre.config.adapters.matrix import MatrixConfig

    target = tmp_path / "signing.json"
    monkeypatch.setattr(
        "medre.adapters.matrix.signing_cli.inspect_signing_file",
        lambda *_: SigningFileStatus(target, "missing"),
    )
    signing_status(
        MatrixConfig(
            adapter_id="matrix",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="PRIVATE_SENTINEL",
            store_path=str(tmp_path),
        )
    )
    output = capsys.readouterr().out
    assert str(target) in output
    assert "restore-signing" in output
    assert "Ordinary login cannot reconstruct" in output
    assert "PRIVATE_SENTINEL" not in output


@pytest.mark.matrix_sdk
async def test_link_failure_fails_closed(real_backup, tmp_path, monkeypatch):
    """Unsupported filesystems never fall back to overwriting a destination."""
    identity, source = real_backup
    store = tmp_path / "store"

    def unsupported(*_args, **_kwargs):
        raise OSError("no hard links")

    monkeypatch.setattr("medre.adapters.matrix.signing_recovery.os.link", unsupported)
    with pytest.raises(MatrixConnectionError, match="hard links"):
        await restore_signing_backup(
            source, store, identity.user_id, verifier_for(identity)
        )
    assert not list(store.iterdir())


@pytest.mark.matrix_sdk
@pytest.mark.skipif(os.name != "posix", reason="Directory fsync is POSIX-only")
async def test_post_publication_error_reports_existing_complete_file(
    real_backup, tmp_path, monkeypatch
):
    """A directory durability failure must not masquerade as no publication."""
    from nio.crypto.cross_signing import cross_signing_sidecar_path

    identity, source = real_backup
    store = tmp_path / "store"
    original_fsync = os.fsync

    def fail_directory(fd):
        if os.path.samestat(os.fstat(fd), store.stat()):
            raise OSError("directory sync failed")
        original_fsync(fd)

    monkeypatch.setattr(
        "medre.adapters.matrix.signing_recovery.os.fsync", fail_directory
    )
    with pytest.raises(MatrixConnectionError, match="may already be published"):
        await restore_signing_backup(
            source, store, identity.user_id, verifier_for(identity)
        )
    target = Path(cross_signing_sidecar_path(str(store), identity.user_id))
    assert (
        load_signing_backup(target, identity.user_id).master_public_key
        == identity.master_public_key
    )


@pytest.mark.matrix_sdk
async def test_command_rejects_other_token_owner_and_closes_client(
    real_backup, tmp_path, monkeypatch
):
    """whoami ownership is verified before any account-key query or local write."""
    from medre.adapters.matrix.signing_cli import restore_signing
    from medre.config.adapters.matrix import MatrixConfig

    identity, source = real_backup
    client = SimpleNamespace(
        restore_login=lambda *_: None,
        whoami=AsyncMock(
            return_value=SimpleNamespace(
                user_id="@other:example.com", device_id="OTHER"
            )
        ),
        close=AsyncMock(),
    )

    def factory(*_args, **kwargs):
        assert kwargs["config"].encryption_enabled is False
        assert "store_path" not in kwargs
        return client

    monkeypatch.setattr("nio.AsyncClient", factory)
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth_session.manager_for_credentials",
        lambda *_args, **_kwargs: None,
    )
    config = MatrixConfig(
        adapter_id="matrix",
        homeserver="https://example.com",
        user_id=identity.user_id,
        access_token="test-token",
        store_path=str(tmp_path / "untouched"),
    )
    with pytest.raises(MatrixConnectionError, match="account/device"):
        await restore_signing(config, source)
    client.close.assert_awaited_once()
    assert not (tmp_path / "untouched").exists()


@pytest.mark.parametrize(
    "state",
    ["unreadable", "invalid_or_unreadable", "provider_unavailable", "path_unavailable"],
)
def test_status_does_not_offer_an_unusable_restore_command(
    state, tmp_path, monkeypatch, capsys
):
    """Existing or uninspectable state needs diagnosis before an absent-file import."""
    from medre.adapters.matrix.signing_cli import signing_status
    from medre.adapters.matrix.signing_recovery import SigningFileStatus
    from medre.config.adapters.matrix import MatrixConfig

    monkeypatch.setattr(
        "medre.adapters.matrix.signing_cli.inspect_signing_file",
        lambda *_: SigningFileStatus(tmp_path / "signing.json", state),
    )
    signing_status(
        MatrixConfig(
            adapter_id="selected",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            store_path=str(tmp_path),
        )
    )
    output = capsys.readouterr().out
    assert "medre adapter matrix auth restore-signing" not in output
    if state in {"unreadable", "invalid_or_unreadable"}:
        assert "move the original aside manually" in output
        assert "permissions" in output
    else:
        assert "missing keys have not been established" in output


@pytest.mark.parametrize(
    "category,restore,reset,login",
    [
        ("local_identity_missing", True, True, True),
        ("identity_mismatch", True, True, True),
        ("bootstrap_required", False, False, True),
        ("device_signature_repair_failed", False, False, False),
        ("signature_repair_unsupported", False, False, False),
        ("provider_error", False, False, False),
        ("operation_timeout", False, False, False),
    ],
)
async def test_runtime_guidance_matches_failure_category(
    category, restore, reset, login, monkeypatch, caplog
):
    """A device repair or initial bootstrap must never recommend resetting account keys."""
    import logging
    from medre.adapters.matrix.identity import MatrixCrossSigningDiagnostics
    from medre.adapters.matrix.session import MatrixSession
    from medre.config.adapters.matrix import MatrixConfig

    diagnostics = MatrixCrossSigningDiagnostics(
        last_failure_category=category,
        reset_required=reset,
        repair_required=category
        in {
            "bootstrap_required",
            "device_signature_repair_failed",
            "signature_repair_unsupported",
        },
    )
    service = SimpleNamespace(
        reconcile=AsyncMock(return_value=None), diagnostics=lambda: diagnostics
    )
    monkeypatch.setattr(
        "medre.adapters.matrix.session.MatrixCrossSigningService",
        lambda *_args, **_kwargs: service,
    )
    session = MatrixSession(
        MatrixConfig(
            adapter_id="selected",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="test-token",
        )
    )
    session._crypto_enabled = True
    session._client = object()
    with caplog.at_level(logging.INFO):
        await session._reconcile_cross_signing_runtime()
    assert ("auth restore-signing" in caplog.text) is restore
    assert ("--reset-cross-signing" in caplog.text) is reset
    assert ("auth login" in caplog.text) is login


@pytest.mark.matrix_sdk
async def test_whoami_transport_failure_is_sanitized(
    real_backup, tmp_path, monkeypatch
):
    """Non-OSError transport failures use the CLI's safe error path and close HTTP."""
    from medre.adapters.matrix.signing_cli import restore_signing
    from medre.config.adapters.matrix import MatrixConfig

    identity, source = real_backup
    client = SimpleNamespace(
        restore_login=lambda *_: None,
        whoami=AsyncMock(side_effect=RuntimeError("PRIVATE_SENTINEL")),
        close=AsyncMock(),
    )
    monkeypatch.setattr("nio.AsyncClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        "medre.adapters.matrix.oauth_session.manager_for_credentials",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(MatrixConnectionError, match="Cannot verify") as failure:
        await restore_signing(
            MatrixConfig(
                adapter_id="matrix",
                homeserver="https://example.com",
                user_id=identity.user_id,
                access_token="test-token",
                store_path=str(tmp_path / "untouched"),
            ),
            source,
        )
    assert "PRIVATE_SENTINEL" not in str(failure.value)
    client.close.assert_awaited_once()
    assert not (tmp_path / "untouched").exists()
