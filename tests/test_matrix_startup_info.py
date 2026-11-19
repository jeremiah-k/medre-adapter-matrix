"""Startup-info provider: facts the adapter already holds at ready.

Verifies the pure-read ``startup_info()`` provider against the core
startup-info boundary (duck-typed discovery, ``collect_startup_info``,
block rendering, cache round-trip) using fake clients only — no
homeserver, no SDK, no network I/O.  The logged-in client identity
(user ID and device ID) is reported only when the session's client
actually holds it; missing identity is omitted, never probed.
Access tokens and key material are never offered.
"""

from __future__ import annotations

import ast
import importlib.metadata
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from medre.core.startup_info import (
    CACHE_FILENAME,
    collect_startup_info,
    read_cache_file,
    render_startup_info_block,
    startup_info_callable,
    write_cache_file,
)
from medre_matrix_tests.helpers.matrix_adapter import (
    make_adapter_context as _make_adapter_context,
)
from medre_matrix_tests.helpers.matrix_adapter import (
    make_matrix_config as _make_matrix_config,
)
from medre_matrix_tests.helpers.matrix_adapter import (
    wire_mock_session as _wire_mock_session,
)

from medre.adapters.matrix import startup_info as startup_info_module
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.matrix import MatrixConfig

_EMITTED_AT = "2026-11-19T00:00:00+00:00"


def _identity_client(
    user_id: str | None = "@bot:example.com",
    device_id: str | None = "MOCKDEVICE1",
) -> SimpleNamespace:
    """A duck-typed client holding the given logged-in identity fields."""
    return SimpleNamespace(user_id=user_id, device_id=device_id)


def _fact_names(payload: dict[str, Any]) -> list[str]:
    return [f["name"] for row in payload["rows"] for f in row]


