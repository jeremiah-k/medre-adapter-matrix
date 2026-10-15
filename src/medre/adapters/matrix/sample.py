"""Offline example values owned by this adapter distribution."""

from medre.adapter_spec import AdapterSample


def sample_config() -> AdapterSample:
    """Supply fake configuration and its opaque conversation address."""
    return AdapterSample(
        config={
            "homeserver": "https://matrix.example.com",
            "user_id": "@bot:example.com",
            "access_token": "fake_sample_token",
            "room_allowlist": ["!room:example.com"],
            "encryption_mode": "plaintext",
        },
        channel="!room:example.com",
    )
