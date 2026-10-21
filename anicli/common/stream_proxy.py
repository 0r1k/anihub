"""Local HTTP proxy between the video player and video hostings.

Some hostings drop a part of new connections: kodik's CDN (solodcdn.com) leaves every third connection
unanswered. A player waits for its network timeout on each of them (mpv: 60 seconds), and ffmpeg opens
a new connection for every 6-second HLS segment, so the video starts slowly, freezes or does not start.

The proxy keeps working connections to the hosting alive and reuses them for the next segments, gives up
a connection attempt after a few seconds and tries again, and resumes a broken download from the last
received byte. Players get `http://127.0.0.1:<port>/<origin id>/<path of the hosting>` urls: relative urls
of HLS playlists keep working, absolute ones are rewritten to the proxy as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx
from anicli_api._http import HEADERS
from anicli_api.player.base import Video

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

logger = logging.getLogger(__name__)

# a lost connection attempt is given up quickly: the next one usually succeeds at once
CONNECT_TIMEOUT = 3
# no data from the hosting for this long: reconnect and continue from the received byte
READ_TIMEOUT = 15
ATTEMPTS = 8
# dash manifests may point to other hosts with absolute urls and are not rewritten: played directly
PROXIED_TYPES = ("m3u8", "mp4", "webm")
_RETRY_STATUSES = (502, 503, 504)
_PLAYLIST_MAX_SIZE = 4 * 1024 * 1024
_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')
# player request headers passed to the hosting and hosting response headers passed to the player
_FORWARDED_REQUEST = ("range", "if-range")
_FORWARDED_RESPONSE = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


class _PlayerGoneError(Exception):
    """The player closed the connection (seek, stop)."""


def _is_playlist(url: str, content_type: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith((".m3u8", ".m3u")) or "mpegurl" in content_type.lower()


def _range_start(value: str | None) -> int:
    """`bytes=100-` -> 100; anything else (no range, suffix range) -> 0."""
    match = re.fullmatch(r"\s*bytes=(\d+)-(\d*)\s*", value or "")
    return int(match[1]) if match else 0


def _range_end(value: str | None) -> str:
    match = re.fullmatch(r"\s*bytes=(\d+)-(\d*)\s*", value or "")
    return match[2] if match else ""


class StreamProxy:
    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.Server | None = None
        self._client: httpx.AsyncClient | None = None
        # origin id in the local url path -> (scheme://host of the hosting, headers it requires)
        self._origins: list[tuple[str, dict[str, str]]] = []
        self._origin_ids: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    async def start(self) -> None:
        if self._server is not None:
            return
        self._client = httpx.AsyncClient(
            # the user's proxy is for the sites: players never used it for the video itself
            headers={"User-Agent": HEADERS["User-Agent"], "Accept-Encoding": "identity"},
            follow_redirects=True,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            # hostings close idle connections themselves, keep them while the player cache is full
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60),
        )
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("stream proxy on 127.0.0.1:%s", self.port)

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def wrap(self, videos: Sequence[Video]) -> list[Video]:
        """Videos to give the player: the supported ones point to the proxy and need no headers."""
        await self.start()
        return [
            Video(type=v.type, quality=v.quality, url=self.local_url(v.url, v.headers))
            if v.type in PROXIED_TYPES and urlsplit(v.url).scheme in ("http", "https")
            else v
            for v in videos
        ]

    def local_url(self, url: str, headers: dict[str, str]) -> str:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        key = (origin, tuple(sorted(headers.items())))
        if key not in self._origin_ids:
            self._origin_ids[key] = len(self._origins)
            self._origins.append((origin, dict(headers)))
        local = f"http://127.0.0.1:{self.port}/{self._origin_ids[key]}{parts.path or '/'}"
        return f"{local}?{parts.query}" if parts.query else local

    def _upstream(self, target: str) -> tuple[str, dict[str, str]] | None:
        """`/<origin id>/path?query` -> hosting url and its headers."""
        if target.startswith(("http://", "https://")):
            # absolute form, sent by players configured to use an http proxy
            parts = urlsplit(target)
            target = f"{parts.path}?{parts.query}" if parts.query else parts.path
        origin_id, _, rest = target.lstrip("/").partition("/")
        if not origin_id.isdigit() or int(origin_id) >= len(self._origins):
            return None
        origin, headers = self._origins[int(origin_id)]
        return f"{origin}/{rest}", headers

    def rewrite_playlist(self, text: str, base_url: str, headers: dict[str, str]) -> str:
        """Point every uri of the HLS playlist (variants, segments, keys, init sections) to the proxy."""

        def local(uri: str) -> str:
            url = urljoin(base_url, uri.strip())
            return self.local_url(url, headers) if urlsplit(url).scheme in ("http", "https") else uri

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(local(stripped))
            elif stripped.startswith("#") and 'URI="' in stripped:
                lines.append(_URI_ATTRIBUTE.sub(lambda m: f'URI="{local(m[1])}"', line))
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    # --- server ------------------------------------------------------------

    async def _handle(self, reader: StreamReader, writer: StreamWriter) -> None:
        try:
            while request := await self._read_request(reader):
                method, target, headers, keep_alive = request
                if not await self._serve(method, target, headers, writer) or not keep_alive:
                    break
        except (_PlayerGoneError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            logger.exception("stream proxy failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _read_request(reader: StreamReader) -> tuple[str, str, dict[str, str], bool] | None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None  # the player closed an idle keep-alive connection
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = [*lines[0].split(" ", 2), "", ""][:3]
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        connection = headers.get("connection", "").lower()
        keep_alive = connection != "close" if version == "HTTP/1.1" else connection == "keep-alive"
        return method.upper(), target, headers, keep_alive

    @staticmethod
    async def _send(writer: StreamWriter, data: bytes) -> None:
        writer.write(data)
        try:
            await writer.drain()
        except (ConnectionError, OSError) as e:
            raise _PlayerGoneError from e

    async def _send_head(
        self, writer: StreamWriter, status: int, reason: str, headers: dict[str, str], *, keep_alive: bool
    ) -> None:
        lines = [f"HTTP/1.1 {status} {reason or 'OK'}"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        lines.append(f"Connection: {'keep-alive' if keep_alive else 'close'}")
        await self._send(writer, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))

    async def _send_error(self, writer: StreamWriter, status: int, reason: str) -> bool:
        await self._send_head(writer, status, reason, {"Content-Length": "0"}, keep_alive=True)
        return True

    async def _serve(self, method: str, target: str, headers: dict[str, str], writer: StreamWriter) -> bool:
        """Answer one request. False: the connection must be closed (the body is incomplete)."""
        upstream = self._upstream(target)
        if method not in ("GET", "HEAD") or upstream is None or self._client is None:
            return await self._send_error(writer, 404, "Not Found")
        url, extra = upstream
        request_headers = {**extra, **{k: headers[k] for k in _FORWARDED_REQUEST if k in headers}}
        wanted_range = headers.get("range")
        sent = 0  # body bytes given to the player
        started = False  # the status line is sent: a retry continues the body
        length: int | None = None

        for attempt in range(ATTEMPTS):
            if started and sent:
                request_headers["range"] = f"bytes={_range_start(wanted_range) + sent}-{_range_end(wanted_range)}"
            try:
                async with self._client.stream("GET", url, headers=request_headers) as response:
                    if not started:
                        if response.status_code in _RETRY_STATUSES and attempt < ATTEMPTS - 1:
                            logger.info("stream proxy: %s %s, retry", response.status_code, url)
                            await asyncio.sleep(1)
                            continue
                        content_type = response.headers.get("content-type", "")
                        if response.is_success and _is_playlist(str(response.url), content_type):
                            body = await response.aread()
                            if len(body) <= _PLAYLIST_MAX_SIZE and body.lstrip().startswith(b"#EXTM3U"):
                                text = self.rewrite_playlist(body.decode("utf-8", "replace"), str(response.url), extra)
                                return await self._send_playlist(writer, method, text, content_type)
                            return await self._send_body(writer, method, response, body)
                        reply = {
                            name.title(): response.headers[name]
                            for name in _FORWARDED_RESPONSE
                            if name in response.headers
                        }
                        if "content-length" in response.headers:
                            length = int(response.headers["content-length"])
                        await self._send_head(
                            writer, response.status_code, response.reason_phrase, reply, keep_alive=length is not None
                        )
                        started = True
                        if method == "HEAD":
                            return True
                        skip = 0
                    else:
                        # the hosting ignored the range: drop what the player already has
                        partial = response.status_code == httpx.codes.PARTIAL_CONTENT
                        skip = 0 if partial else _range_start(wanted_range) + sent
                    async for chunk in response.aiter_raw():
                        data = chunk[skip:]
                        skip = max(0, skip - len(chunk))
                        if data:
                            await self._send(writer, data)
                            sent += len(data)
                    if length is None or sent >= length:
                        return length is not None
            except httpx.TransportError as e:
                # lost connection attempt, a stalled or broken download: try again on another connection
                logger.info("stream proxy: %s on %s (attempt %s)", type(e).__name__, url, attempt + 1)
                continue
        if not started:
            return await self._send_error(writer, 504, "Gateway Timeout")
        return False

    async def _send_playlist(self, writer: StreamWriter, method: str, text: str, content_type: str) -> bool:
        body = text.encode()
        reply = {"Content-Type": content_type or "application/vnd.apple.mpegurl", "Content-Length": str(len(body))}
        await self._send_head(writer, 200, "OK", reply, keep_alive=True)
        if method == "GET":
            await self._send(writer, body)
        return True

    async def _send_body(self, writer: StreamWriter, method: str, response: httpx.Response, body: bytes) -> bool:
        reply = {name.title(): response.headers[name] for name in ("content-type",) if name in response.headers}
        reply["Content-Length"] = str(len(body))
        await self._send_head(writer, response.status_code, response.reason_phrase, reply, keep_alive=True)
        if method == "GET":
            await self._send(writer, body)
        return True
