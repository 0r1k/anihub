"""External video players."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .base import BasePlayer, CustomPlayer, PlayerNotFoundError, VLCPlayer, is_installed
from .mpv import MPVPlayer

if TYPE_CHECKING:
    from anicli_api.player.base import Video

# "custom" runs any command: the url or m3u playlist path is passed as the last argument
PLAYERS = ("mpv", "vlc", "custom")

__all__ = [
    "PLAYERS",
    "BasePlayer",
    "CustomPlayer",
    "MPVPlayer",
    "PlayerNotFoundError",
    "VLCPlayer",
    "create_player",
    "is_installed",
    "player_command",
]


def player_command(player: str, custom_command: str = "") -> str:
    if player == "custom":
        return custom_command
    return {"mpv": MPVPlayer.command, "vlc": VLCPlayer.command}[player]


def create_player(
    player: str,
    videos: Sequence[Video],
    titles: Sequence[str],
    *,
    custom_command: str = "",
    args: str = "",
    start: int | None = None,
    save_time: bool = False,
    on_watched: Callable[[str], None] | None = None,
) -> BasePlayer:
    kwargs = {"args": args, "start": start, "save_time": save_time, "on_watched": on_watched}
    if player == "mpv":
        return MPVPlayer(videos, titles, **kwargs)
    if player == "vlc":
        return VLCPlayer(videos, titles, **kwargs)
    if player == "custom":
        return CustomPlayer(custom_command, videos, titles, **kwargs)
    msg = f"unknown player {player!r}, expected one of {PLAYERS}"
    raise ValueError(msg)
