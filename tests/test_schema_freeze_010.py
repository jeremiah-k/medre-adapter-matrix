# medre-test-tier: compatibility
"""Freeze the exact published 0.1.0 schema artifacts before the first tag."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"
FROZEN = json.loads((SCHEMA_DIR / "v0.1.0.freeze.json").read_text(encoding="utf-8"))


def test_first_release_schema_file_set_is_frozen() -> None:
    """Adding/removing an exported schema needs deliberate release review."""
    actual = {
        p.name
        for p in SCHEMA_DIR.iterdir()
        if p.name.endswith((".schema.json", ".schema.yaml", ".schema.sql"))
    }
    assert actual == set(FROZEN["schema_files_sha256"])


@pytest.mark.parametrize("name,sha256", sorted(FROZEN["schema_files_sha256"].items()))
def test_first_release_schema_content_is_frozen(name: str, sha256: str) -> None:
    """Changing a public schema requires an explicit supported-version decision."""
    assert hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest() == sha256


@pytest.mark.parametrize(
    "name",
    sorted(
        name for name in FROZEN["schema_files_sha256"] if name.endswith(".schema.json")
    ),
)
def test_frozen_matrix_schema_is_valid(name: str) -> None:
    """Verify syntax and references of the schema we actually ship."""
    from jsonschema import Draft202012Validator

    schema = json.loads((SCHEMA_DIR / name).read_text())
    Draft202012Validator.check_schema(schema)
    if "$id" in schema:
        assert schema["$id"] == (
            "https://raw.githubusercontent.com/jeremiah-k/"
            f"medre-adapter-matrix/main/docs/schemas/{name}"
        )


def test_relay_provenance_schema_rejects_unauthorized_fields() -> None:
    """The native relay hint is strict even though Matrix wire content is not."""
    from jsonschema import Draft202012Validator

    schema = json.loads((SCHEMA_DIR / "matrix-native-metadata.schema.json").read_text())
    example = json.loads(
        (SCHEMA_DIR / "examples" / "matrix-native-metadata-example.json").read_text()
    )
    validator = Draft202012Validator(schema)
    validator.validate(example)
    from copy import deepcopy

    unknown = deepcopy(example)
    unknown["matrix"]["relay"] = {
        "medre_envelope": {
            "schema_version": 1,
            "canonical_event_id": "evt",
            "metadata_mode": "safe",
            "access_token": "must-not-be-projected",
        }
    }
    assert not validator.is_valid(unknown)
    minimal = deepcopy(example)
    minimal["matrix"]["relay"] = {
        "medre_envelope": {
            "schema_version": 1,
            "canonical_event_id": "evt",
            "metadata_mode": "minimal",
            "source_transport_id": "!a1b2",
        }
    }
    assert validator.is_valid(minimal)


def test_frozen_schema_bytes_are_checkout_invariant() -> None:
    """Git must not rewrite frozen schema bytes on Windows (autocrlf)."""
    attributes = (Path(__file__).resolve().parents[1] / ".gitattributes").read_text(
        encoding="utf-8"
    )
    assert "docs/schemas/** -text" in attributes.splitlines()


def test_first_release_matrix_native_contract_excludes_legacy_mmrelay() -> None:
    """Do not permanently standardize one relay's prerelease flat wire keys."""
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (SCHEMA_DIR / "matrix-native-metadata.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (SCHEMA_DIR / "examples" / "matrix-native-metadata-example.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)
    validator.validate(example)
    contaminated = dict(example)
    contaminated["interop"] = {"mmrelay": {"meshtastic_text": "override"}}
    assert not validator.is_valid(contaminated)
    assert set(schema["properties"]) == {"matrix"}
    assert not any("meshtastic_" in key for key in schema["$defs"])
