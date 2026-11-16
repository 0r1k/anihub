"""The stream proxy: one server for concurrent starts, closed with its connections, idle connections time out."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from urllib.parse import urlsplit

import pytest
from anicli_api.player.base import Video

from anihub.common import stream_proxy
from anihub.common.stream_proxy import StreamProxy

BODY = bytes(range(256)) * 64


def run(coro, timeout: float = 20):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def hosting_server(*, stall: asyncio.Event | None = None, pause: float = 0):
    """A hosting that sends BODY: half of it, then (after `pause`, or never while `stall` is unset) the rest."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(BODY) + BODY[: len(BODY) // 2])
                await writer.drain()
                if stall is not None:
                    await stall.wait()
                await asyncio.sleep(pause)
                writer.write(BODY[len(BODY) // 2 :])
                await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v.mp4"


async def player(proxy: StreamProxy, url: str):
    """A connection to the proxy and the request line of the video."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    return reader, writer, f"GET {urlsplit(url).path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()


async def read_response(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n")[0], head
    return await reader.readexactly(len(BODY))


async def closed(reader: asyncio.StreamReader, timeout: float = 3) -> bool:
    """The proxy closed the connection: the end of the data (after what was already sent) comes in time."""
    try:
        await asyncio.wait_for(reader.read(), timeout)
    except ConnectionError:
        pass
    except asyncio.TimeoutError:
        return False
    return True


def test_concurrent_wraps_start_one_server(monkeypatch):
    started = []
    start_server = asyncio.start_server

    async def counting(*args, **kwargs):
        started.append(args)
        await asyncio.sleep(0.05)  # the second wrap comes while the first one starts the server
        return await start_server(*args, **kwargs)

    monkeypatch.setattr(stream_proxy.asyncio, "start_server", counting)

    async def check() -> None:
        proxy = StreamProxy()
        try:
            video = [Video(type="mp4", quality=720, url="https://hosting.example/v.mp4")]
            first, second = await asyncio.gather(proxy.wrap(video), proxy.wrap(video))
            assert len(started) == 1
            assert first[0].url == second[0].url and f":{proxy.port}/" in first[0].url
        finally:
            await proxy.aclose()

    run(check())


def test_aclose_closes_the_connections_of_the_players():
    """A download in the middle and an idle keep-alive connection: aclose does not wait for the player."""

    async def check() -> None:
        stall = asyncio.Event()
        server, url = await hosting_server(stall=stall)
        proxy = StreamProxy()
        try:
            (local,) = await proxy.wrap([Video(type="mp4", quality=720, url=url)])
            downloading, writer, request = await player(proxy, local.url)
            writer.write(request)
            await downloading.readuntil(b"\r\n\r\n")
            idle, _, _ = await player(proxy, local.url)
            await asyncio.sleep(0.1)
            await asyncio.wait_for(proxy.aclose(), 3)
            assert await closed(downloading) and await closed(idle)
            assert proxy._connections == {}
        finally:
            stall.set()
            await proxy.aclose()
            server.close()

    run(check())


def test_an_idle_connection_times_out(monkeypatch):
    monkeypatch.setattr(stream_proxy, "KEEP_ALIVE_TIMEOUT", 1.0)

    async def check() -> None:
        server, url = await hosting_server()
        proxy = StreamProxy()
        try:
            (local,) = await proxy.wrap([Video(type="mp4", quality=720, url=url)])
            # never sends a request
            silent, _, _ = await player(proxy, local.url)
            assert await closed(silent)
            # a keep-alive connection: the next request in time is served, then the connection is closed
            reader, writer, request = await player(proxy, local.url)
            writer.write(request)
            assert await read_response(reader) == BODY
            await asyncio.sleep(0.1)
            writer.write(request)
            assert await read_response(reader) == BODY
            assert await closed(reader)
        finally:
            await proxy.aclose()
            server.close()

    run(check())


def test_a_slow_body_is_not_cut_by_the_timeout(monkeypatch):
    """The timeout is only for the wait of a request: a download longer than it goes on."""
    monkeypatch.setattr(stream_proxy, "KEEP_ALIVE_TIMEOUT", 0.2)

    async def check() -> None:
        server, url = await hosting_server(pause=0.8)
        proxy = StreamProxy()
        try:
            (local,) = await proxy.wrap([Video(type="mp4", quality=720, url=url)])
            reader, writer, request = await player(proxy, local.url)
            writer.write(request)
            assert await read_response(reader) == BODY
        finally:
            await proxy.aclose()
            server.close()

    run(check())


def test_a_paused_player_gets_the_rest_of_the_body(monkeypatch):
    """The player does not read for longer than the timeout in the middle of a body the buffers do not hold."""
    data = bytes(range(251)) * (8 * 1024 * 1024 // 251)
    monkeypatch.setattr(stream_proxy, "KEEP_ALIVE_TIMEOUT", 0.2)

    async def hosting(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        with contextlib.suppress(ConnectionError):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(data) + data)
            await writer.drain()
        writer.close()

    async def check() -> None:
        server = await asyncio.start_server(hosting, "127.0.0.1", 0)
        proxy = StreamProxy()
        try:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v.mp4"
            (local,) = await proxy.wrap([Video(type="mp4", quality=720, url=url)])
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
            sock.connect(("127.0.0.1", proxy.port))
            sock.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=sock)
            writer.write(f"GET {urlsplit(local.url).path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await reader.readuntil(b"\r\n\r\n")
            body = await reader.readexactly(1024 * 1024)
            await asyncio.sleep(0.6)  # paused
            body += await reader.readexactly(len(data) - len(body))
            writer.close()
            assert body == data
        finally:
            await proxy.aclose()
            server.close()

    run(check())


def test_a_broken_url_is_left_to_the_player():
    """`http://[x/` makes urlsplit raise: not wrapped (the player check reports it), not a crash of wrap()."""

    async def check() -> None:
        proxy = StreamProxy()
        broken = Video(type="mp4", quality=720, url="http://[x/1.mp4")
        good = Video(type="mp4", quality=720, url="https://hosting.example/v.mp4")
        try:
            wrapped = await proxy.wrap([broken, good])
            assert wrapped[0] is broken
            assert wrapped[1].url.startswith(f"http://127.0.0.1:{proxy.port}/")
            # a broken uri inside a HLS playlist stays as it is, the others point to the proxy
            text = proxy.rewrite_playlist("#EXTM3U\nhttp://[x/seg.ts\nseg-2.ts\n", "https://hosting.example/a.m3u8", {})
            lines = text.splitlines()
            assert lines[1] == "http://[x/seg.ts"
            assert lines[2].startswith(f"http://127.0.0.1:{proxy.port}/")
        finally:
            await proxy.aclose()

    run(check())


def test_a_failed_client_leaves_no_server(monkeypatch):
    """A bad proxy in the environment: every wrap() raises the real error, no half started server answers 404."""
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        msg = "Unknown scheme for proxy URL"
        raise ValueError(msg)

    monkeypatch.setattr(stream_proxy.httpx, "AsyncClient", broken)

    async def check() -> None:
        proxy = StreamProxy()
        video = [Video(type="mp4", quality=720, url="https://hosting.example/v.mp4")]
        for _ in range(2):
            with pytest.raises(ValueError, match="Unknown scheme"):
                await proxy.wrap(video)
        assert len(calls) == 2 and proxy._server is None and proxy._client is None
        await proxy.aclose()

    run(check())
