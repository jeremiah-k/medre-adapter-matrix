"""Capture Matrix state under the same local lease used by the pinned SDK.

Preview imports no SDK and discovers no files. Capture resolves only the
configured account's database, trust sidecars and MEDRE credential sidecar
while core retains the native store's existing exclusive lifetime lease.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from medre.maintenance.stopped_declare import (
    DeclaredFile,
    ExclusionRefused,
    LocalExclusion,
    ProviderCoverage,
    StoppedPlanError,
    StoppedStateDeclaration,
    declared,
)


def _store_path(config: object, adapter_id: str) -> Path:
    """Mirror the prepared runtime store location without opening its SDK."""
    value = getattr(config, "store_path", None)
    if isinstance(value, str) and value:
        return Path(value).expanduser().absolute()
    from medre.config.paths import resolve

    return resolve().adapter_transport_state_dir(adapter_id, "matrix") / "store"


def _credentials_sidecar() -> Path:
    """Use the same SDK-free credential path authority as normal configuration."""
    from medre.config.adapters.matrix_credentials import get_credentials_path

    return get_credentials_path().absolute()


def _account_stem(config: object) -> str | None:
    """Mirror the pinned SDK's account naming and reject path-bearing identities."""
    user = getattr(config, "user_id", None)
    device = getattr(config, "device_id", None)
    if (
        not isinstance(user, str)
        or not user
        or not isinstance(device, str)
        or not device
    ):
        return None
    if any(character in user + device for character in ("/", "\\", "\x00")):
        raise StoppedPlanError("Matrix account identity cannot contain path separators")
    return f"{user}_{device}"


def _regular(path: Path) -> None:
    """Refuse aliases and special inputs before native ownership or file selection."""
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ExclusionRefused("Matrix state path traverses a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ExclusionRefused("Matrix state requires an unaliased regular file")


class MatrixStoppedState:
    """Select exact account state without claiming remote-device exclusion."""

    provider = "matrix"

    def coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage:
        """Preview paths; native resolution and ownership remain mandatory for capture."""
        stem = _account_stem(config)
        if stem is None:
            return declared(
                provider=self.provider,
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
                notes=(
                    "configured user_id and device_id required; native identity is not guessed",
                ),
            )
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(
                DeclaredFile(
                    _store_path(config, adapter_id) / f"{stem}.db",
                    "native_identity",
                    "sqlite",
                    purpose="durable sync and crypto store",
                ),
            ),
            state_class="unsupported",
            notes=(
                "capture-config resolves account state only under the retained native store lease",
            ),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        """Retain the SDK's existing flock; never create or replace its owner file."""
        stem = _account_stem(config)
        if stem is None:
            raise ExclusionRefused("Matrix account identity is required")
        database = _store_path(config, adapter_id) / f"{stem}.db"
        _regular(database)
        lease = database.with_name(database.name + ".ingest.lock")
        _regular(lease)
        exclusion = LocalExclusion(self.provider, note="not retained")
        try:
            exclusion.retain_flock(lease, purpose="native store lifetime lease")
        except BaseException:
            exclusion.close()
            raise
        exclusion.note = "native SDK store lease retained locally; other hosts using the same device are not excluded"
        return exclusion

    def resolve_coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage:
        """Resolve a closed account selection while core retains native exclusion.

        SQLite backup includes committed WAL. Ordinary-store trust sidecars are
        preserved when present; durable adoption may already have imported their
        values into the database. Credentials remain selected at their actual
        XDG path, so the common capture root must include that path as well.
        """
        stem = _account_stem(config)
        if stem is None:
            return self.coverage(config, adapter_id=adapter_id)
        store = _store_path(config, adapter_id)
        database = store / f"{stem}.db"
        _regular(database)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _regular(sidecar)
        files = [
            DeclaredFile(
                database,
                "native_identity",
                "sqlite",
                purpose="durable sync, crypto and cross-signing state",
            )
        ]
        for suffix in ("trusted_devices", "blacklisted_devices", "ignored_devices"):
            path = store / f"{stem}.{suffix}"
            if path.exists() or path.is_symlink():
                _regular(path)
                files.append(
                    DeclaredFile(
                        path,
                        "native_identity",
                        "opaque",
                        purpose="native device trust sidecar",
                    )
                )
        credentials = _credentials_sidecar()
        if credentials.exists() or credentials.is_symlink():
            _regular(credentials)
            if credentials.stat().st_size > 1024 * 1024:
                raise StoppedPlanError(
                    "Matrix credential sidecar exceeds the capture bound"
                )
            with credentials.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024 or not isinstance(json.loads(raw), dict):
                raise StoppedPlanError(
                    "Matrix credential sidecar is not a bounded object"
                )
            files.append(
                DeclaredFile(
                    credentials,
                    "native_identity",
                    "opaque",
                    purpose="MEDRE access-token sidecar",
                )
            )
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=files,
            notes=(
                "SQLite backup retains committed WAL; lock and transient SQLite files are not restored",
                "keep runtime configuration and any separately supplied secrets; capture does not discover configuration files",
                "colon-bearing SDK names require a POSIX restore host; configure restored store and XDG locations before activation",
                "stop the original owner before activating the restored device; local exclusion cannot fence another host",
            ),
        )


def declaration() -> StoppedStateDeclaration:
    """Return pure preview, retained native-store exclusion and capture resolution."""
    return MatrixStoppedState()
