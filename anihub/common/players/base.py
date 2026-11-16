from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import tempfile
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from anihub.common.m3u import checked_url, generate_m3u_str_playlist, one_line

if TYPE_CHECKING:
    from anicli_api.player.base import Video

logger = logging.getLogger(__name__)


class PlayerNotFoundError(FileNotFoundError):
    def __init__(self, command: str) -> None:
        super().__init__(command)
        self.command = command


class PlayerArgsError(ValueError):
    """The player command or arguments of the settings cannot be split (an unclosed quote)."""


class PlayerFailedError(Exception):
    """The player could not play the video (a dead link, an unsupported format)."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.title = title


IS_WINDOWS = os.name == "nt"
# where installers put players that are not added to PATH (VLC on Windows and macOS, mpv.app)
_KNOWN_LOCATIONS = {
    "mpv": (
        "/Applications/mpv.app/Contents/MacOS/mpv",
        "/opt/homebrew/bin/mpv",
        "/usr/local/bin/mpv",
        r"%ProgramFiles%\mpv\mpv.exe",
        r"%LOCALAPPDATA%\Programs\mpv\mpv.exe",
    ),
    "vlc": (
        "/Applications/VLC.app/Contents/MacOS/VLC",
        r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
        r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe",
    ),
}


def _split_windows(text: str) -> list[str]:
    """Windows rules without escapes: double quotes group spaces anywhere in an argument and are removed."""
    args: list[str] = []
    current: list[str] = []
    quoted = started = False
    for char in text:
        if char == '"':
            quoted = not quoted
            started = True
        elif char.isspace() and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if quoted:
        msg = "No closing quotation"
        raise ValueError(msg)
    if started:
        args.append("".join(current))
    return args


def split_args(text: str, *, windows: bool = IS_WINDOWS) -> list[str]:
    """Command line arguments. On Windows backslashes are path separators, not escapes."""
    try:
        return _split_windows(text) if windows else shlex.split(text)
    except ValueError as e:
        msg = f"{e}: {text}"
        raise PlayerArgsError(msg) from e


def split_command(command: str) -> list[str]:
    """Player command to argv: `C:\\Program Files\\VLC\\vlc.exe` (an existing path with spaces) stays whole."""
    command = command.strip()
    if Path(command.strip('"')).is_file():
        return [command.strip('"')]
    return split_args(command)


def resolve_executable(name: str) -> str | None:
    """Full path of the player: from PATH or, for a bare mpv/vlc, the usual install locations."""
    if found := shutil.which(name):
        return found
    if os.sep in name or "/" in name:
        return None
    for location in _KNOWN_LOCATIONS.get(Path(name).stem.lower(), ()):
        path = Path(os.path.expandvars(location))
        if "%" not in str(path) and path.is_file():
            return str(path)
    return None


def resolve_command(command: str) -> list[str]:
    """Player command to argv with the full path of the program.

    PlayerArgsError: the command cannot be split (an unclosed quote), PlayerNotFoundError: there is no such program.
    """
    parts = split_command(command)
    executable = resolve_executable(parts[0]) if parts else None
    if executable is None:
        raise PlayerNotFoundError(command)
    return [executable, *parts[1:]]


def is_installed(command: str) -> bool:
    try:
        resolve_command(command)
    except (PlayerArgsError, PlayerNotFoundError):
        return False
    return True


class BasePlayer:
    """External video player running one video or a m3u playlist.

    `play()` returns True if playback reached the end of the last video (or the user skipped it), False if the user
    closed the player before, None if the player cannot report it. PlayerFailedError: the last video failed to play.

    `on_watched(title)` is called once per video watched almost to the end, if the player can track it:
    `watched` holds titles reported so far.
    """

    command: str = NotImplemented

    def __init__(
        self,
        videos: Sequence[Video],
        titles: Sequence[str],
        *,
        args: str = "",
        start: int | None = None,
        save_time: bool = False,
        on_watched: Callable[[str], None] | None = None,
    ) -> None:
        if not videos:
            msg = "videos should not be empty"
            raise ValueError(msg)
        if len(videos) != len(titles):
            msg = "videos and titles length mismatch"
            raise ValueError(msg)
        self.videos = videos
        self.titles = titles
        self.args = args
        self.start = start
        self.save_time = save_time
        self.on_watched = on_watched
        self.watched: set[str] = set()
        self.process: Process | None = None

    # --- to override ------------------------------------------------------------

    def build_args(self, target: str) -> list[str]:
        """Player arguments; `target` is a video url or a playlist path."""
        return [*split_args(self.args), target]

    async def run(self, args: list[str]) -> bool | None:
        await self._spawn(args)
        await self._wait()
        return None

    # --- common -----------------------------------------------------------------

    def report_watched(self, title: str) -> None:
        if title in self.watched:
            return
        self.watched.add(title)
        if self.on_watched:
            try:
                self.on_watched(title)
            except Exception:
                logger.exception("on_watched callback failed")

    @property
    def headers(self) -> dict[str, str]:
        # all videos have the same provider (eg: only aniboom or sibnet), headers are equal
        return {k.lower(): v for k, v in self.videos[0].headers.items()}

    async def play(self) -> bool | None:
        # an unclosed quote in the command is PlayerArgsError: installing a player would not fix it
        resolve_command(self.command)
        if len(self.videos) == 1:
            # a link from the site is the last argument: "--script=..." would be taken for an option
            return await self._play_target(checked_url(self.videos[0].url))

        playlist = generate_m3u_str_playlist(self.videos, self.titles)
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".m3u", encoding="utf-8") as f:
            f.write(playlist)
            playlist_path = Path(f.name)
        try:
            return await self._play_target(str(playlist_path))
        finally:
            playlist_path.unlink(missing_ok=True)

    async def _play_target(self, target: str) -> bool | None:
        try:
            return await self.run(self.build_args(target))
        finally:
            # any failure or cancellation: no player left behind (Windows cannot delete an open playlist)
            await self.terminate()

    async def _spawn(self, args: list[str]) -> None:
        cmd = resolve_command(self.command)
        logger.debug("Executing: %s", shlex.join([*cmd, *args]))
        # player output must not leak into the terminal: it breaks TUI rendering
        self.process = await create_subprocess_exec(*cmd, *args, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)

    async def _wait(self) -> None:
        if self.process:
            await self.process.wait()

    async def terminate(self, timeout: float = 3) -> None:
        """Stop the player if it still runs: politely, then kill it if it hangs."""
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout)


class VLCPlayer(BasePlayer):
    command = "vlc"

    def build_args(self, target: str) -> list[str]:
        args = ["--play-and-exit"]
        if user_agent := self.headers.get("user-agent"):
            args.append(f"--http-user-agent={one_line(user_agent)}")
        if referer := self.headers.get("referer"):
            args.append(f"--http-referrer={one_line(referer)}")
        if self.start:
            args.append(f"--start-time={self.start}")
        if len(self.videos) == 1:
            args.append(f"--meta-title={one_line(self.titles[0])}")
        return [*args, *split_args(self.args), target]


class CustomPlayer(BasePlayer):
    """Any player accepting an url or m3u playlist path as the last argument."""

    def __init__(self, command: str, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.command = command