def _identity_facts(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(name, value, label) triples from the identity row, if any."""
    for row in payload["rows"]:
        if row and row[0]["name"] in {"user_id", "device_id"}:
            return [(f["name"], f["value"], f["label"]) for f in row]
    return []


# ---------------------------------------------------------------------------
# Discovery: core duck-types the callable on the adapter object
# ---------------------------------------------------------------------------


def test_real_adapter_exposes_discoverable_startup_info() -> None:
    adapter = MatrixAdapter(_make_matrix_config())
    discovered = startup_info_callable(adapter)
    assert discovered is not None
    assert callable(discovered)
    assert discovered.__name__ == "startup_info"


# ---------------------------------------------------------------------------
# Facts follow held state; nothing is probed
# ---------------------------------------------------------------------------


def test_unstarted_adapter_omits_client_identity() -> None:
    """No session yet, so no user or device identity is offered."""
    adapter = MatrixAdapter(_make_matrix_config())
    payload = adapter.startup_info()
    names = _fact_names(payload)
    assert "user_id" not in names
    assert "device_id" not in names
    assert "homeserver" in names


def test_held_client_identity_forms_the_first_row(monkeypatch) -> None:
    """The logged-in client's user and device identity lead, endpoint follows."""
    monkeypatch.setattr(startup_info_module, "_sdk_version", lambda: None)
    adapter = MatrixAdapter(_make_matrix_config())
    _wire_mock_session(adapter, _identity_client())
    payload = adapter.startup_info()
    assert len(payload["rows"]) == 2
    assert _identity_facts(payload) == [
        ("user_id", "@bot:example.com", "user"),
        ("device_id", "MOCKDEVICE1", "device"),
    ]
    assert [f["name"] for f in payload["rows"][1]] == ["homeserver"]
    assert payload["rows"][1][0]["value"] == "https://matrix.example.com"
    assert payload["rows"][1][0]["label"] == "homeserver"


@pytest.mark.parametrize(
    ("user_id", "device_id", "expected"),
    [
        pytest.param(
            None,
            "MOCKDEVICE1",
            [("device_id", "MOCKDEVICE1", "device")],
            id="device-only",
        ),
        pytest.param(
            "@bot:example.com",
            None,
            [("user_id", "@bot:example.com", "user")],
            id="user-only",
        ),
        pytest.param(
            "", "MOCKDEVICE1", [("device_id", "MOCKDEVICE1", "device")], id="empty-user"
        ),
        pytest.param(
            "@bot:example.com",
            "",
            [("user_id", "@bot:example.com", "user")],
            id="empty-device",
        ),
        pytest.param(None, None, [], id="nothing-held"),
    ],
)
def test_partial_identity_omits_missing_fields(
    user_id: str | None, device_id: str | None, expected: list[tuple[str, str, str]]
) -> None:
    """Absent identity fields are omitted, not fabricated or probed."""
    adapter = MatrixAdapter(_make_matrix_config())
    _wire_mock_session(adapter, _identity_client(user_id, device_id))
    assert _identity_facts(adapter.startup_info()) == expected


def test_non_string_identity_values_are_omitted() -> None:
    """A client holding non-string identity placeholders offers nothing."""
    adapter = MatrixAdapter(_make_matrix_config())
    client = SimpleNamespace(user_id=object(), device_id=42)
    _wire_mock_session(adapter, client)
    assert _identity_facts(adapter.startup_info()) == []


def test_clientless_session_omits_identity() -> None:
    """A session whose client was already torn down reports no identity."""
    adapter = MatrixAdapter(_make_matrix_config())
    _wire_mock_session(adapter, MagicMock(name="client"))
    adapter._session._client = None
    assert _identity_facts(adapter.startup_info()) == []


def test_homeserver_fact_follows_configured_endpoint() -> None:
    config = _make_matrix_config(homeserver="https://matrix2.example.org")
    adapter = MatrixAdapter(config)
    payload = adapter.startup_info()
    homeserver = [
        f for row in payload["rows"] for f in row if f["name"] == "homeserver"
    ]
    assert [(f["value"], f["label"]) for f in homeserver] == [
        ("https://matrix2.example.org", "homeserver")
    ]


async def test_stopped_adapter_reports_no_identity() -> None:
    """After stop() retires the session, only configured facts remain."""
    adapter = MatrixAdapter(_make_matrix_config())
    mock_session = MagicMock(name="session")
    mock_session.start = AsyncMock()
    mock_session.stop = AsyncMock()
    mock_session.client_identity = MagicMock(
        return_value=("@bot:example.com", "MOCKDEVICE1")
    )
    with (
        patch("medre.adapters.matrix.adapter.HAS_NIO", True),
        patch(
            "medre.adapters.matrix.adapter.MatrixSession",
            return_value=mock_session,
        ),
    ):
        _, ctx = _make_adapter_context("matrix-1")
        await adapter.start(ctx)
        assert _identity_facts(adapter.startup_info()) != []
        await adapter.stop()
    assert adapter._session is None
    assert _identity_facts(adapter.startup_info()) == []


# ---------------------------------------------------------------------------
# SDK version fact: installed metadata only, never a literal
# ---------------------------------------------------------------------------


def test_sdk_version_fact_tracks_installed_distribution() -> None:
    """The SDK version comes from installed metadata or is omitted."""
    try:
        installed = importlib.metadata.version(startup_info_module._SDK_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    payload = startup_info_module.build_startup_info(
        homeserver="https://matrix.example.com"
    )
    names = _fact_names(payload)
    if installed is None:
        assert "sdk_version" not in names
    else:
        value = next(
            f["value"]
            for row in payload["rows"]
            for f in row
            if f["name"] == "sdk_version"
        )
        assert value == installed


def test_sdk_version_fact_is_offered_when_held(monkeypatch) -> None:
    monkeypatch.setattr(startup_info_module, "_sdk_version", lambda: "9.9.9")
    payload = startup_info_module.build_startup_info(
        homeserver="https://matrix.example.com"
    )
    sdk = [f for row in payload["rows"] for f in row if f["name"] == "sdk_version"]
    assert [(f["value"], f["label"]) for f in sdk] == [("9.9.9", "sdk")]


# ---------------------------------------------------------------------------
# Secrets are never offered; pure read is stable on worker threads
# ---------------------------------------------------------------------------


def test_access_token_never_appears_in_payload() -> None:
    config = _make_matrix_config(access_token="s3cr3t-token-value")
    adapter = MatrixAdapter(config)
    _wire_mock_session(adapter, _identity_client())
    payload = adapter.startup_info()

    def _walk(value: object) -> None:
        if isinstance(value, str):
            assert "s3cr3t-token-value" not in value
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(payload)


def test_startup_info_snapshot_is_safe_from_worker_threads() -> None:
    """Core runs synchronous providers off-loop; the read must be stable."""
    adapter = MatrixAdapter(_make_matrix_config())
    _wire_mock_session(adapter, _identity_client())
    baseline = adapter.startup_info()
    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(lambda _: adapter.startup_info(), range(32)))
    assert all(payload == baseline for payload in payloads)
    assert adapter._session.client_identity() == (
        "@bot:example.com",
        "MOCKDEVICE1",
    )


def test_provider_module_imports_nothing_from_core() -> None:
    """The provider stays duck-typed: plain data, no medre.core coupling."""
    source = Path(startup_info_module.__file__ or "").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == "medre.core" or alias.name.startswith("medre.core.")
            )
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module == "medre.core" or module.startswith("medre.core."):
                offenders.append(module)
    assert offenders == []


