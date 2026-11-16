"""Players: argument splitting, the mpv end-file rule, IPC replies and the player process lifetime.

The players are small Python scripts: no mpv is needed and no window opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import signal
import socket
import sys
import textwrap
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
from anicli_api.player.base import Video

from anihub.common.m3u import UnsafeVideoUrlError
from anihub.common.players import base, mpv
from anihub.common.players.base import (
    BasePlayer,
    CustomPlayer,
    PlayerArgsError,
    PlayerFailedError,
    PlayerNotFoundError,
    resolve_command,
    split_args,
)
from anihub.common.players.mpv import MpvIpcClient, MPVPlayer, playback_result

TITLES = ["Title - 1 One", "Title - 2 Two"]
END_OF_FILE = b'{"event":"end-file","reason":"eof","playlist_entry_id":1}\n'
T = TypeVar("T")
# mpv on Windows serves a named pipe: the fake one serves a unix socket
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake mpv serves a unix socket")


def videos(count: int = 1, headers: dict[str, str] | None = None) -> list[Video]:
    return [
        Video(type="mp4", quality=720, url=f"https://a.example/{i}.mp4", headers=headers or {}) for i in range(count)
    ]


def fake_player(tmp_path: Path, body: str) -> str:
    """Player command running a Python script: the player arguments are passed to it."""
    script = tmp_path / "player.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    # the quoting of the settings on this OS
    return f'"{sys.executable}" "{script}"'


def _running_tasks() -> set[asyncio.Task[Any]]:
    """Tasks of the loop but the accept task of a closed server on Windows: the proactor cancels its future
    and the task ends a loop turn later, it is asyncio's, not one left running by anihub."""
    return {
        task
        for task in asyncio.all_tasks()
        if not getattr(task.get_coro(), "__qualname__", "").endswith("IocpProactor.accept.<locals>.accept_coro")
    }


