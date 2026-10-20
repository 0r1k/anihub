from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import tempfile
from asyncio.streams import StreamReader, StreamWriter
from pathlib import Path
from typing import Any

from anicli.common import history
from anicli.common.sanitizer import sanitize_filename

from .base import BasePlayer, split_args

logger = logging.getLogger(__name__)

TITLE_KEY = "--force-media-title"
HEADERS_KEY = "--http-header-fields"
USER_AGENT_KEY = "--user-agent"
REFERER_KEY = "--referrer"
IS_WINDOWS = platform.system() == "Windows"
# a video is watched when played up to this percent (the rest is usually the ending)
WATCHED_PERCENT = 80


class MpvIpcClient:
    """Minimal JSON IPC client for mpv (unix socket or windows named pipe)."""

    def __init__(self) -> None:
        self.tmp_dir: Path | None = None
        self.socket_path: str | None = None
        self.reader: StreamReader | None = None
        self.writer: StreamWriter | None = None
        # reason of the last `end-file` event: eof, stop, quit, error, redirect
        self.last_end_reason: str | None = None

    async def __aenter__(self) -> MpvIpcClient:
        if IS_WINDOWS:
            self.socket_path = rf"\\.\pipe\mpvserver_{os.getpid()}"
        else:
            self.tmp_dir = Path(tempfile.mkdtemp(prefix="anicli_mpv_"))
            self.socket_path = str(self.tmp_dir / "socket")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
            self.writer = self.reader = None
        if self.tmp_dir and self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def connect(self, attempts: int = 50, delay: float = 0.1) -> bool:
        """Wait until mpv creates the socket and connect to it. Windows pipes are opened per request."""
        if IS_WINDOWS or not self.socket_path:
            return IS_WINDOWS
        for _ in range(attempts):
            if Path(self.socket_path).exists():
                with contextlib.suppress(OSError):
                    self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
                    return True
            await asyncio.sleep(delay)
        return False

    def _handle_event(self, data: dict[str, Any]) -> None:
        if data.get("event") == "end-file":
            self.last_end_reason = data.get("reason")

    async def send(self, command: dict[str, Any]) -> dict[str, Any] | None:
        line: bytes | None = None
        if IS_WINDOWS:
            line = await asyncio.get_running_loop().run_in_executor(None, self._windows_send, command)
        elif self.reader and self.writer:
            self.writer.write(json.dumps(command).encode() + b"\n")
            await self.writer.drain()
            # events are interleaved with the command response
            while line := await self.reader.readline():
                data = json.loads(line.decode())
                if "event" not in data:
                    return data
                self._handle_event(data)
            return None
        return json.loads(line.decode()) if line else None

    async def drain_events(self, timeout: float = 1) -> None:
        """Read events buffered before mpv exit (the socket is closed after that)."""
        if not self.reader:
            return
        with contextlib.suppress(asyncio.TimeoutError, OSError, ValueError):
            while line := await asyncio.wait_for(self.reader.readline(), timeout):
                data = json.loads(line.decode())
                if "event" in data:
                    self._handle_event(data)

    def _windows_send(self, command: dict[str, Any]) -> bytes | None:
        try:
            if self.socket_path:
                with open(self.socket_path, "w+b", buffering=0) as f:
                    f.write(json.dumps(command).encode() + b"\n")
                    f.flush()
                    return f.readline()
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
            args.append(f"{TITLE_KEY}={sanitize_filename(self.titles[0])}")
        return [*args, target]

    async def play(self) -> bool | None:
        async with MpvIpcClient() as ipc:
            self.ipc = ipc
            return await super().play()

    async def run(self, args: list[str]) -> bool | None:
        await self._spawn(args)
        monitor_task: asyncio.Task | None = None
        connected = False
        try:
            if self.ipc and await self.ipc.connect():
                connected = True
                if self.save_time or self.on_watched:
                    monitor_task = asyncio.create_task(self._monitor_time())
            await self._wait()
        finally:
            if monitor_task:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

        if not connected or IS_WINDOWS or not self.ipc:
            return None
        await self.ipc.drain_events()
        if self.ipc.last_end_reason is None:
            return None
        completed = self.ipc.last_end_reason != "quit"
        if completed:
            # the last video reached EOF: the end could be skipped between polls
            self.report_watched(self.titles[-1])
        return completed

    async def _monitor_time(self, interval: float = 5) -> None:
        last_title = last_time = None
        while True:
            try:
                if self.save_time:
                    title = await self.get_property("media-title")
                    if title and title != last_title:
                        last_title = title
                        history.update_last({"episode": title})

                    pos = await self.get_property("time-pos")
                    if pos is not None and int(pos) != last_time:
                        last_time = int(pos)
                        history.update_last({"time": last_time})

                if self.on_watched:
                    await self._check_watched()
            except (OSError, ValueError, TypeError) as e:
                logger.debug("mpv ipc error: %s", e)
            await asyncio.sleep(interval)

    async def _check_watched(self) -> None:
        # media-title of a single video is sanitized, the playlist index is reliable
        index = await self.get_property("playlist-pos")
        percent = await self.get_property("percent-pos")
        if isinstance(index, int) and 0 <= index < len(self.titles) and percent is not None:
            if float(percent) >= WATCHED_PERCENT:
                self.report_watched(self.titles[index])

    async def get_property(self, name: str) -> Any:
        if self.ipc and (response := await self.ipc.send({"command": ["get_property", name]})):
            return response.get("data")
        return None

    @staticmethod
    def _build_header_args(headers: dict[str, str]) -> list[str]:
        """Map video headers to mpv arguments."""
        args = []
        lower = {k.lower(): v for k, v in headers.items()}
        if user_agent := lower.get("user-agent"):
            args.append(f"{USER_AGENT_KEY}={user_agent}")
        if referer := lower.get("referer"):
            args.append(f"{REFERER_KEY}={referer}")

        others = [f"{k}: {v}" for k, v in headers.items() if k.lower() not in ("user-agent", "referer")]
        if others:
            args.append(f"{HEADERS_KEY}={','.join(others)}")
        return args
