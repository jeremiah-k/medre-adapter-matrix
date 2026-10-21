"""Deterministic Matrix OAuth sessions and clocks for adapter tests."""

from medre.adapters.matrix.oauth import API_SCOPE, DEVICE_SCOPE, OAuthSession


def make_session(**overrides):
    values = dict(
        homeserver="https://matrix.example",
        user_id="@relay:example",
        device_id="MEDRE_device",
        issuer="https://auth.example",
        token_endpoint="https://auth.example/token",
        revocation_endpoint="https://auth.example/revoke",
        client_id="public-client",
        scope=API_SCOPE + " " + DEVICE_SCOPE + "MEDRE_device",
        expires_at=2000.0,
        refresh_at=1800.0,
        access_token="ACCESS_SECRET",
        refresh_token="REFRESH_SECRET",
    )
    values.update(overrides)
    return OAuthSession(**values)


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        assert seconds >= 0
        self.value += seconds
