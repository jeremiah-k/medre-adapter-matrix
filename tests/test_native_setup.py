"""SDK-free native form metadata remains provider-owned and deterministic."""

import pytest

from medre import adapter_spec as registration
from medre.adapters.matrix.plugin import adapter_spec


def test_native_setup_fields_are_sdk_free_and_named_for_the_config_model():
    spec = adapter_spec()
    fields = spec.native_setup_fields
    names = {field.name for field in fields}
    config_model = spec.config.load()
    from dataclasses import fields as model_fields

    assert names <= {field.name for field in model_fields(config_model)}
    assert len(names) == len(fields)
    token = next(field for field in fields if field.name == "access_token")
    assert token.secret is True
    assert token.required is False
    assert {"homeserver", "user_id"}.issubset(names)


def test_provider_requires_declared_native_form_contract(monkeypatch):
    monkeypatch.delattr(registration, "NativeSetupField", raising=False)
    with pytest.raises(AttributeError, match="NativeSetupField"):
        adapter_spec()


def _run_native_form(monkeypatch, tmp_path, answers, secret=""):
    import io
    from argparse import Namespace

    from medre.cli import native_commands
    from medre.config._yaml import parse_yaml_config

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output = tmp_path / "connection.yaml"
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", Terminal())
    replies = iter(answers)
    monkeypatch.setattr(native_commands, "_ask", lambda *args: next(replies))
    monkeypatch.setattr(native_commands.getpass, "getpass", lambda *args: secret)
    monkeypatch.setattr("medre.cli.bridge_commands._confirm_save", lambda *args: True)
    native_commands._config_native(
        Namespace(transport="matrix", adapter_id="bot", config=None, output=str(output))
    )
    assert not (tmp_path / "state").exists()
    return parse_yaml_config(output.read_text())["adapters"]["matrix"]["bot"]


def test_native_form_validates_a_complete_matrix_connection(monkeypatch, tmp_path):
    values = _run_native_form(
        monkeypatch,
        tmp_path,
        ["https://matrix.example.org", "@bot:example.org"],
        secret="test-only-token",
    )
    assert values["homeserver"] == "https://matrix.example.org"
    assert values["user_id"] == "@bot:example.org"
    assert values["access_token"] == "test-only-token"
