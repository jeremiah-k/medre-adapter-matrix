"""Operator-selected signing recovery using existing credentials and state paths."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.runtime import prepare_matrix_runtime_config
from medre.adapters.matrix.signing_recovery import (
    inspect_signing_file,
    load_signing_backup,
    restore_signing_backup,
)
from medre.config.adapters.matrix import MatrixConfig


def signing_config(adapter_id: str, config_path: str | None = None) -> MatrixConfig:
    """Resolve the selected adapter's runtime-equivalent identity and store path."""
    if config_path:
        from medre.config.loader import load_config

        runtime, _, paths = load_config(config_path)
        selected = runtime.adapters.for_transport("matrix").get(adapter_id)
        if selected is None or not isinstance(selected.config, MatrixConfig):
            raise MatrixConnectionError(
                "Selected Matrix adapter is absent from config."
            )
        config = selected.config
    else:
        from medre.config.paths import resolve

        paths = resolve()
        config = MatrixConfig(
            adapter_id=adapter_id, homeserver="", user_id=""
        ).validate()
    return prepare_matrix_runtime_config(
        config, adapter_id=adapter_id, paths=paths, routes=()
    )


def signing_status(config: MatrixConfig, config_path: str | None = None) -> None:
    """Show actionable local status without a native client, database or network."""
    assert config.store_path is not None
    status = inspect_signing_file(Path(config.store_path), config.user_id)
    print(f"Adapter:      {config.adapter_id}")
    print(f"Account:      {config.user_id}")
    print(f"E2EE store:   {config.store_path}")
    print(f"Signing file: {status.path if status.path else 'unavailable from SDK'}")
    print(f"Local signing status: {status.state}")
    if status.state == "present":
        print(
            "Local account keys are readable. Server/device verification occurs at runtime."
        )
        return
    print(
        "Stop the bridge before restoring state. Ordinary login cannot reconstruct existing account signing keys."
    )
    if status.state in {"unreadable", "invalid_or_unreadable"}:
        print(
            "The signing file exists. Check its ownership/read permissions first; it may still contain usable keys."
        )
        print(
            "If confirmed unusable, preserve a private backup and move the original aside manually. Restore refuses existing files. Rerun this status command once the destination is absent."
        )
        return
    if status.state != "missing":
        print(
            "The SDK or store path could not be inspected. Check the installed Matrix SDK and configured state path before attempting restoration; missing keys have not been established."
        )
        return
    command = "medre adapter matrix auth restore-signing --adapter-id " + shlex.quote(
        config.adapter_id
    )
    if config_path:
        command += " --config " + shlex.quote(config_path)
    print("Validate and import an existing SDK signing JSON with:")
    print(command + " --from /path/to/account-signing-backup.json")
    print(
        "Check another store or backup for this same Matrix account (including MMRelay). Keep tokens and device databases in their original stores."
    )
    print(
        "If no matching backup exists, an authenticated --reset-cross-signing is an explicit last resort and requires device re-verification."
    )


async def restore_signing(config: MatrixConfig, source: Path) -> Path:
    """Import a server-matching account backup using a verified existing token."""
    import nio

    from medre.adapters.matrix.client_lifecycle import close_matrix_client
    from medre.adapters.matrix.identity import MatrixCrossSigningService
    from medre.adapters.matrix.oauth_session import (
        attach_oauth,
        manager_for_credentials,
    )

    assert config.store_path is not None
    load_signing_backup(source, config.user_id)
    manager = manager_for_credentials(
        config.homeserver,
        config.user_id,
        config.device_id,
        config.access_token,
        managed_source=config.oauth_sidecar_token,
    )
    client = nio.AsyncClient(
        config.homeserver,
        user=config.user_id,
        device_id=config.device_id or "",
        config=nio.AsyncClientConfig(encryption_enabled=False),
    )
    try:
        async with asyncio.timeout(30):
            token = await manager.access_token() if manager else config.access_token
            client.restore_login(config.user_id, config.device_id or "", token)
            if manager:
                attach_oauth(client, manager)
            try:
                who = await client.whoami()
            except (asyncio.CancelledError, MatrixConnectionError):
                raise
            except Exception as exc:
                raise MatrixConnectionError(
                    f"Cannot verify the saved token ({type(exc).__name__}); no keys were imported."
                ) from None
            if (
                getattr(who, "user_id", None) != config.user_id
                or not isinstance(getattr(who, "device_id", None), str)
                or not who.device_id
                or (config.device_id and who.device_id != config.device_id)
            ):
                raise MatrixConnectionError(
                    "Saved token does not verify the selected Matrix account/device; no keys were imported."
                )
            client.device_id = who.device_id
            return await restore_signing_backup(
                source,
                Path(config.store_path),
                config.user_id,
                MatrixCrossSigningService(client),
            )
    finally:
        await close_matrix_client(client)
