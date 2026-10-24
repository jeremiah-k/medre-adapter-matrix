"""Inspect and restore account signing keys without replacing an identity."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from medre.adapters.matrix.errors import MatrixConnectionError

if TYPE_CHECKING:
    from medre.adapters.matrix.identity import MatrixCrossSigningService


class SigningIdentity(Protocol):
    """SDK private identity used transiently during explicit recovery."""

    user_id: str
    master_seed: bytes
    self_signing_seed: bytes
    master_public_key: str
    self_signing_public_key: str

    def save(self, path: Path) -> None: ...


@dataclass(frozen=True)
class SigningFileStatus:
    """Secret-free file status; never retain the decoded private identity."""

    path: Path | None
    state: str


def load_signing_backup(source: Path, user_id: str) -> SigningIdentity:
    """Validate a small, readable SDK backup belonging to the selected account."""
    from nio.crypto.cross_signing import CrossSigningIdentity

    try:
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("not a small regular backup")
        identity = CrossSigningIdentity.load(source)
        if identity is None:
            raise FileNotFoundError
        if identity.user_id != user_id:
            raise ValueError("another account")
        if len(identity.master_seed) != 32 or len(identity.self_signing_seed) != 32:
            raise ValueError("invalid seed length")
        _ = identity.master_public_key, identity.self_signing_public_key
    except OSError:
        raise MatrixConnectionError(f"Cannot read signing backup: {source}") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MatrixConnectionError(
            f"Invalid signing backup for the selected account: {source}"
        ) from None
    return cast(SigningIdentity, identity)


def inspect_signing_file(store_path: Path, user_id: str) -> SigningFileStatus:
    """Inspect the SDK-owned filename without modifying state or querying Matrix."""
    try:
        from nio.crypto.cross_signing import cross_signing_sidecar_path
    except ImportError:
        return SigningFileStatus(None, "provider_unavailable")
    try:
        path = Path(cross_signing_sidecar_path(str(store_path), user_id))
    except (OSError, ValueError):
        return SigningFileStatus(None, "path_unavailable")
    try:
        path.lstat()
    except FileNotFoundError:
        return SigningFileStatus(path, "missing")
    except OSError:
        return SigningFileStatus(path, "unreadable")
    try:
        load_signing_backup(path, user_id)
    except MatrixConnectionError:
        return SigningFileStatus(path, "invalid_or_unreadable")
    return SigningFileStatus(path, "present")


async def restore_signing_backup(
    source: Path,
    store_path: Path,
    user_id: str,
    verifier: MatrixCrossSigningService,
) -> Path:
    """Match account keys against Matrix, then atomically publish without overwrite.

    Hard-link publication deliberately fails closed on unsupported filesystems;
    a check-then-replace fallback would overwrite a concurrent import. Existing
    device databases, credentials, source backups and server keys are untouched.
    """
    from nio.crypto.cross_signing import cross_signing_sidecar_path

    identity = load_signing_backup(source, user_id)
    target = Path(cross_signing_sidecar_path(str(store_path), user_id))
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise MatrixConnectionError(
            f"Signing file already exists; refusing replacement: {target}"
        )
    await verifier.verify_backup_identity(identity)
    # No private state or directories are created before server validation.
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".signing-import-", dir=target.parent
        ) as temporary:
            staging = Path(temporary) / "identity.json"
            identity.save(staging)
            staging.chmod(0o600)
            with staging.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.link(staging, target)
            if os.name == "posix":
                fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except OSError:
        raise MatrixConnectionError(
            f"Signing import could not finish at {target}. Check auth status before "
            "retrying; a complete file may already be published. Existing files "
            "were not replaced. The filesystem must support hard links."
        ) from None
    return target
