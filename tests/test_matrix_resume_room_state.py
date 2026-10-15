"""A durable cursor does not replace an SDK client's in-memory room state."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from medre.adapters.matrix.session import MatrixSession
from medre_matrix_tests.helpers.matrix_session import make_matrix_config


@pytest.mark.parametrize("cursor", [None, "durable-cursor"])
async def test_fresh_client_requests_full_room_state_even_with_a_saved_cursor(
    cursor: str | None,
) -> None:
    session = MatrixSession(make_matrix_config())
    session._committed_sync_token = cursor
    session._client = MagicMock(sync_forever=AsyncMock())

    await session._run_sync_forever_attempt()

    session._client.sync_forever.assert_awaited_once_with(
        timeout=session._config.sync_timeout_ms,
        since=cursor,
        full_state=True,
    )


async def test_reconnect_keeps_cursor_without_refetching_initialized_room_state() -> (
    None
):
    session = MatrixSession(make_matrix_config())
    session._committed_sync_token = "durable-cursor"
    session._initial_sync_done = True
    session._client = MagicMock(sync_forever=AsyncMock())

    await session._run_sync_forever_attempt()

    session._client.sync_forever.assert_awaited_once_with(
        timeout=session._config.sync_timeout_ms,
        since="durable-cursor",
        full_state=False,
    )
