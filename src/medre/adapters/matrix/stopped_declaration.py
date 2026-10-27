"""Matrix declaration: SDK-named durable store database and token sidecar.

Layout contract read from this distribution: the durable sync database and
the ordinary crypto store share the SDK-derived file name
``<user_id>_<device_id>.db`` inside the store directory; the access-token
sidecar lives under the XDG configuration home and is declared export-only.
The SDK owns its store lease; this maintenance declaration does not acquire it. This module mirrors those contracts
without importing the transport SDK, per the seam contract in
``medre.maintenance.stopped_declare``.
"""

from __future__ import annotations

from pathlib import Path

from medre.maintenance.stopped_declare import (
    DeclaredFile,
    LocalExclusion,
    ProviderCoverage,
    StoppedStateDeclaration,
    declared,
)


def _store_path(config: object, adapter_id: str) -> Path:
    value = getattr(config, "store_path", None)
    if isinstance(value, str) and value:
        return Path(value)
    # Mirror the runtime default from this adapter's e2ee bootstrap helper.
    from medre.config.paths import resolve

    return resolve().adapter_transport_state_dir(adapter_id, "matrix") / "store"


def _credentials_sidecar() -> Path:
    """Use the same SDK-free credential path authority as normal configuration."""
    from medre.config.adapters.matrix_credentials import get_credentials_path

    return get_credentials_path()


class MatrixStoppedState:
    """Provider-owned declaration of one configured Matrix bridge."""

    provider = "matrix"

    def coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage:
        """Declare the durable database when the device identity is configured."""
        user_id = getattr(config, "user_id", "")
        device_id = getattr(config, "device_id", None)
        if not user_id or not device_id:
            return declared(
                provider=self.provider,
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
                notes=(
                    "without a configured device_id the SDK names its store "
                    "databases only at runtime; capture is refused rather "
                    "than guessed",
                ),
            )
        store = _store_path(config, adapter_id)
        database = f"{user_id}_{device_id}.db"
        if ":" in database or "\\" in database:
            # The inventory path grammar rejects drive/colon spellings, and
            # the SDK derives this name from the account ID; generic
            # selection is impossible without remapping the store directory.
            return declared(
                provider=self.provider,
                adapter_id=adapter_id,
                files=(),
                state_class="externally_managed",
                notes=(
                    "the SDK store database name derived from this account "
                    "cannot be represented in the inventory path grammar; "
                    "prepare a provider export or relocate the store "
                    "directory before capture",
                ),
            )
        files = [
            DeclaredFile(
                store / database,
                "native_identity",
                "sqlite",
                purpose="durable sync and crypto store database",
            ),
            DeclaredFile(
                _credentials_sidecar(),
                "native_identity",
                "opaque",
                presence="export_only",
                purpose="access-token sidecar under the XDG configuration home",
            ),
        ]
        notes = [
            "the token sidecar is export-only by declaration policy; "
            "preserve it separately or provision credentials after restore",
            "no retained local ownership primitive is implemented by this declaration; "
            "stop every local process and confirm no other host uses the "
            "same account and device before capture",
            "database naming follows the released SDK inline form; SDK "
            "development trees may rename it, so verify against the store "
            "directory before capture",
        ]
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=files,
            notes=notes,
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        """Report honestly that no retained local ownership primitive exists."""
        return LocalExclusion(
            self.provider,
            note=(
                "maintenance does not retain the SDK store lease; local preflight cannot "
                "prove that another host with the same account and device "
                "is inactive"
            ),
        )


def declaration() -> StoppedStateDeclaration:
    """Return the provider-owned declaration for stopped-state planning."""
    return MatrixStoppedState()
