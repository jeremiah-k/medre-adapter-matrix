"""SDK-free preview and retained ownership for exact Matrix account capture."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.adapters.matrix import stopped_declaration as module
from medre.adapters.matrix.plugin import adapter_spec
from medre.maintenance.stopped_declare import ExclusionRefused, declaration_from_spec


def settings(tmp_path, **overrides):
    """Use one fixed account and a separate configured store directory."""
    return SimpleNamespace(
        **dict(
            {
                "user_id": "@relay:example.org",
                "device_id": "DEVICE1",
                "store_path": str(tmp_path / "store"),
            },
            **overrides,
        )
    )


def selected(tmp_path, monkeypatch):
    """Create bounded native placeholders; no SDK import or profile activation."""
    config = settings(tmp_path)
    store = Path(config.store_path)
    store.mkdir()
    database = store / "@relay:example.org_DEVICE1.db"
    database.touch()
    database.with_name(database.name + ".ingest.lock").touch()
    credentials = tmp_path / "matrix.json"
    credentials.write_text(json.dumps({"access_token": "private-test-token"}))
    monkeypatch.setattr(module, "_credentials_sidecar", lambda: credentials)
    return config, database, credentials


def test_spec_resolves_the_provider_declaration():
    resolved = declaration_from_spec(adapter_spec())
    assert resolved.provider == "matrix"
    assert adapter_spec().stopped_state_declaration.module == module.__name__


def test_preview_is_configuration_only_and_requires_native_resolution(tmp_path):
    coverage = module.declaration().coverage(settings(tmp_path), adapter_id="bridge")
    assert coverage.state_class == "unsupported"
    assert coverage.files[0].path.name == "@relay:example.org_DEVICE1.db"
    assert not (tmp_path / "store").exists()


def test_runtime_derived_device_is_not_guessed(tmp_path):
    coverage = module.declaration().coverage(
        settings(tmp_path, device_id=None), adapter_id="bridge"
    )
    assert coverage.state_class == "unsupported" and coverage.files == ()


@pytest.mark.skipif(os.name == "nt", reason="native SDK lifetime lease requires flock")
def test_capture_selects_database_credentials_and_present_trust_state(
    tmp_path, monkeypatch
):
    config, database, credentials = selected(tmp_path, monkeypatch)
    trust = database.with_name("@relay:example.org_DEVICE1.trusted_devices")
    trust.write_text("trust data")
    provider = module.declaration()
    exclusion = provider.local_exclusion(config, adapter_id="bridge")
    try:
        assert exclusion.scope == "local_only"
        coverage = provider.resolve_coverage(config, adapter_id="bridge")
        assert {item.path for item in coverage.files} == {database, credentials, trust}
        assert all(item.presence == "required" for item in coverage.files)
        with pytest.raises(ExclusionRefused):
            provider.local_exclusion(config, adapter_id="bridge")
    finally:
        exclusion.close()
    provider.local_exclusion(config, adapter_id="bridge").close()


@pytest.mark.parametrize(
    "name",
    [
        "@relay:example.org_DEVICE1.db",
        "@relay:example.org_DEVICE1.db.ingest.lock",
        "matrix.json",
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="native account filenames require POSIX")
def test_hardlinked_state_is_refused(tmp_path, monkeypatch, name):
    config, database, credentials = selected(tmp_path, monkeypatch)
    target = credentials if name == "matrix.json" else database.parent / name
    (tmp_path / "alias").hardlink_to(target)
    provider = module.declaration()
    with pytest.raises(ExclusionRefused):
        if name == "matrix.json":
            provider.resolve_coverage(config, adapter_id="bridge")
        else:
            provider.local_exclusion(config, adapter_id="bridge")


def test_missing_store_cannot_claim_exclusion(tmp_path):
    with pytest.raises((ExclusionRefused, FileNotFoundError)):
        module.declaration().local_exclusion(settings(tmp_path), adapter_id="bridge")


@pytest.mark.parametrize(
    "field,value",
    [("user_id", "../relay"), ("device_id", "../device"), ("device_id", "a/b")],
)
def test_identity_cannot_escape_configured_store(tmp_path, field, value):
    with pytest.raises(ValueError):
        module.declaration().coverage(
            settings(tmp_path, **{field: value}), adapter_id="bridge"
        )
