"""Installed mindroom-nio cross-signing contract guard.

This test is intentionally skipped in the base development environment and
becomes active when the Matrix E2EE extra is installed.  It protects the
provider surface MEDRE's identity policy depends on from silent SDK drift.
"""

from __future__ import annotations

import inspect
import json

import pytest
from medre_matrix_tests.helpers.sdk_contract import (
    assert_installed_extra_matches_declared_pins,
)


def test_mindroom_nio_exposes_cross_signing_contract() -> None:
    nio = pytest.importorskip("nio")
    # Import-name availability cannot distinguish the declared mindroom-nio
    # fork from another package exposing ``nio``. Verify the installed
    # distribution before feature-based skip paths can hide a pin mismatch.
    assert_installed_extra_matches_declared_pins(
        "matrix", ("mindroom-nio",), sdk_extra="e2e"
    )

    crypto = pytest.importorskip("nio.crypto")
    if not bool(getattr(crypto, "ENCRYPTION_ENABLED", False)):
        pytest.skip("nio E2EE support is not enabled")

    ensure = getattr(nio.AsyncClient, "ensure_cross_signing", None)
    send = getattr(nio.AsyncClient, "send", None)
    identity = getattr(nio.AsyncClient, "cross_signing_identity", None)
    assert callable(ensure)
    assert callable(send)
    assert isinstance(identity, property)
    assert "password" in inspect.signature(ensure).parameters
    assert list(inspect.signature(send).parameters)[:5] == [
        "self",
        "method",
        "path",
        "data",
        "headers",
    ]

    cross_signing = pytest.importorskip("nio.crypto.cross_signing")
    identity_cls = cross_signing.CrossSigningIdentity
    assert callable(getattr(identity_cls, "load", None))
    assert callable(getattr(identity_cls, "generate", None))
    assert callable(getattr(identity_cls, "self_signing_key_payload", None))
    assert callable(getattr(identity_cls, "signed_device_payload", None))
    assert callable(getattr(cross_signing, "cross_signing_sidecar_path", None))

    repair = getattr(nio.AsyncClient, "_upload_own_device_signature", None)
    assert callable(repair)


@pytest.mark.matrix_sdk
async def test_real_crypto_store_reuses_account_identity_for_fresh_device(tmp_path):
    nio = pytest.importorskip("nio")
    assert_installed_extra_matches_declared_pins(
        "matrix", ("mindroom-nio",), sdk_extra="e2e"
    )
    crypto = pytest.importorskip("nio.crypto")
    if not crypto.ENCRYPTION_ENABLED:
        pytest.skip("Matrix E2EE dependencies are unavailable")
    from nio.crypto.cross_signing import (
        CrossSigningIdentity,
        cross_signing_sidecar_path,
    )
    from medre.adapters.matrix.client_lifecycle import close_matrix_client
    from medre.adapters.matrix.identity import MatrixCrossSigningService

    user, device = "@relay:example", "FRESH_DEVICE"
    identity = CrossSigningIdentity.generate(user)
    identity.uploaded = True
    identity.signed_devices = ["PREVIOUS_DEVICE"]
    sidecar = cross_signing_sidecar_path(str(tmp_path), user)
    identity.save(sidecar)
    original_sidecar = sidecar.read_bytes()
    client = nio.AsyncClient(
        "https://matrix.example",
        user,
        device_id=device,
        store_path=str(tmp_path),
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    client.restore_login(user, device, "secret")
    device_keys = {}
    calls = []

    class Response:
        status = 200
        content_type = "application/json"
        content_disposition = None

        def __init__(self, document):
            self.document = document

        async def json(self, **kwargs):
            return self.document

        async def text(self):
            return json.dumps(self.document)

    async def send(method, path, data=None, headers=None, *args, **kwargs):
        from urllib.parse import urlsplit

        path = urlsplit(path).path
        calls.append(path)
        body = json.loads(data) if data else {}
        if path.endswith("/keys/query"):
            return Response(
                {
                    "master_keys": {user: identity.master_key_payload()},
                    "self_signing_keys": {user: identity.self_signing_key_payload()},
                    "device_keys": {user: device_keys},
                    "failures": {},
                }
            )
        if path.endswith("/keys/upload"):
            device_keys[device] = body["device_keys"]
            return Response({"one_time_key_counts": {"signed_curve25519": 50}})
        if path.endswith("/keys/signatures/upload"):
            assert (
                device in device_keys
            ), "device signature preceded device-key admission"
            device_keys[device].setdefault("signatures", {}).update(
                body[user][device]["signatures"]
            )
            return Response({"failures": {}})
        pytest.fail(f"unexpected SDK request {path}")

    client.send = send
    try:
        first = MatrixCrossSigningService(client)
        assert (
            await first.reconcile(password="password", allow_bootstrap=True)
            == "device_signed"
        )
        assert first.diagnostics().current_device_self_signed is True
        assert sidecar.read_bytes() == original_sidecar
        second = MatrixCrossSigningService(client)
        assert await second.reconcile() == "already_signed"
        assert calls.count("/_matrix/client/v3/keys/upload") == 1
        assert not any(path.endswith("/keys/device_signing/upload") for path in calls)
    finally:
        await close_matrix_client(client)
