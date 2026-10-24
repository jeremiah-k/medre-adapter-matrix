"""Bridge committed nio batches to MEDRE admission without advancing early.

The SDK owns capture, crypto, history recovery and its upstream cursor. MEDRE
owns semantic admission. A batch is acknowledged only after every record and
its application evidence have been accepted; crash replay relies on native-ID
deduplication at MEDRE's admission boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from medre.core.ingress.types import DurableIngressDeferredError
from medre.adapters.matrix.errors import MatrixAttachmentFetchDeferredError

if TYPE_CHECKING:
    from medre.adapters.matrix.oauth import OAuthSession
    from nio import AsyncClient
    from nio.durable import DurableSync, SyncBatch, SyncRecord

# Callback tasks inherit the exact record, never another consumer's provenance.
current_record: ContextVar[SyncRecord | None] = ContextVar(
    "matrix_committed_record", default=None
)


def is_adopted_store_error(error: BaseException) -> bool:
    """Recognize the pinned SDK's explicit format guard, not arbitrary I/O failure."""
    try:
        from nio.exceptions import LocalProtocolError
    except ImportError:
        return False

    return isinstance(error, LocalProtocolError) and str(error) == (
        "this database requires a durable sync session"
    )


def _canonical_homeserver(value: str) -> str:
    """Normalize URL spelling without conflating distinct Matrix origins."""
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid Matrix homeserver URL")
    host = parts.hostname.lower().rstrip(".")
    if not host:
        raise ValueError("invalid Matrix homeserver URL")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    authority = host + (f":{port}" if port is not None and port != default_port else "")
    return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))


def _durable_database_name(user_id: str, device_id: str) -> str:
    """Locate the durable database exactly where the installed SDK puts it.

    Released SDKs build the default name inline as ``{user_id}_{device_id}``;
    development trees expose the same POSIX derivation through
    ``nio._storage_names.account_filename_stem``.
    """
    try:
        from nio._storage_names import account_filename_stem
    except ImportError:
        return f"{user_id}_{device_id}.db"
    return f"{account_filename_stem(user_id, device_id)}.db"


def _existing_consumer_id(
    store_path: Path, user_id: str, device_id: str
) -> UUID | None:
    """Read only the already-adopted binding; leave validation to the SDK."""
    database = store_path / _durable_database_name(user_id, device_id)
    if not database.is_file():
        return None
    uri = f"file:{quote(database.resolve().as_posix(), safe='/')}?mode=ro"
    # closing() is required: sqlite3's transaction context manager does not
    # close the connection, and a leaked read handle outlives this probe.
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='NioDurableMeta'"
        ).fetchone()
        if not tables:
            return None
        row = connection.execute(
            "SELECT consumer_id FROM NioDurableMeta WHERE id=1"
        ).fetchone()
        return UUID(row[0]) if row else None


def open_sync(
    client: AsyncClient,
    *,
    adapter_id: str,
    homeserver: str,
    store_path: Path,
    sync_timeout_ms: int,
    legacy_cursor: str | None,
    managed_session: OAuthSession | None = None,
) -> DurableSync:
    """Adopt existing crypto state and the application's Classic cursor.

    The ordinary SDK store API seeds the token before durable adoption. Its
    ownership guard refuses adopted databases before writes; that documented
    refusal permits reopening the durable owner after a crash in the handoff.
    Other errors propagate, including outstanding legacy recovery at adoption.
    """
    from nio.store import DefaultStore
    from nio.crypto import Olm
    from nio.durable import DurableSyncConfig, open_durable_sync
    from nio.exceptions import LocalProtocolError

    # A verified sidecar binds discovered/delegated and configured base URLs to
    # the same login; never trust an unrelated account's credential record.
    from medre.config.adapters.matrix_credentials import load_credentials_json

    candidates = [homeserver]
    saved = load_credentials_json()
    if managed_session is not None:
        if (managed_session.user_id, managed_session.device_id) != (
            client.user_id,
            client.device_id,
        ):
            raise LocalProtocolError(
                "managed OAuth identity differs from durable client"
            )
        # The owning manager already authenticated this device. Tokens rotate;
        # its account/device/homeserver binding does not.
        candidates.insert(0, managed_session.homeserver)
    elif (
        saved is not None
        and saved.get("user_id") == client.user_id
        and saved.get("device_id") == client.device_id
        and saved.get("access_token") == client.access_token
        and isinstance(saved.get("homeserver"), str)
    ):
        candidates.insert(0, saved["homeserver"])

    def identity(server: str) -> UUID:
        """Derive an unambiguous account/instance binding for one authorized URL."""
        return uuid5(
            NAMESPACE_URL,
            "medre:matrix:" + "\0".join((server, client.user_id, adapter_id)),
        )

    desired = identity(_canonical_homeserver(candidates[0]))
    # Previously adopted stores used the literal URL. Preserve that binding;
    # never rewrite SDK consumer IDs, and never accept arbitrary stored IDs.
    legacy_ids = {identity(candidate.rstrip("/")) for candidate in candidates}
    authorized_ids = legacy_ids | {
        identity(_canonical_homeserver(candidate)) for candidate in candidates
    }
    stored = _existing_consumer_id(store_path, client.user_id, client.device_id)
    if stored is not None:
        if stored not in authorized_ids:
            raise LocalProtocolError("durable consumer identity mismatch")
        desired = stored
    store_path.mkdir(parents=True, exist_ok=True)
    if legacy_cursor:
        try:
            ordinary = DefaultStore(
                client.user_id,
                client.device_id,
                str(store_path),
                pickle_key=client.config.pickle_key,
            )
        except LocalProtocolError as error:
            # This is the pinned SDK's explicit ordinary/durable format guard.
            # Never interpret an arbitrary store failure as successful adoption.
            if not is_adopted_store_error(error):
                raise
        else:
            try:
                # Plaintext installations can have a core cursor without any
                # SDK account row. Seed through the SDK's account constructor,
                # preserving an existing pickle instead of inventing SQL state.
                Olm(client.user_id, client.device_id, ordinary)
                ordinary.save_sync_token(legacy_cursor)
            finally:
                ordinary.database.close()
    return open_durable_sync(
        client,
        consumer_id=desired,
        store_path=store_path,
        config=DurableSyncConfig(sync_timeout_ms=sync_timeout_ms),
        source_store_class=DefaultStore,
    )


