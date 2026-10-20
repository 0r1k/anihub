"""Playback scenarios: fetch videos, write history and run the video player."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from anicli_api.tools.helpers import get_video_by_quality

from anicli.common import history
from anicli.common.anicli_api_helpers import videos_iterator
from anicli.common.players import PlayerNotFoundError, create_player

from .i18n import t

if TYPE_CHECKING:
    from anicli_api.base import BaseAnime, BaseEpisode, BaseSource
    from anicli_api.player.base import Video

    from .session import Session


class PlaybackError(Exception):
    pass


# called with the episode number when the episode is watched (MyAnimeList progress)
OnWatched = Callable[[int], None]
# asked when the player cannot report whether the episode was watched
ConfirmWatched = Callable[[str], Awaitable[bool]]


@dataclass
class PlayerResult:
    # True if played to the end, False if the player was closed, None if unknown
    completed: bool | None
    # titles reported as watched by the player
    watched: set[str]


@dataclass
class BatchResult:
    played: int
    # the user closed the player (or declined to continue) before the end
    stopped: bool


def episode_title(anime: BaseAnime, episode: BaseEpisode) -> str:
    return f"{anime.title} - {episode.ordinal} {episode.title}".strip()


# video links of the sources, shared by the quality column of the dub list and the playback:
# the links of video hostings live for hours, a short cache is safe
VIDEOS_TTL = 600
_videos_cache: dict[tuple[str, str | None], tuple[float, list[Video]]] = {}


async def source_videos(source: BaseSource) -> list[Video]:
    """`source.a_get_videos()` cached for VIDEOS_TTL (the episode is in the source url)."""
    key = (source.url, getattr(source, "cdn_videohub_vk_id", None))
    cached = _videos_cache.get(key)
    if cached and time.monotonic() - cached[0] < VIDEOS_TTL:
        return cached[1]
    videos = list(await source.a_get_videos())
    if videos:
        _videos_cache[key] = (time.monotonic(), videos)
    return videos


def max_quality(videos: Sequence[Video]) -> int:
    """The best video quality (0: audio only or unknown)."""
    return max((v.quality for v in videos), default=0)


async def _pick_video(source: BaseSource, quality: int) -> Video:
    player = urlsplit(source.url).netloc
    try:
        videos = await source_videos(source)
    except Exception as e:
        msg = t(
            "Player {player} («{source}») failed to return video: {error}. Try another player",
            player=player,
            source=source.title,
            error=f"{type(e).__name__}: {e}",
        )
        raise PlaybackError(msg) from e
    if not videos:
        msg = t(
            "Player {player} («{source}») returned no video. Try another player", player=player, source=source.title
        )
        raise PlaybackError(msg)
    return get_video_by_quality(list(videos), quality)


async def _run_player(
    session: Session,
    videos: Sequence[Video],
    titles: Sequence[str],
    start: int | None = None,
    on_watched: Callable[[str], None] | None = None,
) -> PlayerResult:
    s = session.settings
    player = create_player(
        s.player,
        videos,
        titles,
        custom_command=s.player_command,
        args=s.player_args,
        start=start,
        save_time=True,
        on_watched=on_watched,
    )
    try:
        completed = await player.play()
    except PlayerNotFoundError as e:
        raise PlaybackError(
            t("Player not found: {command}. Choose another one in settings (F2)", command=e.command)
        ) from e
    return PlayerResult(completed, player.watched)


async def _report_unknown(
    result: PlayerResult, title: str, ordinal: int | None, on_watched: OnWatched, confirm: ConfirmWatched | None
) -> None:
    """The player cannot track watching (not mpv): ask the user."""
    if ordinal is not None and result.completed is None and not result.watched and confirm and await confirm(title):
        on_watched(ordinal)


async def play_episode(
    session: Session,
    item: Any,
    extractor_name: str,
    anime: BaseAnime,
    episode: BaseEpisode,
    source: BaseSource,
    *,
    on_watched: OnWatched | None = None,
    confirm_watched: ConfirmWatched | None = None,
) -> None:
    video = await _pick_video(source, session.settings.quality)
    title = episode_title(anime, episode)
    history.save(item, extractor_name, {title: source}, title, ordinal=episode.ordinal)
    result = await _run_player(
        session, [video], [title], on_watched=(lambda _title: on_watched(episode.ordinal)) if on_watched else None
    )
    if on_watched:
        await _report_unknown(result, title, episode.ordinal, on_watched, confirm_watched)


async def _always(_title: str) -> bool:
    return True


async def play_batch(
    session: Session,
    item: Any,
    extractor_name: str,
    anime: BaseAnime,
    episodes: Sequence[BaseEpisode],
    source: BaseSource,
    *,
    on_fetched: Callable[[int, str], None] = lambda _n, _title: None,
    on_play: Callable[[Sequence[str]], None] = lambda _titles: None,
    confirm_next: Callable[[str], Awaitable[bool]] = _always,
    on_watched: OnWatched | None = None,
    confirm_watched: ConfirmWatched | None = None,
) -> BatchResult:
    """Play episodes with the same dubber and player by chunks of `m3u_size` episodes.

    Stops when the user closes the player before the end of a chunk. If the player cannot report it
    (not mpv), `confirm_next` is asked before the next chunk.
    """
    initial_video = await _pick_video(source, session.settings.quality)
    history.save(item, extractor_name)

    chunk: list[tuple[Video, str]] = []
    played = 0
    ask_before: str | None = None  # title of the next chunk start, when confirmation is required
    ordinals: dict[str, int] = {}

    def make_title(anime: BaseAnime, episode: BaseEpisode, _source: BaseSource, _video: Video) -> str:
        title = episode_title(anime, episode)
        ordinals[title] = episode.ordinal
        return title

    def title_watched(title: str) -> None:
        if on_watched and title in ordinals:
            on_watched(ordinals[title])

    async def flush() -> bool | None:
        nonlocal played
        titles = [title for _, title in chunk]
        # player reports the current title via IPC later, remember the start point right now
        history.update_last({"episode": titles[0], "time": None})
        on_play(titles)
        result = await _run_player(
            session, [video for video, _ in chunk], titles, on_watched=title_watched if on_watched else None
        )
        if on_watched:
            # progress is the max watched episode: the last one of the chunk is enough
            await _report_unknown(result, titles[-1], ordinals.get(titles[-1]), on_watched, confirm_watched)
        played += len(chunk)
        chunk.clear()
        return result.completed

    async for video, title, match_source in videos_iterator(
        list(episodes),
        initial_anime=anime,
        initial_source=source,
        initial_video=initial_video,
        cb_title=make_title,
    ):
        if ask_before is not None:
            if not await confirm_next(title):
                return BatchResult(played, stopped=True)
            ask_before = None
        history.append_source(title, match_source, ordinals.get(title))
        chunk.append((video, title))
        on_fetched(played + len(chunk), title)
        if len(chunk) >= session.settings.m3u_size:
            completed = await flush()
            if completed is False:
                return BatchResult(played, stopped=True)
            if completed is None:
                ask_before = ""
    if chunk and (await flush()) is False:
        return BatchResult(played, stopped=True)
    return BatchResult(played, stopped=False)


async def resume(
    session: Session,
    entry: dict[str, Any],
    *,
    on_watched: OnWatched | None = None,
    confirm_watched: ConfirmWatched | None = None,
) -> None:
    """Continue the last watched episode of the history entry from the saved position."""
    try:
        source = history.load_source(entry, entry["episode"], session.http_kwargs)
    except LookupError as e:
        raise PlaybackError(t("No saved sources for this title")) from e
    video = await _pick_video(source, session.settings.quality)
    history.move_to_top(entry)
    title = entry["episode"]
    # entries saved before 7.1 have no episode numbers
    ordinal = history.episode_ordinal(entry, title)
    if on_watched is None or ordinal is None:
        await _run_player(session, [video], [title], start=entry.get("time") or 0)
        return
    episode = ordinal
    result = await _run_player(
        session, [video], [title], start=entry.get("time") or 0, on_watched=lambda _t: on_watched(episode)
    )
    await _report_unknown(result, title, ordinal, on_watched, confirm_watched)
