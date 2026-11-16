from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import platform
import secrets
import shutil
import tempfile
from asyncio.streams import StreamReader, StreamWriter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anihub.common import history
from anihub.common.m3u import one_line

from .base import BasePlayer, PlayerFailedError, split_args

logger = logging.getLogger(__name__)

TITLE_KEY = "--force-media-title"
# one header per option: the plain option is a comma separated list and a comma in a value would split it
HEADERS_KEY = "--http-header-fields-append"
USER_AGENT_KEY = "--user-agent"
REFERER_KEY = "--referrer"
IS_WINDOWS = platform.system() == "Windows"
# a video is watched when played up to this percent (the rest is usually the ending)
WATCHED_PERCENT = 80
# mpv exit code when none of the files could be played
EXIT_ALL_FAILED = 2


def playback_result(end_reason: str | None) -> bool | None:
    """`BasePlayer.play()` result by the `end-file` reason of the last video.

    eof: played to the end; stop: the user skipped it (`playlist-next`), the episodes go on; quit: the user closed mpv;
    error: the video failed (PlayerFailedError). Watching is counted by the position, not by the reason: a stream
    cut by the network ends with eof too.
    """
    if end_reason in ("eof", "stop"):
        return True
    if end_reason == "quit":
        return False
    return None


def _parse(line: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    # a broken line (invalid UTF-8 in a title, a too deep nesting) must not hide the following events
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


class MpvIpcClient:
    """Minimal JSON IPC client for mpv (unix socket or windows named pipe).

    A background task reads the connection from `connect()` on: every event is taken when mpv sends it, a reply
    goes to the request waiting for it. mpv closes the socket shortly before it exits, and a request written then
    fails: the transport drops the connection with the lines not read yet, the last `end-file` among them.
    """

    def __init__(self) -> None:
        self.tmp_dir: Path | None = None
        self.socket_path: str | None = None
        self.reader: StreamReader | None = None
        self.writer: StreamWriter | None = None
        # an event loop without async pipes (selector loop on Windows): a pipe per request, events are lost
        self.per_request = False
        # reason of the last `end-file` event: eof, stop, quit, error, redirect, unknown
        self.last_end_reason: str | None = None
        self._request_ids = itertools.count(1)
        self._reading: asyncio.Task[None] | None = None
        # requests waiting for their reply, by request_id
        self._replies: dict[int, asyncio.Future[dict[str, Any] | None]] = {}

    async def __aenter__(self) -> MpvIpcClient:
        if IS_WINDOWS:
            # unique per player: a previous mpv of this process can still be closing
            self.socket_path = rf"\\.\pipe\mpvserver_{os.getpid()}_{secrets.token_hex(4)}"
        else:
            self.tmp_dir = Path(tempfile.mkdtemp(prefix="anihub_mpv_"))
            self.socket_path = str(self.tmp_dir / "socket")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self.close()
        finally:
            if self.tmp_dir and self.tmp_dir.exists():
                shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def close(self) -> None:
        """Stop reading and close the connection."""
        task, self._reading = self._reading, None
        writer, self.writer, self.reader = self.writer, None, None
        try:
            if task:
                task.cancel()
                # not `await task`: the CancelledError of the reader cannot be told from a cancellation of the caller
                await asyncio.wait([task])
        finally:
            # the connection is closed even if the caller is cancelled while the reader stops
            if writer:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def connect(self, attempts: int = 50, delay: float = 0.1, running: Callable[[], bool] = lambda: True) -> bool:
        """Wait until mpv creates the socket and connect to it; stop waiting when mpv has exited."""
        if not self.socket_path:
            return False
        for _ in range(attempts):
            with contextlib.suppress(OSError):
                if await self._open(self.socket_path):
                    return True
            if not running():
                return False
            await asyncio.sleep(delay)
        return False

    async def _open(self, path: str) -> bool:
        if not IS_WINDOWS:
            if not Path(path).exists():
                return False
            self._attach(*await asyncio.open_unix_connection(path))
            return True
        loop = asyncio.get_running_loop()
        create_pipe_connection = getattr(loop, "create_pipe_connection", None)
        if create_pipe_connection is None:
            self.per_request = True
            return True
        # the proactor loop (the Windows default) keeps the pipe open: events come like on a unix socket
        reader = StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await create_pipe_connection(lambda: protocol, path)
        self._attach(reader, StreamWriter(transport, protocol, reader, loop))
        return True

    def _attach(self, reader: StreamReader, writer: StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._reading = asyncio.create_task(self._read(reader))

    async def _read(self, reader: StreamReader) -> None:
        """Take the lines until mpv closes the connection."""
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # a broken connection raises its error at once, on every call
                    if reader.exception() is not None:
                        return
                    # a line over the limit of the reader is dropped by readline(), the next ones are read
                    continue
                if not line:
                    return
                self._dispatch(line)
        except OSError as e:  # the connection is lost: a failed write drops it, a pipe breaks
            logger.debug("mpv ipc connection lost: %s", e)
        except Exception:
            logger.exception("mpv ipc reading failed")
        finally:
            # no reply comes after this
            for reply in self._replies.values():
                if not reply.done():
                    reply.set_result(None)

    def _dispatch(self, line: bytes) -> None:
        data = _parse(line)
        if data is None:
            return
        if "event" in data:
            self._handle_event(data)
            return
        # a request cancelled or timed out has left its reply behind: nobody waits for it
        request_id = data.get("request_id")
        reply = self._replies.get(request_id) if isinstance(request_id, int) else None
        if reply is not None and not reply.done():
            reply.set_result(data)

    def _handle_event(self, data: dict[str, Any]) -> None:
        if data.get("event") == "end-file":
            self.last_end_reason = data.get("reason")

    def _request(self, command: dict[str, Any]) -> tuple[int, bytes]:
        request_id = next(self._request_ids)
        return request_id, json.dumps({**command, "request_id": request_id}).encode() + b"\n"

    async def send(self, command: dict[str, Any], timeout: float = 5) -> dict[str, Any] | None:
        """The reply to the command; None if mpv has closed the connection or not answered in `timeout` seconds."""
        request_id, request = self._request(command)
        if self.per_request:
            return await asyncio.get_running_loop().run_in_executor(None, self._send_per_request, request_id, request)
        # mpv has closed the connection: a write would fail and no reply would come
        if self.writer is None or self._reading is None or self._reading.done():
            return None
        reply: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._replies[request_id] = reply
        try:
            # the write fails when mpv has just closed the socket: the reader then ends and answers None
            with contextlib.suppress(OSError):
                self.writer.write(request)
                await self.writer.drain()
            return await asyncio.wait_for(reply, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._replies.pop(request_id, None)

    async def drain_events(self, timeout: float = 1) -> None:
        """Wait until the events mpv sent before its exit are read: it closes the socket after them."""
        if self._reading:
            await asyncio.wait([self._reading], timeout=timeout)

    def _send_per_request(self, request_id: int, request: bytes) -> dict[str, Any] | None:
        if not self.socket_path:
            return None
        try:
            with open(self.socket_path, "w+b", buffering=0) as f:
                f.write(request)
                while line := f.readline():
                    data = _parse(line)
                    if data is not None and "event" not in data and data.get("request_id") == request_id:
                        return data
        except OSError:
            return None
        return None


class MPVPlayer(BasePlayer):
    """mpv with IPC: tracks watched episode/time into history and detects closing by the user."""

    command = "mpv"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ipc: MpvIpcClient | None = None

    def build_args(self, target: str) -> list[str]:
        args = self._build_header_args(self.videos[0].headers)
        args.extend(split_args(self.args))
        if self.start:
            args.append(f"--start={self.start}")
        if self.ipc:
            args.append(f"--input-ipc-server={self.ipc.socket_path}")
        if len(self.videos) == 1:
            # an argument, not a shell command: the title is shown as it is
            args.append(f"{TITLE_KEY}={one_line(self.titles[0])}")
        return [*args, target]

    async def play(self) -> bool | None:
        async with MpvIpcClient() as ipc:
            self.ipc = ipc
            return await super().play()

    def _running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def run(self, args: list[str]) -> bool | None:
        await self._spawn(args)
        monitor_task: asyncio.Task | None = None
        connected = False
        try:
            if self.ipc and await self.ipc.connect(running=self._running):
                connected = True
                if self.save_time or self.on_watched:
                    monitor_task = asyncio.create_task(self._monitor_time())
            await self._wait()
        finally:
            if monitor_task:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

        end_reason = None
        if connected and self.ipc:
            await self.ipc.drain_events()
            end_reason = self.ipc.last_end_reason
        # a dead link can close mpv before the IPC connects: its exit code tells it
        exit_code = self.process.returncode if self.process else None
        if end_reason == "error" or (end_reason is None and exit_code == EXIT_ALL_FAILED):
            raise PlayerFailedError(self.titles[-1])
        return playback_result(end_reason)

    async def _monitor_time(self, interval: float = 5) -> None:
        last_title = last_time = None
        while True:
            try:
                # the playlist position, not media-title: the title of the history must stay the same
                index = await self.get_property("playlist-pos")
                title = self.titles[index] if isinstance(index, int) and 0 <= index < len(self.titles) else None
                if self.save_time:
                    if title and title != last_title:
                        last_title = title
                        history.update_last({"episode": title})

                    pos = await self.get_property("time-pos")
                    if pos is not None and int(pos) != last_time:
                        last_time = int(pos)
                        history.update_last({"time": last_time})

                if self.on_watched and title:
                    await self._check_watched(title)
            except (OSError, ValueError, TypeError) as e:
                logger.debug("mpv ipc error: %s", e)
            await asyncio.sleep(interval)

    async def _check_watched(self, title: str) -> None:
        percent = await self.get_property("percent-pos")
        if percent is not None and float(percent) >= WATCHED_PERCENT:
            self.report_watched(title)

    async def get_property(self, name: str) -> Any:
        if self.ipc and (response := await self.ipc.send({"command": ["get_property", name]})):
            return response.get("data")
        return None

    @staticmethod
    def _build_header_args(headers: dict[str, str]) -> list[str]:
        """Map video headers to mpv arguments; a line break from the site would add a header of its own."""
        args = []
        lower = {k.lower(): one_line(v) for k, v in headers.items()}
        if user_agent := lower.get("user-agent"):
            args.append(f"{USER_AGENT_KEY}={user_agent}")
        if referer := lower.get("referer"):
            args.append(f"{REFERER_KEY}={referer}")

        args.extend(
            f"{HEADERS_KEY}={one_line(k)}: {one_line(v)}"
            for k, v in headers.items()
            if k.lower() not in ("user-agent", "referer")
        )
        return args