def run_clean(coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run() that fails on a task left running and on an error the loop reports (an exception never
    retrieved): the run ending cancels the tasks left and would hide them."""
    reported: list[dict[str, Any]] = []

    async def main() -> T:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context))
        result = await coro
        assert _running_tasks() == {asyncio.current_task()}, "a task is left running"
        gc.collect()  # a future holding an exception reports it when collected
        return result

    result = asyncio.run(main())
    assert reported == []
    return result


async def until(condition: Callable[[], object], timeout: float = 30) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


# --- split_args --------------------------------------------------------------


@pytest.mark.parametrize("windows", [False, True])
def test_split_args_quotes(windows):
    assert split_args('--sub-font="Segoe UI" --fs', windows=windows) == ["--sub-font=Segoe UI", "--fs"]
    assert split_args('"--title=a b"  ""', windows=windows) == ["--title=a b", ""]
    assert split_args("", windows=windows) == []
    with pytest.raises(PlayerArgsError):
        split_args('--title="unclosed', windows=windows)


def test_split_args_windows_keeps_backslashes():
    assert split_args(r'--sub-file=C:\subs\a.ass "C:\Program Files\mpv\mpv.exe"', windows=True) == [
        r"--sub-file=C:\subs\a.ass",
        r"C:\Program Files\mpv\mpv.exe",
    ]


def test_split_args_posix_escapes():
    assert split_args(r"--title=a\ b '--x=c d'", windows=False) == ["--title=a b", "--x=c d"]


def test_bad_command_is_not_installed():
    assert base.is_installed('mpv --title="unclosed') is False


def test_resolve_command_tells_wrong_arguments_from_a_missing_player():
    with pytest.raises(PlayerArgsError):
        resolve_command('mpv --title="unclosed')
    with pytest.raises(PlayerNotFoundError):
        resolve_command("anihub-no-such-player --fs")
    with pytest.raises(PlayerNotFoundError):
        resolve_command("")
    assert resolve_command(f'"{sys.executable}" -V') == [sys.executable, "-V"]


def test_custom_command_with_an_unclosed_quote_is_wrong_arguments():
    """The settings are wrong ("Wrong player arguments"), not the player missing ("Player not found")."""
    player = CustomPlayer('mpv --title="unclosed', videos(), TITLES[:1])
    with pytest.raises(PlayerArgsError):
        asyncio.run(player.play())
    assert player.process is None
    with pytest.raises(PlayerNotFoundError):
        asyncio.run(CustomPlayer("anihub-no-such-player --fs", videos(), TITLES[:1]).play())


# --- the end-file rule ---------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "result"),
    [("eof", True), ("stop", True), ("quit", False), ("redirect", None), ("unknown", None), (None, None)],
)
def test_playback_result(reason, result):
    assert playback_result(reason) is result


class FakeIpc(MpvIpcClient):
    """IPC that "connects" and reports the given end-file reason (None: no event came)."""

    reason: str | None = None

    async def connect(self, *_args: Any, **_kwargs: Any) -> bool:
        self.last_end_reason = self.reason
        return True


def run_mpv(
    monkeypatch, tmp_path, reason: str | None, exit_code: int = 0, count: int = 2
) -> tuple[bool | None, MPVPlayer]:
    monkeypatch.setattr(FakeIpc, "reason", reason)
    monkeypatch.setattr(mpv, "MpvIpcClient", FakeIpc)
    watched: list[str] = []
    player = MPVPlayer(videos(count), TITLES[:count], on_watched=watched.append)
    player.command = fake_player(tmp_path, f"import sys; sys.exit({exit_code})")
    result = asyncio.run(player.play())
    assert watched == []  # watching is counted by the position only
    return result, player


@pytest.mark.parametrize(("reason", "result"), [("eof", True), ("stop", True), ("quit", False), (None, None)])
def test_mpv_end_reason(monkeypatch, tmp_path, reason, result):
    played, player = run_mpv(monkeypatch, tmp_path, reason)
    assert played is result
    assert player.watched == set()


def test_mpv_error_is_not_watched(monkeypatch, tmp_path):
    with pytest.raises(PlayerFailedError) as e:
        run_mpv(monkeypatch, tmp_path, "error", exit_code=2)
    assert e.value.title == TITLES[1]


def test_mpv_closed_before_ipc_by_a_dead_link(monkeypatch, tmp_path):
    """No end-file event: the exit code of mpv says that nothing could be played."""
    with pytest.raises(PlayerFailedError):
        run_mpv(monkeypatch, tmp_path, None, exit_code=mpv.EXIT_ALL_FAILED, count=1)


def test_mpv_monitor_uses_playlist_position(monkeypatch):
    """The history keeps the real title and an episode watched to 80% is reported."""
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(mpv.history, "update_last", updates.append)
    player = MPVPlayer(videos(2), TITLES, save_time=True, on_watched=lambda _title: None)
    properties = {"playlist-pos": 1, "time-pos": 1300.5, "percent-pos": 85.0, "media-title": "sanitized"}

    async def get_property(name: str) -> Any:
        return properties[name]

    monkeypatch.setattr(player, "get_property", get_property)

    async def monitor() -> None:
        task = asyncio.create_task(player._monitor_time(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(monitor())
    assert {"episode": TITLES[1]} in updates
    assert {"time": 1300} in updates
    assert player.watched == {TITLES[1]}


def test_mpv_args():
    player = MPVPlayer(
        videos(1, {"User-Agent": "UA\r\nX-Evil: 1", "Referer": "https://r.example/", "Cookie": "a=1, b=2"}),
        ["Title: 1\nnext"],
        args='--sub-font="Segoe UI"',
    )
    args = player.build_args("https://a.example/0.mp4")
    assert "--user-agent=UA X-Evil: 1" in args
    assert "--referrer=https://r.example/" in args
    # one header per option: the comma stays in the value
    assert "--http-header-fields-append=Cookie: a=1, b=2" in args
    assert "--force-media-title=Title: 1 next" in args
    assert "--sub-font=Segoe UI" in args
    assert args[-1] == "https://a.example/0.mp4"


@pytest.mark.parametrize("url", ["--script=evil.lua", "http://[x/1.mp4"])
def test_link_from_the_site_is_not_an_option(tmp_path, url):
    player = BasePlayer([Video(type="mp4", quality=720, url=url)], ["t"])
    player.command = fake_player(tmp_path, "raise SystemExit(0)")
    with pytest.raises(UnsafeVideoUrlError):
        asyncio.run(player.play())
    assert player.process is None


# --- IPC replies ---------------------------------------------------------------


@contextlib.asynccontextmanager
async def ipc_with_server(lines: list[dict[str, Any] | bytes]) -> AsyncIterator[MpvIpcClient]:
    """IPC connected to a local server answering every request with `lines` (`{id}`: the id of the request)."""
    handlers: list[asyncio.Task[Any] | None] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.current_task())
        while request := await reader.readline():
            request_id = json.loads(request)["request_id"]
            for line in lines:
                if isinstance(line, bytes):
                    writer.write(line)
                else:
                    reply = {k: request_id if v == "{id}" else v for k, v in line.items()}
                    writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        writer.close()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with MpvIpcClient() as ipc:
            ipc._attach(*await asyncio.open_connection("127.0.0.1", port))
            yield ipc
        # Python 3.10: closing the server does not wait for its connections
        await asyncio.gather(*handlers)


async def ipc_on_socket() -> tuple[MpvIpcClient, socket.socket]:
    """IPC connected to a socket the test uses as the mpv end."""
    ours, mpv_end = socket.socketpair()
    mpv_end.setblocking(False)
    ipc = MpvIpcClient()
    ipc._attach(*await asyncio.open_connection(sock=ours))
    return ipc, mpv_end


def reply_line(request_id: int, data: Any) -> bytes:
    return json.dumps({"data": data, "request_id": request_id, "error": "success"}).encode() + b"\n"


def test_ipc_reply_matches_the_request():
    async def scenario() -> list[Any]:
        lines: list[dict[str, Any] | bytes] = [
            {"event": "end-file", "reason": "stop", "playlist_entry_id": 1},
            {"data": "stale reply of a cancelled request", "request_id": 0, "error": "success"},
            b"\xff not json\n",
            {"data": 42, "request_id": "{id}", "error": "success"},
        ]
        async with ipc_with_server(lines) as ipc:
            first = await ipc.send({"command": ["get_property", "playlist-pos"]})
            second = await ipc.send({"command": ["get_property", "playlist-pos"]})
        return [first and first["data"], second and second["data"], ipc.last_end_reason]

    assert run_clean(scenario()) == [42, 42, "stop"]


def test_ipc_late_reply_is_not_taken_for_the_next_one():
    """A request mpv does not answer in time gets None; its late reply does not answer the next request."""

    async def scenario() -> tuple[Any, Any]:
        ipc, mpv_end = await ipc_on_socket()
        loop = asyncio.get_running_loop()
        with mpv_end:
            async with ipc:
                first = await ipc.send({"command": ["get_property", "time-pos"]}, timeout=0.1)
                second = asyncio.create_task(ipc.send({"command": ["get_property", "time-pos"]}))
                requests = b""
                while requests.count(b"\n") < 2:
                    requests += await loop.sock_recv(mpv_end, 4096)
                first_id, second_id = (json.loads(line)["request_id"] for line in requests.splitlines())
                await loop.sock_sendall(mpv_end, reply_line(first_id, "late") + reply_line(second_id, "right"))
                answer = await second
                return first, answer and answer["data"]

    assert run_clean(scenario()) == (None, "right")


def test_ipc_skips_broken_and_overlong_lines():
    """A line over the 64 KiB limit of the reader, invalid UTF-8 and a too deep nesting: the events after them count."""

    lines = [
        b'{"event":"end-file","reason":"redirect"}\n',
        b'{"event":"client-message","args":["' + b"x" * 100_000 + b'"]}\n',
        b"\xff not json\n",
        b"[" * 10_000 + b"\n",  # RecursionError, not ValueError
        END_OF_FILE,
    ]

    async def scenario() -> str | None:
        ipc, mpv_end = await ipc_on_socket()
        with mpv_end:
            async with ipc:
                await asyncio.get_running_loop().sock_sendall(mpv_end, b"".join(lines))
                mpv_end.close()
                await ipc.drain_events()
                return ipc.last_end_reason

    assert run_clean(scenario()) == "eof"


def test_ipc_poll_after_mpv_closed_the_socket_keeps_the_end_reason():
    """mpv sends end-file and closes the socket ~0.1 s before its exit is seen: a poll in between writes to the closed
    socket. The event must be read by then: a failed write drops the connection with the lines not read yet."""

    async def scenario() -> tuple[Any, str | None]:
        ipc, mpv_end = await ipc_on_socket()
        with mpv_end:
            async with ipc:
                await asyncio.get_running_loop().sock_sendall(mpv_end, END_OF_FILE)
                mpv_end.close()
                await asyncio.sleep(0.1)  # the next poll comes later
                answer = await ipc.send({"command": ["get_property", "playlist-pos"]})
                await ipc.drain_events()
                return answer, ipc.last_end_reason

    assert run_clean(scenario()) == (None, "eof")


def test_ipc_failed_write_keeps_the_end_reason():
    """The poll writes before the reader sees the closed socket: the write fails, the event read before stays."""

    async def scenario() -> tuple[Any, str | None]:
        ipc, mpv_end = await ipc_on_socket()
        with mpv_end:
            async with ipc:
                await asyncio.get_running_loop().sock_sendall(mpv_end, END_OF_FILE)
                await asyncio.sleep(0.1)  # the event comes long before the poll
                mpv_end.close()
                # no pause: the write gets EPIPE (a unix socket) while the reader still waits for the next line
                answer = await ipc.send({"command": ["get_property", "playlist-pos"]})
                await ipc.drain_events()
                return answer, ipc.last_end_reason

    assert run_clean(scenario()) == (None, "eof")


def test_ipc_cancelled_while_closing_still_cleans_up():
    """The playback is cancelled again while the IPC stops its reader: the connection and the socket directory go."""

    async def scenario() -> tuple[bool, bool]:
        ipc, mpv_end = await ipc_on_socket()
        with mpv_end:
            await ipc.__aenter__()
            writer = ipc.writer
            assert writer is not None
            closing = asyncio.create_task(ipc.__aexit__(None, None, None))
            await asyncio.sleep(0)  # it waits for the reader to stop
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            return writer.is_closing(), ipc.tmp_dir is None or not ipc.tmp_dir.exists()

    assert run_clean(scenario()) == (True, True)


def test_ipc_pipe_per_request_matches_the_reply(monkeypatch):
    """Windows without async pipes: a pipe per request, events and other replies before the reply are skipped."""
    written: list[bytes] = []

    class Pipe:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.lines: list[bytes] = []

        def __enter__(self) -> Pipe:
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def write(self, data: bytes) -> None:
            written.append(data)
            request_id = json.loads(data)["request_id"]
            self.lines = [
                b'{"event":"playback-restart"}\n',
                b'{"data":0,"request_id":0,"error":"success"}\n',
                json.dumps({"data": "ok", "request_id": request_id, "error": "success"}).encode() + b"\n",
            ]

        def flush(self) -> None:
            pass

        def readline(self) -> bytes:
            return self.lines.pop(0) if self.lines else b""

    monkeypatch.setattr(mpv, "open", Pipe, raising=False)
    ipc = MpvIpcClient()
    ipc.socket_path = r"\\.\pipe\test"
    ipc.per_request = True
    reply = asyncio.run(ipc.send({"command": ["get_property", "media-title"]}))
    assert reply is not None
    assert reply["data"] == "ok"
    assert json.loads(written[0])["command"] == ["get_property", "media-title"]


# --- the player process ----------------------------------------------------------


HANGING = """
import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).touch()  # SIGTERM is ignored from now on
while True:
    time.sleep(0.1)
