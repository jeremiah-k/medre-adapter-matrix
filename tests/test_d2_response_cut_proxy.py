"""Exercise the actual HTTP fault injector without a homeserver or SDK."""

from __future__ import annotations

import asyncio

import pytest
from medre_matrix_tests.helpers.d2_proxy import SendResponseCutProxy


async def test_split_body_then_room_send_on_same_connection():
    pytest.importorskip("aiohttp")
    from aiohttp import web

    received = []

    async def handle(request):
        received.append((request.path, await request.read()))
        return web.Response(body=b"ok")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    upstream = web.AppRunner(app, access_log=None)
    await upstream.setup()
    site = web.TCPSite(upstream, "127.0.0.1", 0)
    await site.start()
    proxy = SendResponseCutProxy("127.0.0.1", upstream.addresses[0][1])
    writer = None
    try:
        port = await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /keys/upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n"
        )
        await writer.drain()
        # A 100 response establishes that the headers were parsed before the
        # body was transmitted, rather than relying on TCP packet boundaries.
        assert b"100 Continue" in await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), 5
        )
        writer.write(b"data")
        await writer.drain()
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert b"200 OK" in headers
        assert await asyncio.wait_for(reader.readexactly(2), 5) == b"ok"
        writer.write(
            b"PUT /rooms/room/send/m.room.encrypted/txn HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        assert proxy.cuts == 1
        assert received == [
            ("/keys/upload", b"data"),
            ("/rooms/room/send/m.room.encrypted/txn", b"{}"),
        ]
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.close()
        await upstream.cleanup()
