"""Provider-owned route preparation: allowlist validation of source contexts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from medre.adapters.matrix.runtime import prepare_matrix_runtime_config
from medre_matrix_tests.helpers.matrix_adapter import make_matrix_config


def _route(source_channel: str, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        source=SimpleNamespace(adapter="matrix-1", channel=source_channel),
        targets=[],
    )


def test_allowlist_rejects_alias_format_source_channels() -> None:
    config = make_matrix_config(
        room_allowlist={"!room:example.com"}, store_path="/tmp/store"
    )
    with pytest.raises(ValueError, match="must be a room ID"):
        prepare_matrix_runtime_config(
            config,
            adapter_id="matrix-1",
            paths=SimpleNamespace(),
            routes=[_route("#room:server.org")],
        )


def test_allowlist_accepts_listed_room_ids_and_merges_auto_join() -> None:
    config = make_matrix_config(
        room_allowlist={"!room:example.com"}, store_path="/tmp/store"
    )
    prepared = prepare_matrix_runtime_config(
        config,
        adapter_id="matrix-1",
        paths=SimpleNamespace(),
        routes=[_route("!room:example.com")],
    )
    assert prepared.auto_join_rooms == ("!room:example.com",)


def test_allowlist_check_skips_disabled_routes_and_other_adapters() -> None:
    config = make_matrix_config(
        room_allowlist={"!room:example.com"}, store_path="/tmp/store"
    )
    prepared = prepare_matrix_runtime_config(
        config,
        adapter_id="matrix-1",
        paths=SimpleNamespace(),
        routes=[
            _route("#elsewhere:server.org", enabled=False),
            SimpleNamespace(
                enabled=True,
                source=SimpleNamespace(adapter="other", channel="#x:server.org"),
                targets=[
                    SimpleNamespace(adapter="matrix-1", channel="!room:example.com")
                ],
            ),
        ],
    )
    assert prepared.auto_join_rooms == ("!room:example.com",)


def test_alias_source_is_invalid_even_when_no_allowlist_is_configured() -> None:
    """Absent allowlist cannot turn an unmatchable alias into live ingress."""
    config = make_matrix_config(room_allowlist=None, store_path="/tmp/store")
    with pytest.raises(ValueError, match="must be a room ID"):
        prepare_matrix_runtime_config(
            config,
            adapter_id="matrix-1",
            paths=SimpleNamespace(),
            routes=[_route("#room:server.org")],
        )
