"""Real SDK key continuity across setup and durable runtime ownership.

The homeserver protocol is controlled; crypto, persisted stores, admission to
SDK ownership, and closure use the pinned SDK. These are SDK-boundary checks,
not evidence of remote account recovery or a live homeserver session.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from medre.adapters.matrix.auth import MatrixLoginResult
from medre.adapters.matrix.client_lifecycle import close_matrix_client
from medre.adapters.matrix.durable_sync import open_sync
from medre.adapters.matrix.e2ee_bootstrap import bootstrap_login_cross_signing
from medre.adapters.matrix.identity import MatrixCrossSigningService
from medre.adapters.matrix.errors import MatrixConnectionError
from medre_matrix_tests.helpers.sdk_contract import (
    assert_installed_extra_matches_declared_pins,
)

pytestmark = pytest.mark.matrix_sdk


class _Response:
    """Expose the HTTP response surface consumed by the real SDK."""

    status = 200
    content_type = "application/json"
    content_disposition = None

    def __init__(self, body: dict) -> None:
        self.body = body

    async def json(self, **kwargs):
        return self.body

    async def text(self):
        return json.dumps(self.body)


@pytest.mark.parametrize("recovery_state", ["intact", "missing", "server_rotated"])
async def test_signing_setup_and_durable_reopen_preserve_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_state: str
) -> None:
    """Re-authentication releases ownership and preserves both kinds of keys."""
    nio = pytest.importorskip("nio")
    crypto = pytest.importorskip("nio.crypto")
    if not crypto.ENCRYPTION_ENABLED:
        pytest.skip("Matrix crypto dependencies are required")
    assert_installed_extra_matches_declared_pins(
        "matrix", ("mindroom-nio",), sdk_extra="e2e"
    )
    from nio.crypto.cross_signing import (
        CrossSigningIdentity,
        cross_signing_sidecar_path,
    )
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap

    user, device, adapter_id = "@relay:example", "PERSISTED_DEVICE", "continuity"
    homeserver = "https://matrix.example"
    identity = CrossSigningIdentity.generate(user)
    identity.uploaded = True
    sidecar = cross_signing_sidecar_path(str(tmp_path), user)
    identity.save(sidecar)
    original_sidecar = sidecar.read_bytes()
    device_keys = {}
    calls: list[str] = []

    async def send(self, method, path, data=None, headers=None, *args, **kwargs):
        """Preserve server keys and refuse any attempt to rotate account keys."""
        path = urlsplit(path).path
        calls.append(path)
        body = json.loads(data) if data else {}
        if path.endswith("/keys/query"):
            return _Response(
                {
                    "master_keys": {user: identity.master_key_payload()},
                    "self_signing_keys": {user: identity.self_signing_key_payload()},
                    "device_keys": {user: device_keys},
                    "failures": {},
                }
            )
        if path.endswith("/keys/upload"):
            if "device_keys" in body:
                proposed = body["device_keys"]
                if device in device_keys:
                    assert proposed["keys"] == device_keys[device]["keys"]
                device_keys[device] = proposed
            return _Response({"one_time_key_counts": {"signed_curve25519": 50}})
        if path.endswith("/keys/signatures/upload"):
            assert device in device_keys
            device_keys[device].setdefault("signatures", {}).update(
                body[user][device]["signatures"]
            )
            return _Response({"failures": {}})
        pytest.fail(f"unexpected SDK request: {path}")

    monkeypatch.setattr(nio.AsyncClient, "send", send)
    monkeypatch.setattr(bootstrap, "matrix_store_path_for_adapter", lambda _: tmp_path)
    login = MatrixLoginResult(homeserver, user, device, "test-token")

    # Authenticated setup starts with an ordinary store and an existing account
    # identity. It may sign this device, but cannot replace the account keys.
    setup = await bootstrap_login_cross_signing(
        login, "test-password", adapter_id=adapter_id
    )
    assert setup.diagnostics.chain_status == "valid"
    assert sidecar.read_bytes() == original_sidecar
    original_device_keys = dict(device_keys[device]["keys"])

    # Runtime adopts the same database. Every later setup/runtime cycle must
    # reopen through the durable SDK owner and release that owner afterwards.
    for _ in range(2):
        client = nio.AsyncClient(
            homeserver,
            user,
            device_id=device,
            config=nio.AsyncClientConfig(encryption_enabled=True),
        )
        owner = None
        try:
            client.restore_login(user, device, login.access_token)
            owner = open_sync(
                client,
                adapter_id=adapter_id,
                homeserver=homeserver,
                store_path=tmp_path,
                sync_timeout_ms=1000,
                legacy_cursor=None,
            )
            assert {
                f"{algorithm}:{device}": key
                for algorithm, key in client.olm.account.identity_keys.items()
            } == original_device_keys
            service = MatrixCrossSigningService(client)
            assert await service.reconcile() == "already_signed"
            assert service.diagnostics().chain_status == "valid"
            assert sidecar.read_bytes() == original_sidecar
            assert device_keys[device]["keys"] == original_device_keys
        finally:
            try:
                if owner is not None:
                    await owner.close()
            finally:
                await close_matrix_client(client)
        setup = await bootstrap_login_cross_signing(
            login, "test-password", adapter_id=adapter_id
        )
        assert setup.diagnostics.chain_status == "valid"
        assert sidecar.read_bytes() == original_sidecar

    if recovery_state != "intact":
        if recovery_state == "missing":
            sidecar.unlink()
        else:
            # Another client can explicitly reset this account's server keys.
            # Re-authenticating here must not reset them again in response.
            identity = CrossSigningIdentity.generate(user)
        with pytest.raises(MatrixConnectionError, match="verified state"):
            await bootstrap_login_cross_signing(
                login, "test-password", adapter_id=adapter_id
            )
        if recovery_state == "missing":
            assert not sidecar.exists()
        else:
            assert sidecar.read_bytes() == original_sidecar
        assert device_keys[device]["keys"] == original_device_keys

    assert not any(path.endswith("/keys/device_signing/upload") for path in calls)
