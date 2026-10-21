"""Credential publication is bounded, private, and atomic."""

import json
import os
import stat

import pytest
from medre.config.adapters.matrix_credentials import (
    load_credentials_json,
    write_credentials_json,
)


def test_credentials_replace_failure_preserves_original_record(tmp_path, monkeypatch):
    path = tmp_path / "matrix.json"
    write_credentials_json({"access_token": "ORIGINAL"}, path)

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_credentials_json({"access_token": "NEW"}, path)
    assert json.loads(path.read_text()) == {"access_token": "ORIGINAL"}
    assert not list(tmp_path.glob(".matrix-*"))


def test_oversized_credential_write_preserves_original_record(tmp_path):
    path = tmp_path / "matrix.json"
    write_credentials_json({"access_token": "ORIGINAL"}, path)
    with pytest.raises(OSError, match="size limit"):
        write_credentials_json({"access_token": "x" * 65536}, path)
    assert load_credentials_json(path) == {"access_token": "ORIGINAL"}


@pytest.mark.parametrize("contents", ["[]", "null", '"scalar"', "{", "x" * 65537])
def test_invalid_credential_records_are_unreadable(tmp_path, contents):
    path = tmp_path / "matrix.json"
    path.write_text(contents)
    assert load_credentials_json(path) is None


def test_private_permissions_apply_before_credentials_are_published(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not Windows ACLs")
    path = tmp_path / "matrix.json"
    replace = os.replace

    def check(source, destination):
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        replace(source, destination)

    monkeypatch.setattr(os, "replace", check)
    write_credentials_json({"access_token": "secret"}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_directory_sync_failure_after_replace_does_not_revoke_published_token(
    tmp_path, monkeypatch, caplog
):
    if os.name == "nt":
        pytest.skip("Directory fsync applies only on POSIX")
    path = tmp_path / "matrix.json"
    original = os.fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("directory sync unavailable PRIVATE_TOKEN")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    caplog.set_level("DEBUG", logger="medre.config.adapters.matrix_credentials")
    write_credentials_json({"access_token": "still-valid"}, path)
    assert "parent-directory sync unavailable" in caplog.text
    assert "PRIVATE_TOKEN" not in caplog.text
    assert load_credentials_json(path) == {"access_token": "still-valid"}
    assert len(calls) == 2
