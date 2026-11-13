"""Package licensing metadata is independent of optional transport SDKs."""

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_license_metadata_matches_distributed_text():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert project["license"] == "MIT"
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Jeremiah K." in license_text
    assert "The above copyright notice and this permission notice" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text


def test_dependency_license_disclosure_tracks_runtime_pins():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    disclosure = (ROOT / "DEPENDENCY_LICENSES.md").read_text(encoding="utf-8")
    for requirement in project["dependencies"]:
        if requirement.startswith("medre>="):
            continue
        assert requirement.strip("'\"") in disclosure, requirement


def test_renovate_disclosure_regex_matches_pinned_sdk_with_numeric_extras():
    """Renovate must capture SDK names without extras while updating their pins."""
    import json
    import re

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    config = json.loads((ROOT / "renovate.json").read_text())
    managers = [
        manager
        for manager in config["regexManagers"]
        if any("DEPENDENCY_LICENSES" in name for name in manager["fileMatch"])
    ]
    assert len(managers) == 1
    assert managers[0]["datasourceTemplate"] == "pypi"
    matchers = [
        re.compile(pattern.replace("(?<", "(?P<"))
        for pattern in managers[0]["matchStrings"]
    ]
    disclosure = (ROOT / "DEPENDENCY_LICENSES.md").read_text()
    matches = [match for regex in matchers for match in regex.finditer(disclosure)]
    captured = {
        (canonicalize_name(m.group("depName")), m.group("currentValue"))
        for m in matches
    }
    for raw in project["dependencies"]:
        dep = Requirement(raw)
        if canonicalize_name(dep.name) == "medre":
            continue
        assert len(dep.specifier) == 1
        pin = next(iter(dep.specifier))
        assert pin.operator == "=="
        assert (canonicalize_name(dep.name), pin.version) in captured
    assert ("mindroom-nio", "1.3.0") in captured
