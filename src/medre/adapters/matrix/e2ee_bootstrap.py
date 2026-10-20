"""Authenticated Matrix E2EE identity bootstrap helpers.

Basic Matrix password login intentionally remains in :mod:`medre.adapters.matrix.auth`
and keeps its stdlib-only dependency boundary.  This module is imported only when
an operator explicitly asks the auth workflow to prepare an adapter's E2EE store
and cross-signing identity.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import medre.adapters.matrix.compat as _compat_mod
from medre.adapters.matrix.auth import MatrixLoginResult
from medre.adapters.matrix.client_lifecycle import close_matrix_client
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.identity import (
    MatrixCrossSigningDiagnostics,
    MatrixCrossSigningService,
)
from medre.adapters.matrix.oauth_session import OAuthSessionManager, attach_oauth
from medre.config.paths import resolve

__all__ = [
    "MatrixE2EEBootstrapResult",
    "bootstrap_login_cross_signing",
    "matrix_store_path_for_adapter",
]


@dataclass(frozen=True)
class MatrixE2EEBootstrapResult:
    """Result of preparing one adapter's E2EE identity state."""

    store_path: Path
    provider_result: str
    diagnostics: MatrixCrossSigningDiagnostics


def matrix_store_path_for_adapter(adapter_id: str) -> Path:
    """Return the runtime-equivalent Matrix E2EE store for *adapter_id*."""
    paths = resolve()
    return paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"


async def bootstrap_login_cross_signing(
    login: MatrixLoginResult,
    password: str | None,
    *,
    adapter_id: str,
    reset: bool = False,
    oauth_manager: OAuthSessionManager | None = None,
    logger: logging.Logger | None = None,
) -> MatrixE2EEBootstrapResult:
    """Prepare E2EE state and verify the logged-in Matrix device.

    The access token and password are used only for this in-process bootstrap.
    The password is never persisted.  ``adapter_id`` selects the same per-adapter
    store path that the runtime builder derives later.
    """
    if not adapter_id:
        raise ValueError("adapter_id must be non-empty for Matrix E2EE bootstrap")
    if not password and oauth_manager is None:
        raise ValueError("password is required for Matrix E2EE bootstrap")
    if not _compat_mod.HAS_NIO or not _compat_mod.HAS_E2EE:
        raise MatrixConnectionError(
            "Matrix cross-signing setup requires mindroom-nio[e2e]; "
            "install 'medre-adapter-matrix[e2e]'"
        )
    if not login.device_id:
        raise MatrixConnectionError(
            "Matrix login did not return a device_id; cannot prepare E2EE identity"
        )

    import nio

    store_path = matrix_store_path_for_adapter(adapter_id)
    store_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    store_path.chmod(0o700)
    try:
        client_config = nio.AsyncClientConfig(encryption_enabled=True)
        client = nio.AsyncClient(
            homeserver=login.homeserver,
            user=login.user_id,
            device_id=login.device_id,
            store_path=str(store_path),
            config=client_config,
        )
    except Exception as exc:
        raise MatrixConnectionError(f"Failed to configure Matrix E2EE: {exc}") from exc

    try:
        token = login.access_token
        if oauth_manager is not None:
            session = oauth_manager.session
            if (session.homeserver, session.user_id, session.device_id) != (
                login.homeserver,
                login.user_id,
                login.device_id,
            ):
                raise MatrixConnectionError(
                    "OAuth session differs from the E2EE setup identity"
                )
            token = await oauth_manager.access_token()
            attach_oauth(client, oauth_manager)
            from medre.adapters.matrix.oauth_approval import signing_approval

            client.send = signing_approval(client.send, session.issuer)
        client.restore_login(
            user_id=login.user_id,
            device_id=login.device_id,
            access_token=token,
        )
        if not getattr(client, "logged_in", False):
            raise MatrixConnectionError(
                "Matrix E2EE bootstrap could not restore the authenticated session"
            )
        if (
            getattr(client, "olm", None) is None
            or getattr(client, "store", None) is None
        ):
            raise MatrixConnectionError(
                "Matrix E2EE bootstrap could not initialize the crypto store"
            )

        service = MatrixCrossSigningService(client, logger=logger)
        authorization = (
            {"oauth_authenticated": True} if oauth_manager is not None else {}
        )
        provider_result = await service.reconcile(
            password=password,
            allow_bootstrap=True,
            reset=reset,
            **authorization,
        )
        diagnostics = service.diagnostics()
        if provider_result is None or diagnostics.chain_status != "valid":
            category = diagnostics.last_failure_category or "unknown"
            raise MatrixConnectionError(
                "Matrix cross-signing setup did not reach a verified state "
                f"(category={category})"
            )
        return MatrixE2EEBootstrapResult(
            store_path=store_path,
            provider_result=provider_result,
            diagnostics=diagnostics,
        )
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await close_matrix_client(client, logger)
        except Exception:
            (logger or logging.getLogger(__name__)).debug(
                "Matrix E2EE bootstrap client close failed", exc_info=True
            )