"""


def test_terminate_kills_a_hanging_player(tmp_path):
    player = BasePlayer(videos(), TITLES[:1])
    player.command = fake_player(tmp_path, HANGING)
    ready = tmp_path / "ready"

    async def scenario() -> int | None:
        await player._spawn([str(ready)])
        process = player.process
        assert process is not None
        await until(lambda: ready.exists() or process.returncode is not None)
        assert process.returncode is None, "the player has failed to start"
        await player.terminate(timeout=0.3)
        return process.returncode

    returncode = asyncio.run(scenario())
    if sys.platform == "win32":
        assert returncode is not None  # TerminateProcess: there is no signal to ignore
    else:
        assert returncode == -signal.SIGKILL


# mpv serving its IPC socket: every reply is 0 (the first episode, 0 percent: not watched).
# ENDS: after the first poll mpv sends end-file and closes the socket, its process exits 0.5 s later.
FAKE_MPV = """
import json, socket, sys, time
path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--input-ipc-server="))
server = socket.socket(socket.AF_UNIX)
server.settimeout(30)
server.bind(path)
server.listen(1)
connection, _ = server.accept()
connection.settimeout(30)
requests = connection.makefile("rb")
while line := requests.readline():
    request = json.loads(line)
    reply = {"data": 0, "request_id": request["request_id"], "error": "success"}
    connection.sendall(json.dumps(reply).encode() + b"\\n")
    # percent-pos ends a poll: the player sleeps until the next one
    if ENDS and request["command"] == ["get_property", "percent-pos"]:
        connection.sendall(b'{"event":"end-file","reason":"eof","playlist_entry_id":1}\\n')
        requests.close()
        connection.close()
        server.close()
        time.sleep(0.5)
        break
