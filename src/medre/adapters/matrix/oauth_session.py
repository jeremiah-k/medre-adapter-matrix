"""Serialized OAuth renewal and authenticated SDK requests.

Refresh-token rotation and persistence finish together before cancellation is
reported. Cross-process locking prevents two bridges from spending one rotating
refresh token concurrently. The lock file contains no credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from medre.adapters.matrix.oauth import (
    MatrixOAuthClient,
    MatrixOAuthError,
    OAuthSession,
)
from medre.config.adapters.matrix_credentials import (
    get_credentials_path,
    load_credentials_json,
    write_credentials_json,
)

_Result = TypeVar("_Result")


def _record_stamp(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


async def finish_rotation(task: asyncio.Task[_Result]) -> _Result:
    """Drain credential rotation before releasing ownership on cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


@contextmanager
def credential_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        path.with_name(path.name + ".lock"),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(fd, "r+b") as stream:
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MatrixOAuthError(
                "Matrix credentials are in use by another operation; retry shortly."
            ) from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # Closing the descriptor remains the cleanup fallback.
                    pass


@asynccontextmanager
async def credential_lock_async(
    path: Path, *, timeout_seconds: float = 45.0
) -> AsyncIterator[None]:
    """Wait for another owner's rotation without blocking the event loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(credential_lock(path))
                break
            except MatrixOAuthError:
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(min(0.1, deadline - loop.time()))
        # Errors inside the protected operation must not trigger a reacquire.
        yield


class OAuthSessionManager:
    def __init__(
        self,
        session: OAuthSession,
        path: Path,
        protocol: MatrixOAuthClient | None = None,
    ) -> None:
        self.session = session
        self.path = path
        self.protocol = protocol or MatrixOAuthClient()
        self._lock = asyncio.Lock()
        # The supplied session may predate a cross-process replacement. Only
        # cache a token after reading its record under the credentials lock.
        self._stamp: tuple[int, int, int, int] | None = None

    async def access_token(self) -> str:
        if (
            self.protocol.clock() < self.session.refresh_at
            and self._stamp is not None
            and self._stamp == _record_stamp(self.path)
        ):
            return self.session.access_token
        async with self._lock:
            if (
                self.protocol.clock() < self.session.refresh_at
                and self._stamp is not None
                and self._stamp == _record_stamp(self.path)
            ):
                return self.session.access_token
            async with credential_lock_async(self.path):
                record = load_credentials_json(self.path)
                if record is None:
                    raise MatrixOAuthError(
                        "Saved OAuth credentials are missing or unreadable; authenticate again."
                    )
                saved = OAuthSession.parse(record)
                if any(
                    getattr(saved, name) != getattr(self.session, name)
                    for name in (
                        "homeserver",
                        "user_id",
                        "device_id",
                        "issuer",
                        "client_id",
                        "token_endpoint",
                        "revocation_endpoint",
                        "scope",
                    )
                ):
                    raise MatrixOAuthError(
                        "The saved OAuth session changed; restart the bridge."
                    )
                self.session = saved
                if self.protocol.clock() >= saved.refresh_at:
                    self.session = await finish_rotation(
                        asyncio.create_task(self._renew(saved))
                    )
                self._stamp = _record_stamp(self.path)
                return self.session.access_token

    async def _renew(self, session: OAuthSession) -> OAuthSession:
        try:
            refreshed = await self.protocol.refresh(session)
        except MatrixOAuthError:
            raise MatrixOAuthError(
                "OAuth renewal failed. Check connectivity; if the session was revoked, use auth logout and auth login --oauth. Password login was not attempted."
            ) from None
        try:
            write_credentials_json(refreshed.credentials(), self.path)
        except OSError:
            try:
                await self.protocol.revoke_unpersisted(refreshed, session)
            except MatrixOAuthError:
                pass
            raise MatrixOAuthError(
                "Rotated OAuth credentials could not be saved; authenticate again."
            ) from None
        return refreshed


def manager_for_credentials(
    homeserver: str,
    user_id: str,
    device_id: str | None,
    access_token: str,
    *,
    path: Path | None = None,
    managed_source: bool = False,
) -> OAuthSessionManager | None:
    """Find the managed identity even if its token rotated since config loading."""
    path = path or get_credentials_path()
    data = load_credentials_json(path)
    if not managed_source and (
        data is None or data.get("access_token") != access_token
    ):
        # An unrelated sidecar must not affect deliberately supplied tokens,
        # including when its OAuth record is malformed.
        return None
    if (
        not data
        or data.get("auth_type") != "oauth"
        or not isinstance(data.get("homeserver"), str)
        or (data["homeserver"].rstrip("/"), data.get("user_id"))
        != (homeserver.rstrip("/"), user_id)
    ):
        if managed_source:
            raise MatrixOAuthError(
                "Managed OAuth credentials are missing or changed; authenticate again."
            )
        return None
    session = OAuthSession.parse(data)
    if device_id is None and managed_source:
        raise MatrixOAuthError("Managed OAuth credentials require a Matrix device ID.")
    if device_id is not None and session.device_id != device_id:
        raise MatrixOAuthError(
            "Configured Matrix device differs from its OAuth credentials."
        )
    return OAuthSessionManager(session, path)


def attach_oauth(client: Any, manager: OAuthSessionManager) -> None:
    """Refresh before each HTTP attempt, including nio's internal retries.

    Only homeserver-relative paths may carry the token. Existing query tokens
    and Authorization headers are replaced, never carried into SDK retries.
    """
    original = client.send

    async def send(
        method: str,
        path: str,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        trace_context: Any = None,
        timeout: float | None = None,
    ) -> Any:
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
        ):
            raise MatrixOAuthError(
                "OAuth SDK requests must use homeserver-relative paths."
            )
        if client.homeserver.rstrip("/") != manager.session.homeserver.rstrip("/"):
            raise MatrixOAuthError(
                "OAuth credentials cannot be sent to another homeserver."
            )
        custom = getattr(client.config, "custom_headers", None) or {}
        if any(key.lower() == "authorization" for key in custom):
            raise MatrixOAuthError(
                "Custom Authorization headers conflict with managed OAuth."
            )
        token = await manager.access_token()
        client.access_token = token
        authenticated = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() != "authorization"
        }
        authenticated["Authorization"] = "Bearer " + token
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "access_token"
        ]
        relative = urlunsplit(("", "", parsed.path, urlencode(query), ""))
        return await original(
            method,
            relative,
            data=data,
            headers=authenticated,
            trace_context=trace_context,
            timeout=timeout,
        )

    client.send = send
