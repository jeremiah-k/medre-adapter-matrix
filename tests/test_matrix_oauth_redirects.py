"""Discovery redirects cannot forward credentials or downgrade HTTPS."""

import json

import pytest

from medre.adapters.matrix.oauth import OAuthHTTP, MatrixOAuthError

pytestmark = pytest.mark.matrix_sdk


class Reply:
    def __init__(self, status=200, location=None):
        self.status = status
        self.headers = {"Location": location} if location is not None else {}
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        yield json.dumps({"discovered": True}).encode()

    def release(self):
        pass


class HTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.replies.pop(0)


def install_http(monkeypatch, replies):
    import aiohttp

    http = HTTP(replies)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: http)
    return http


async def test_discovery_follows_relative_and_https_cross_origin_redirects(monkeypatch):
    replies = [
        Reply(302, "/discovery"),
        Reply(307, "https://cdn.example/client?tenant=public"),
        Reply(),
    ]
    http = install_http(monkeypatch, replies)
    result = await OAuthHTTP().request(
        "GET", "https://example.com/.well-known/matrix/client", follow_redirects=True
    )
    assert result.body == {"discovered": True}
    assert [url for _, url, _ in http.calls] == [
        "https://example.com/.well-known/matrix/client",
        "https://example.com/discovery",
        "https://cdn.example/client?tenant=public",
    ]
    assert all(reply.closed for reply in replies)
    for method, _, options in http.calls:
        assert method == "GET"
        assert not options.get("data") and not options.get("json")
        assert not options.get("headers") or "Authorization" not in options["headers"]
        assert options["allow_redirects"] is False


@pytest.mark.parametrize(
    "target",
    [
        "http://insecure.example/client",
        "https://user:secret@host.example/client",
        "https://host.example/client#fragment",
        "https://[invalid",
        "\nhttps://host.example/client",
        "/\\untrusted.example/client",
    ],
)
async def test_unsafe_discovery_redirect_never_sends_a_second_request(
    monkeypatch, target
):
    http = install_http(monkeypatch, [Reply(302, target)])
    with pytest.raises(MatrixOAuthError):
        await OAuthHTTP().request(
            "GET",
            "https://example.com/.well-known/matrix/client",
            follow_redirects=True,
        )
    assert len(http.calls) == 1


async def test_discovery_redirect_loop_has_a_fixed_limit(monkeypatch):
    replies = [Reply(302, "/.well-known/matrix/client") for _ in range(6)]
    http = install_http(monkeypatch, replies)
    with pytest.raises(MatrixOAuthError, match="redirect limit"):
        await OAuthHTTP().request(
            "GET",
            "https://example.com/.well-known/matrix/client",
            follow_redirects=True,
        )
    assert len(http.calls) == 6
    assert all(reply.closed for reply in replies)


@pytest.mark.parametrize(
    "method,url,options",
    [
        ("POST", "https://example.com/.well-known/matrix/client", {}),
        ("GET", "https://example.com/token", {}),
        (
            "GET",
            "https://example.com/.well-known/matrix/client",
            {"token": "PRIVATE_TOKEN"},
        ),
        ("GET", "https://example.com/.well-known/matrix/client", {"form": {}}),
        ("GET", "https://example.com/.well-known/matrix/client", {"document": {}}),
    ],
)
async def test_redirect_opt_in_refuses_credential_bearing_requests(
    monkeypatch, method, url, options
):
    http = install_http(monkeypatch, [])
    with pytest.raises(MatrixOAuthError, match="credential-free"):
        await OAuthHTTP().request(method, url, follow_redirects=True, **options)
    assert http.calls == []
