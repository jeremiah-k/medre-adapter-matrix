"""Matrix stopped-state declaration truthfulness.

The declaration names the SDK-derived durable database only when the device
identity is configured, reports the grammar reality of standard account IDs,
and admits honestly that no local ownership primitive exists. No SDK import
and no homeserver contact is involved.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from medre.adapters.matrix.plugin import adapter_spec
from medre.adapters.matrix.stopped_declaration import declaration
from medre.maintenance.stopped_declare import (
    DeclaredFile,
    declaration_from_spec,
    declared,
    plan_from_coverages,
)


def _settings(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "user_id": "@relay:example.org",
        "device_id": "DEVICE1",
        "store_path": str(tmp_path / "store"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_spec_references_the_declaration_module():
    reference = adapter_spec().stopped_state_declaration
    assert reference is not None
    assert reference.module == "medre.adapters.matrix.stopped_declaration"
    assert reference.name == "declaration"
    assert declaration().provider == "matrix"


def test_core_resolution_loads_the_declaration_through_the_spec():
    resolved = declaration_from_spec(adapter_spec())
    assert resolved.provider == "matrix"


def test_plan_selects_representable_store_and_skips_export_only_sidecar(tmp_path):
    state_root = tmp_path / "state"
    coverage = declaration().coverage(
        _settings(tmp_path, user_id="relay", store_path=str(state_root / "store")),
        adapter_id="bridge",
    )
    core = declared(
        provider="core",
        adapter_id="core",
        files=(DeclaredFile(state_root / "medre.db", "core_sqlite", "sqlite"),),
    )
    plan = plan_from_coverages(state_root, (core, coverage))
    selected = [entry.path for entry in plan.entries]
    assert "store/relay_DEVICE1.db" in selected
    assert all("matrix.json" not in name for name in selected)


def test_runtime_derived_device_is_unsupported(tmp_path):
    coverage = declaration().coverage(
        _settings(tmp_path, device_id=None), adapter_id="bridge"
    )
    assert coverage.state_class == "unsupported"
    assert coverage.files == ()
    assert any("guessed" in note for note in coverage.notes)


def test_colon_in_sdk_name_refuses_generic_capture(tmp_path):
    coverage = declaration().coverage(_settings(tmp_path), adapter_id="bridge")
    assert coverage.state_class == "externally_managed"
    assert coverage.files == ()
    assert any("grammar" in note for note in coverage.notes)


def test_sidecar_is_export_only_when_name_is_representable(tmp_path):
    # Standard account IDs always contain a colon and fall into the
    # externally-managed branch; a synthetic colon-free configured name is
    # the only case where the durable database is generically selectable.
    coverage = declaration().coverage(
        _settings(tmp_path, user_id="relay"), adapter_id="bridge"
    )
    assert coverage.state_class == "file_backed"
    required = [item for item in coverage.files if item.presence == "required"]
    assert [item.path.name for item in required] == ["relay_DEVICE1.db"]
    sidecar = next(item for item in coverage.files if item.presence == "export_only")
    assert sidecar.path.name == "matrix.json"
    assert any(
        "no retained local ownership primitive" in note for note in coverage.notes
    )


def test_no_local_lock_primitive_is_admitted(tmp_path):
    exclusion = declaration().local_exclusion(_settings(tmp_path), adapter_id="bridge")
    assert exclusion.scope == "unsupported"
    assert "another host" in exclusion.note
    assert exclusion.method == "none"
