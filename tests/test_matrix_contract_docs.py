"""Prevent current Matrix SDK requirements drifting from the spec."""

from pathlib import Path
import re
import tomllib

import pytest


def _declared_sdk_requirement() -> str:
    """The spec names the SDK pyproject pins, without coupling to its version."""
    root = Path(__file__).resolve().parent.parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        dependency.partition("==")[0]
        for dependency in metadata["project"]["dependencies"]
        if dependency.startswith("mindroom-nio") and "==" in dependency
    ]
    assert len(requirements) == 1
    return requirements[0]


def test_durable_sync_spec_names_the_pinned_sdk() -> None:
    contract = (
        Path(__file__).resolve().parent.parent / "docs/spec/durable-sync.md"
    ).read_text(encoding="utf-8")
    assert _mentions_sdk_name(contract, _declared_sdk_requirement())


def _mentions_sdk_name(contract: str, requirement: str) -> bool:
    """Match a complete SDK requirement name while rejecting longer identifiers."""
    return (
        re.search(rf"{re.escape(requirement)}(?![0-9A-Za-z_-])", contract) is not None
    )


@pytest.mark.parametrize(
    ("documented", "expected"),
    [
        ("mindroom-nio[e2e]", True),
        ("mindroom-nio[e2e].", True),
        ("mindroom-nio[e2e]:", True),
        ("mindroom-nio[e2e]-cli", False),
        ("mindroom-nio[e2e]2", False),
        ("mindroom-nio", False),
    ],
)
def test_sdk_name_matching_rejects_longer_identifiers(documented, expected):
    """A longer identifier cannot satisfy the pinned SDK name mention."""
    assert (
        _mentions_sdk_name(f"the pinned {documented} SDK", "mindroom-nio[e2e]")
        is expected
    )