class MatrixBatchAcceptanceError(Exception):
    """Application failure requiring repair; the SDK batch remains unacknowledged."""


class MatrixBatchConsumer:
    """One producer and one ordered consumer with explicit acknowledgement."""

    def __init__(
        self,
        owner: DurableSync,
        *,
        accept_loss: Callable[[SyncRecord], Awaitable[None]],
        accepted: Callable[[SyncBatch], Awaitable[None]],
        retry_delay: float = 0.25,
        acknowledged: Callable[[SyncBatch], None] | None = None,
    ) -> None:
        """Bind admission, durable evidence and post-ack progress to one SDK owner."""
        self.owner = owner
        self.accept_loss = accept_loss
        self.accepted = accepted
        self.retry_delay = retry_delay
        self.acknowledged = acknowledged
        self.last_acked_sequence: int | None = None

    async def consume(self) -> None:
        """Retry deferred records in place; never acknowledge failed admission."""
        while True:
            batch = await self.owner.next_batch()
            if batch is None:
                await self.owner.wait_for_work()
                continue
            for record in batch.records:
                media_attempts = capacity_attempts = 0
                while True:
                    token = current_record.set(record)
                    try:
                        if record.kind == "loss":
                            await self.accept_loss(record)
                        else:
                            await self.owner.dispatch(record)
                        break
                    except DurableIngressDeferredError as error:
                        if isinstance(error, MatrixAttachmentFetchDeferredError):
                            delay = min(120.0, 15.0 * 2 ** min(media_attempts, 4))
                            media_attempts += 1
                        else:
                            delay = min(
                                30.0, self.retry_delay * 2 ** min(capacity_attempts, 16)
                            )
                            capacity_attempts += 1
                        await asyncio.sleep(delay)
                    except Exception as error:
                        raise MatrixBatchAcceptanceError(
                            f"Matrix record acceptance failed ({type(error).__name__}): {error}"
                        ) from error
                    finally:
                        current_record.reset(token)
            checkpoint_attempts = 0
            while True:
                try:
                    await self.accepted(batch)
                    break
                except DurableIngressDeferredError:
                    delay = min(
                        30.0, self.retry_delay * 2 ** min(checkpoint_attempts, 16)
                    )
                    checkpoint_attempts += 1
                    await asyncio.sleep(delay)
                except Exception as error:
                    raise MatrixBatchAcceptanceError(
                        f"Matrix batch evidence failed ({type(error).__name__}): {error}"
                    ) from error
            await self.owner.ack(batch)
            self.last_acked_sequence = batch.sequence
            if self.acknowledged is not None:
                self.acknowledged(batch)

    async def run(self) -> None:
        """Retire both tasks together when either side fails or is cancelled."""
        producer = asyncio.create_task(self.owner.run(), name="matrix-durable-source")
        consumer = asyncio.create_task(self.consume(), name="matrix-durable-consumer")
        try:
            done, _ = await asyncio.wait(
                {producer, consumer}, return_when=asyncio.FIRST_COMPLETED
            )
            # Retrieve both exceptions in finally, even if both fail together.
            for task in (producer, consumer):
                if task in done:
                    await task
            raise RuntimeError("Matrix durable sync exited unexpectedly")
        finally:
            for task in (producer, consumer):
                task.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
