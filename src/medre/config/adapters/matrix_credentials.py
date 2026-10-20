"""Matrix credential sidecar file helpers.

Provides functions for loading and resolving the Matrix credentials
JSON sidecar file (``$XDG_CONFIG_HOME/medre/credentials/matrix.json``).

These helpers are owned by the config layer because they deal with
credential resolution at config-time, not at runtime.  Concrete adapter
code may import them from here.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def get_credentials_path() -> Path:
    """Return the canonical path for the Matrix credentials JSON file.

    The path points to
    ``$XDG_CONFIG_HOME/medre/credentials/matrix.json``
    (defaulting to ``~/.config/medre/credentials/matrix.json``).

    This function does **not** create any directories.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(config_home) / "medre" / "credentials" / "matrix.json"


def load_credentials_json(path: Path | None = None) -> dict[str, Any] | None:
    """Load Matrix credentials JSON file.

    If *path* is provided, read from that path.  Otherwise use
    the default path from `get_credentials_path()`.

    Returns the credential dict, or ``None`` if the file does not exist or
    cannot be parsed.
    """
    target = path if path is not None else get_credentials_path()
    if not target.exists():
        return None
    try:
        with target.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None
        data: object = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_credentials_json(
    data: Mapping[str, object], path: Path | None = None
) -> Path:
    """Write Matrix credentials JSON file with restrictive permissions.

    If *path* is provided, write to that path.  Otherwise use
    the default path from `get_credentials_path()`.

    Creates parent directories if they don't exist.
    Returns the path that was written.
    """
    import tempfile

    target = path if path is not None else get_credentials_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(dict(data), indent=2, allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise OSError("Matrix credential record exceeds the size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".matrix-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if sys.platform != "win32":
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    return target


__all__ = [
    "get_credentials_path",
    "load_credentials_json",
    "write_credentials_json",
]