# ---------------------------------------------------------------------------
# Core boundary: collection, rendering, cache
# ---------------------------------------------------------------------------


def _started_adapter_with_identity(
    monkeypatch,
    **config_overrides: Any,
) -> tuple[MatrixAdapter, MagicMock, tuple[Any, Any]]:
    """Build an adapter whose started session holds a client identity."""
    monkeypatch.setattr(startup_info_module, "_sdk_version", lambda: None)
    adapter = MatrixAdapter(_make_matrix_config(**config_overrides))
    mock_session = MagicMock(name="session")
    mock_session.start = AsyncMock()
    mock_session.stop = AsyncMock()
    mock_session.connected = True
    mock_session.is_logged_in = MagicMock(return_value=True)
    mock_session.last_successful_sync = time.monotonic()
    mock_session.last_sync_error = None
    mock_session.reconnecting = False
    mock_session.crypto_enabled = False
    mock_session.client_identity = MagicMock(
        return_value=("@bot:example.com", "MOCKDEVICE1")
    )
    patcher = (
        patch("medre.adapters.matrix.adapter.HAS_NIO", True),
        patch(
            "medre.adapters.matrix.adapter.MatrixSession",
            return_value=mock_session,
        ),
    )
    return adapter, mock_session, patcher


async def test_started_adapter_emits_held_facts_through_core(monkeypatch) -> None:
    """At the ready transition the held identity renders through core."""
    adapter, _mock_session, patchers = _started_adapter_with_identity(monkeypatch)
    with patchers[0], patchers[1]:
        _, ctx = _make_adapter_context("matrix-1")
        await adapter.start(ctx)
        try:
            info = await collect_startup_info(
                "matrix-1", "matrix", adapter, emitted_at=_EMITTED_AT
            )
            assert info is not None
            assert [(f.name, f.row, f.label) for f in info.fields] == [
                ("user_id", 0, "user"),
                ("device_id", 0, "device"),
                ("homeserver", 1, "homeserver"),
            ]
            assert info.fields[0].value == "@bot:example.com"
            assert info.fields[1].value == "MOCKDEVICE1"
            assert render_startup_info_block("matrix.matrix-1  ready", info) == (
                "matrix.matrix-1  ready\n"
                "  user @bot:example.com · device MOCKDEVICE1\n"
                "  homeserver https://matrix.example.com"
            )
        finally:
            await adapter.stop()


async def test_degraded_unsynced_adapter_still_emits_held_identity(
    monkeypatch,
) -> None:
    """Degraded-until-first-sync readiness does not delay or gate facts."""
    adapter, mock_session, patchers = _started_adapter_with_identity(
        monkeypatch,
        # Disable the first-sync wait so start() completes in the degraded
        # "waiting for the first sync acknowledgement" state immediately.
        initial_sync_timeout_seconds=0,
    )
    # No sync acknowledgement yet: the adapter stays degraded while the
    # identity it already holds still renders.
    mock_session.last_successful_sync = None
    with patchers[0], patchers[1]:
        _, ctx = _make_adapter_context("matrix-1")
        await adapter.start(ctx)
        try:
            info = await collect_startup_info(
                "matrix-1", "matrix", adapter, emitted_at=_EMITTED_AT
            )
            assert info is not None
            assert [f.name for f in info.fields] == [
                "user_id",
                "device_id",
                "homeserver",
            ]
        finally:
            await adapter.stop()


async def test_facts_pass_the_core_cache_boundary(monkeypatch, tmp_path: Path) -> None:
    adapter, _mock_session, patchers = _started_adapter_with_identity(monkeypatch)
    with patchers[0], patchers[1]:
        _, ctx = _make_adapter_context("matrix-1")
        await adapter.start(ctx)
        try:
            info = await collect_startup_info(
                "matrix-1", "matrix", adapter, emitted_at=_EMITTED_AT
            )
            assert info is not None
            path = tmp_path / CACHE_FILENAME
            write_cache_file(path, info)
            cached = read_cache_file(path, adapter_id="matrix-1", transport="matrix")
            assert cached == info.with_cached_flags(True)
            assert (
                read_cache_file(path, adapter_id="matrix-1", transport="meshtastic")
                is None
            )
        finally:
            await adapter.stop()


def test_config_only_facts_for_unheld_identity_surface() -> None:
    """Only held identity and endpoint facts are offered."""
    config: MatrixConfig = _make_matrix_config()
    adapter = MatrixAdapter(config)
    names = _fact_names(adapter.startup_info())
    assert "user_id" not in names and "device_id" not in names
