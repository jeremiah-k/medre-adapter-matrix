"""Fixture teardown checks without a Docker daemon or SDK credentials."""

import subprocess

import pytest
from medre_matrix_tests.helpers import d2_synapse


@pytest.mark.parametrize("retirement_fails", [False, True])
def test_generation_timeout_retires_both_named_resources(
    tmp_path, monkeypatch, retirement_fails
):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(d2_synapse.tempfile, "mkdtemp", lambda **kwargs: str(parent))
    monkeypatch.setattr(d2_synapse.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(d2_synapse.os, "getgid", lambda: 1000, raising=False)
    removals = []
    names = []

    def docker(*args, timeout=120):
        if args[0] == "run":
            names.append(args[args.index("--name") + 1])
            assert args[args.index("--user") + 1] == "1000:1000"
            raise subprocess.TimeoutExpired(args, timeout)
        if args[0] == "ps":
            return "present"
        if args[0] == "rm":
            removals.append(args[-1])
            if retirement_fails and not args[-1].endswith("-generate"):
                raise subprocess.TimeoutExpired(args, timeout)
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(d2_synapse, "docker", docker)
    fixture = d2_synapse._boot_synapse(
        "test",
        bot_localpart="bot",
        bot_password="unused",
        peer_localpart="peer",
        peer_password="unused",
        rc_message=None,
    )
    with pytest.raises(
        ExceptionGroup if retirement_fails else subprocess.TimeoutExpired
    ):
        next(fixture)
    assert removals == [names[0].removesuffix("-generate"), names[0]]
    assert parent.exists() is retirement_fails


def test_runtime_timeout_retires_runtime_after_generator_exits(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(d2_synapse.tempfile, "mkdtemp", lambda **kwargs: str(parent))
    monkeypatch.setattr(d2_synapse.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(d2_synapse.os, "getgid", lambda: 1000, raising=False)
    names = []
    removals = []

    def docker(*args, timeout=120):
        if args[0] == "run":
            names.append(args[args.index("--name") + 1])
            assert args[args.index("--user") + 1] == "1000:1000"
            if args[-1] == "generate":
                (parent / "synapse" / "homeserver.yaml").write_text(
                    "server_name: test\n"
                )
                return ""
            raise subprocess.TimeoutExpired(args, timeout)
        if args[0] == "ps":
            return "present" if names[-1] + "$" in args[-1] else ""
        if args[0] == "rm":
            removals.append(args[-1])
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(d2_synapse, "docker", docker)
    fixture = d2_synapse._boot_synapse(
        "test",
        bot_localpart="bot",
        bot_password="unused",
        peer_localpart="peer",
        peer_password="unused",
        rc_message=None,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        next(fixture)
    assert names[0] == names[1] + "-generate"
    assert removals == [names[1]]
    assert not parent.exists()
