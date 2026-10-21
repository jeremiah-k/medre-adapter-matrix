"""Windows credential ownership releases its region before closing the handle."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from medre.adapters.matrix.oauth import MatrixOAuthError


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("unlock_fails", [False, True])
def test_windows_lock_is_released_before_close(
    tmp_path, monkeypatch, outcome, unlock_fails
):
    import medre.adapters.matrix.oauth_session as module

    calls = []

    def locking(fd, operation, size):
        calls.append((fd, operation, os.lseek(fd, 0, os.SEEK_CUR), size))
        os.fstat(fd)  # Unlocking must happen while the descriptor is open.
        if operation == 2 and unlock_fails:
            raise OSError("unlock unavailable")

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    )

    def operation():
        with module.credential_lock(tmp_path / "matrix.json"):
            if outcome == "error":
                raise ValueError("protected operation")
            if outcome == "cancel":
                raise asyncio.CancelledError

    if outcome == "success":
        operation()
    else:
        with pytest.raises(
            ValueError if outcome == "error" else asyncio.CancelledError
        ):
            operation()
    assert [(mode, offset, size) for _, mode, offset, size in calls] == [
        (1, 0, 1),
        (2, 0, 1),
    ]
    with pytest.raises(OSError):
        os.fstat(calls[0][0])


def test_failed_windows_acquisition_does_not_unlock_another_owner(
    tmp_path, monkeypatch
):
    import medre.adapters.matrix.oauth_session as module

    calls = []

    def locking(fd, mode, size):
        calls.append((fd, mode))
        raise OSError("busy")

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    )
    with pytest.raises(MatrixOAuthError, match="in use"):
        with module.credential_lock(tmp_path / "matrix.json"):
            pytest.fail("Acquired another owner's lock")
    assert len(calls) == 1
    with pytest.raises(OSError):
        os.fstat(calls[0][0])