"""


def fake_mpv(tmp_path: Path, *, ends: bool) -> str:
    return fake_player(tmp_path, f"ENDS = {ends}\n{FAKE_MPV}")


@posix_only
def test_mpv_poll_between_socket_close_and_exit_keeps_the_end_reason(monkeypatch, tmp_path):
    """The episode reached its end: a poll after mpv closed the socket must not make the result unknown."""
    monitor = MPVPlayer._monitor_time
    # polls every 0.05 s: they write to the closed socket before the exit is seen
    monkeypatch.setattr(MPVPlayer, "_monitor_time", lambda self: monitor(self, interval=0.05))
    watched: list[str] = []
    player = MPVPlayer(videos(1), TITLES[:1], on_watched=watched.append)
    player.command = fake_mpv(tmp_path, ends=True)
    assert run_clean(player.play()) is True
    assert watched == []


@posix_only
def test_mpv_cancel_stops_the_ipc_reader(tmp_path):
    """No task of the IPC is left running and no player left behind."""
    player = MPVPlayer(videos(1), TITLES[:1], on_watched=lambda _title: None)
    player.command = fake_mpv(tmp_path, ends=False)

    async def scenario() -> None:
        task = asyncio.create_task(player.play())
        await until(lambda: player.ipc is not None and player.ipc.writer is not None)
        await asyncio.sleep(0.2)  # the reader and the polls are running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_clean(scenario())
    assert player.process is not None
    assert player.process.returncode is not None


def test_player_is_stopped_on_any_error(monkeypatch, tmp_path):
    async def broken_connect(*_args: Any, **_kwargs: Any) -> bool:
        msg = "ipc failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(MpvIpcClient, "connect", broken_connect)
    player = MPVPlayer(videos(2), TITLES)
    player.command = fake_player(tmp_path, "import time; time.sleep(60)")
    with pytest.raises(RuntimeError, match="ipc failed"):
        asyncio.run(player.play())
    assert player.process is not None
    assert player.process.returncode is not None


def test_player_is_stopped_on_cancel(tmp_path):
    player = BasePlayer(videos(), TITLES[:1])
    player.command = fake_player(tmp_path, "import time; time.sleep(60)")

    async def scenario() -> None:
        task = asyncio.create_task(player.play())
        while player.process is None:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert player.process is not None
    assert player.process.returncode is not None
