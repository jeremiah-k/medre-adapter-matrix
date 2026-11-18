"""HTTP-framed response-cut fault injection for the Synapse D2 harness."""

from __future__ import annotations

from typing import Any

_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "expect",
        "content-length",
    }
)


def _forward_headers(headers: Any) -> list[tuple[str, str]]:
    connection_tokens = {
        token.strip().lower()
        for value in headers.getall("Connection", [])
        for token in value.split(",")
    }
    return [
        (key, value)
        for key, value in headers.items()
        if key.lower() not in _HOP_HEADERS | connection_tokens
    ]


class SendResponseCutProxy:
    """Forward HTTP messages and optionally cut successful room-send responses.

    aiohttp owns framing, keep-alive retirement and chunk decoding. The
    upstream response is read before aborting the client transport: the
    actual server must have answered, not merely received a partial request.
    Recipient evidence is still required to qualify acceptance.
    """

    def __init__(self, target_host: str, target_port: int) -> None:
        self.set_upstream(target_host, target_port)
        self._runner: Any = None
        self._client: Any = None
        self.armed = True
        self.cuts = 0
        self.room_send_requests = 0
        self.port: int | None = None

    def set_upstream(self, host: str, port: int) -> None:
        """Retarget a restarted container while keeping the client origin."""
        self._target = f"http://{host}:{port}"

    async def start(self) -> int:
        import aiohttp
        from aiohttp import web
        from yarl import URL

        client = aiohttp.ClientSession(
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=60)
        )
        self._client = client

        async def forward(request: Any) -> Any:
            body = await request.read()
            url = URL(self._target + request.raw_path, encoded=True)
            room_send = (
                request.method in {"PUT", "POST"} and "/send/m.room." in request.path
            )
            if room_send:
                self.room_send_requests += 1
            try:
                async with client.request(
                    request.method,
                    url,
                    data=body,
                    headers=_forward_headers(request.headers),
                    allow_redirects=False,
                ) as upstream:
                    response = await upstream.read()
                    if self.armed and room_send and 200 <= upstream.status < 300:
                        self.cuts += 1
                    else:
                        return web.Response(
                            status=upstream.status,
                            body=response,
                            headers=_forward_headers(upstream.headers),
                        )
            except aiohttp.ClientConnectionError:
                # Preserve a network failure while the upstream is offline.
                # A synthesized HTTP reply would exercise a different path.
                pass
            if request.transport is not None:
                request.transport.abort()
            return web.Response(status=502)

        app = web.Application(handler_args={"auto_decompress": False})
        app.router.add_route("*", "/{path:.*}", forward)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            port = self._runner.addresses[0][1]
            self.port = port
            return port
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        client, self._client = self._client, None
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if client is not None:
                await client.close()
