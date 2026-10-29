"""Prevent current Matrix SDK version requirements drifting from the spec."""

from pathlib import Path
import re
import tomllib

import pytest


def test_durable_sync_spec_uses_active_pinned_sdk_revision() -> None:
    root = Path(__file__).resolve().parent.parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [
        dependency.removeprefix("mindroom-nio[e2e]==")
        for dependency in metadata["project"]["dependencies"]
        if dependency.startswith("mindroom-nio[e2e]==")
    ]
    assert len(pins) == 1
    contract = (root / "docs/spec/durable-sync.md").read_text(encoding="utf-8")
    assert _mentions_sdk_pin(contract, pins[0])


def _mentions_sdk_pin(contract: str, version: str) -> bool:
    """Match a complete SDK version while permitting ordinary sentence punctuation."""
    return (
        re.search(
            rf"mindroom-nio\[e2e\]\s+{re.escape(version)}(?![0-9A-Za-z_+!-]|\.[0-9A-Za-z])",
            contract,
        )
        is not None
    )


@pytest.mark.parametrize(
    ("documented", "expected"),
    [
        ("1.1.2", True),
        ("1.1.2.", True),
        ("1.1.20", False),
        ("1.1.2.1", False),
        ("1.1.2rc1", False),
        ("1.1.2.post1", False),
        ("1.1.2+local", False),
    ],
)
def test_sdk_pin_matching_rejects_version_prefixes(documented, expected):
    """A longer release or suffixed version cannot satisfy another SDK pin."""
    assert _mentions_sdk_pin(f"mindroom-nio[e2e] {documented}", "1.1.2") is expected
