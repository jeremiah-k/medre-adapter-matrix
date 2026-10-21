"""Native OAuth setup and revocation without running the bridge."""

from __future__ import annotations

import sys
from typing import Any

from medre.adapters.matrix.auth import (
    MatrixLoginResult,
    _normalize_homeserver,
    extract_domain_from_mxid,
)
from medre.adapters.matrix.oauth import (
    DeviceAuthorization,
    MatrixOAuthClient,
    MatrixOAuthError,
    OAuthSession,
    matches_user,
)
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.oauth_session import (
    OAuthSessionManager,
    credential_lock,
    finish_rotation,
)
from medre.config.adapters.matrix_credentials import (
    get_credentials_path,
    load_credentials_json,
    write_credentials_json,
)


def _display(prompt: DeviceAuthorization) -> None:
    print("Open this address in a browser on a trusted device:")
    print(prompt.url)
    print(f"Enter code: {prompt.code}")
    print("Approve only the MEDRE login you started. Keep this command running.")


async def oauth_login(args: Any) -> None:
    import asyncio

    if getattr(args, "password", None) is not None or getattr(
        args, "password_stdin", False
    ):
        raise MatrixOAuthError("--oauth cannot be combined with password options.")
    path = get_credentials_path()
    protocol = MatrixOAuthClient()
    expected = getattr(args, "user", None)
    server = getattr(args, "homeserver", None)
    with credential_lock(path):
        record = load_credentials_json(path)
        if path.exists() and record is None:
            raise MatrixOAuthError(
                "Credentials are unreadable; preserve the file and repair it before login."
            )
        if record is not None:
            if record.get("auth_type") != "oauth":
                raise MatrixOAuthError(
                    "Saved password credentials exist; use auth logout before switching to --oauth."
                )
            session = OAuthSession.parse(record)
            if not matches_user(session.user_id, expected):
                raise MatrixOAuthError(
                    "Saved OAuth credentials belong to another user; log out before changing accounts."
                )
            if (
                server
                and _normalize_homeserver(server).rstrip("/") != session.homeserver
            ):
                delegated, metadata = await protocol.discover(server)
                if (
                    delegated != session.homeserver
                    or metadata.get("issuer") != session.issuer
                ):
                    raise MatrixOAuthError(
                        "Saved OAuth credentials belong to another homeserver."
                    )
            if getattr(args, "client_id", None) not in (None, session.client_id):
                raise MatrixOAuthError(
                    "Saved OAuth credentials use another client registration."
                )
        else:
            if not server and expected:
                server = extract_domain_from_mxid(expected)
            if not server:
                raise MatrixOAuthError(
                    "OAuth login requires --homeserver or a complete --user MXID."
                )
            session = await protocol.authorize(
                server,
                _display,
                expected_user=expected,
                client_id=getattr(args, "client_id", None),
            )
            try:
                write_credentials_json(session.credentials(), path)
            except OSError:
                try:
                    await finish_rotation(asyncio.create_task(protocol.revoke(session)))
                except MatrixOAuthError:
                    print(
                        "Revoke the unsaved MEDRE session in your account settings.",
                        file=sys.stderr,
                    )
                raise MatrixOAuthError(
                    "OAuth credentials could not be saved."
                ) from None

    manager = OAuthSessionManager(session, path, protocol)
    token = await manager.access_token()
    session = manager.session
    print(f"Authenticated as {session.user_id} (device {session.device_id}).")
    print(f"Credentials: {path}")
    adapter_id = getattr(args, "adapter_id", None)
    if not adapter_id:
        print(
            "For E2EE device signing, repeat --oauth with --adapter-id matching the configured adapter."
        )
        return
    from medre.adapters.matrix.e2ee_bootstrap import bootstrap_login_cross_signing

    try:
        result = await bootstrap_login_cross_signing(
            MatrixLoginResult(
                session.homeserver, session.user_id, session.device_id, token
            ),
            None,
            adapter_id=adapter_id,
            reset=getattr(args, "reset_cross_signing", False),
            oauth_manager=manager,
        )
    except (MatrixConnectionError, MatrixOAuthError) as exc:
        raise MatrixOAuthError(
            "OAuth credentials are retained, but own-device signing did not "
            f"complete: {exc}. Correct the cause and repeat --oauth with the "
            "same --adapter-id."
        ) from None
    except Exception:
        # Keep the authenticated session and private identity material. Retrying
        # signing must not require another device login or an identity rotation.
        raise MatrixOAuthError(
            "OAuth credentials are retained, but own-device signing did not complete. Restore the existing E2EE store and repeat --oauth with the same --adapter-id. Use --reset-cross-signing only to deliberately replace a lost or conflicting account identity."
        ) from None
    print(f"Own Matrix device verified. E2EE store: {result.store_path}")


async def logout_credentials() -> None:
    path = get_credentials_path()
    with credential_lock(path):
        record = load_credentials_json(path)
        if record is None:
            raise MatrixOAuthError("No readable Matrix credentials were found.")
        if record.get("auth_type") == "oauth":
            session = OAuthSession.parse(record)
            await MatrixOAuthClient().revoke(session)
        else:
            import asyncio
            from medre.adapters.matrix.auth import (
                matrix_logout,
                check_credentials_completeness,
            )

            if record.get("auth_type") not in (
                None,
                "password",
            ) or check_credentials_completeness(record):
                raise MatrixOAuthError("Saved Matrix credentials are malformed.")
            await finish_rotation(
                asyncio.create_task(
                    asyncio.to_thread(
                        matrix_logout,
                        record["homeserver"],
                        record["access_token"],
                    )
                )
            )
        path.unlink()
    print("Matrix session revoked. E2EE keys and signing identity were retained.")
