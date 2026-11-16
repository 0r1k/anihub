"""play_batch: chunks of episodes, stopping, asking before the next chunk and counting watched episodes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from anicli_api.player.base import Video

from anihub.common.players import PlayerFailedError
from anihub.tui import playback
from anihub.tui.playback import PlaybackError


@dataclass
class FakeSource:
    title: str
    url: str

    async def a_get_videos(self) -> list[Video]:
        return [Video(type="mp4", quality=720, url=f"https://cdn.example/{self.url.rsplit('/', 1)[-1]}.mp4")]


@dataclass
class FakeEpisode:
    ordinal: Any
    title: str = ""
    has_source: bool = True

    async def a_get_sources(self) -> list[FakeSource]:
        if not self.has_source:
            return [FakeSource("Other dub", f"https://player.example/{self.ordinal}")]
        return [FakeSource("Dub", f"https://player.example/{self.ordinal}")]


@dataclass
class FakePlayer:
    """Plays a chunk: `outcome` is the play() result or an exception, `watch` the titles watched by the position."""

    titles: list[str]
    outcome: Any
    watch: list[str]
    on_watched: Any
    watched: set[str] = field(default_factory=set)

    async def play(self) -> bool | None:
        for title in self.watch:
            if title in self.titles:
                self.watched.add(title)
                self.on_watched(title)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def batch(monkeypatch):
    """Runs play_batch with fake players; returns the played chunks and the counted episode numbers."""
    for name in ("save", "append_source", "update_last"):
        monkeypatch.setattr(playback.history, name, lambda *_args, **_kwargs: None)
    playback._videos_cache.clear()

    def run(
        episodes: list[FakeEpisode],
        outcomes: list[Any],
        *,
        m3u_size: int = 2,
        watch: tuple[str, ...] = (),
        confirm_next: bool = True,
        confirm_watched: bool | None = None,
    ) -> tuple[Any, list[list[str]], list[Any], list[str]]:
        chunks: list[list[str]] = []
        counted: list[Any] = []
        asked: list[str] = []

        def create_player(_player: str, _videos: Any, titles: list[str], **kwargs: Any) -> FakePlayer:
            chunks.append(list(titles))
            return FakePlayer(list(titles), outcomes[len(chunks) - 1], list(watch), kwargs["on_watched"])

        monkeypatch.setattr(playback, "create_player", create_player)
        settings = SimpleNamespace(
            stream_proxy=False, player="mpv", player_command="", player_args="", m3u_size=m3u_size, quality=720
        )
        session = SimpleNamespace(settings=settings)
        anime = SimpleNamespace(title="Anime")

        async def ask_next(title: str) -> bool:
            asked.append(f"next {title}")
            return confirm_next

        async def ask_watched(title: str) -> bool:
            asked.append(f"watched {title}")
            return bool(confirm_watched)

        result = asyncio.run(
            playback.play_batch(
                session,  # type: ignore[arg-type]
                None,
                "site",
                anime,  # type: ignore[arg-type]
                episodes,  # type: ignore[arg-type]
                FakeSource("Dub", "https://player.example/first"),  # type: ignore[arg-type]
                confirm_next=ask_next,
                on_watched=counted.append,
                confirm_watched=ask_watched if confirm_watched is not None else None,
            )
        )
        return result, chunks, counted, asked

    return run


def title(n: Any) -> str:
    return f"Anime - {n}"


def test_all_chunks_played(batch):
    result, chunks, counted, asked = batch([FakeEpisode(n) for n in range(1, 6)], [True, True, True])
    assert chunks == [[title(1), title(2)], [title(3), title(4)], [title(5)]]
    assert (result.played, result.stopped) == (5, False)
    assert counted == []  # played to the end is not watched: only the position counts
    assert asked == []


def test_closed_player_stops(batch):
    result, chunks, _counted, _asked = batch([FakeEpisode(n) for n in range(1, 6)], [False])
    assert len(chunks) == 1
    assert (result.played, result.stopped) == (2, True)


def test_unknown_result_asks_before_next_chunk(batch):
    result, chunks, _counted, asked = batch([FakeEpisode(n) for n in range(1, 6)], [None], confirm_next=False)
    assert len(chunks) == 1
    assert asked == [f"next {title(3)}"]
    assert (result.played, result.stopped) == (2, True)


def test_unknown_result_asks_if_watched(batch):
    """A player that cannot report watching (VLC): the last episode of the chunk is confirmed by the user."""
    result, _chunks, counted, asked = batch([FakeEpisode(n) for n in (13, 14)], [None], confirm_watched=True)
    assert counted == [14]
    assert asked == [f"watched {title(14)}"]
    assert result.stopped is False


def test_watched_episodes_are_counted_by_number(batch):
    _result, _chunks, counted, asked = batch(
        [FakeEpisode(n) for n in (13, 14, 15)], [True, True], watch=(title(14), title(15)), confirm_watched=True
    )
    assert counted == [14, 15]
    assert asked == []


def test_failed_player_is_an_error(batch):
    with pytest.raises(PlaybackError, match=title(2)):
        batch([FakeEpisode(n) for n in range(1, 4)], [PlayerFailedError(title(2))])


def test_stops_where_the_dub_ends(batch):
    episodes = [FakeEpisode(1), FakeEpisode(2), FakeEpisode(3, has_source=False), FakeEpisode(4)]
    result, chunks, _counted, _asked = batch(episodes, [True, True], m3u_size=2)
    assert chunks == [[title(1), title(2)]]
    assert (result.played, result.stopped) == (2, False)


def test_a_broken_player_link_is_a_playback_error():
    """`http://[x/` from the site: named in the message, not a crash of urlsplit."""

    class Failing(FakeSource):
        async def a_get_videos(self) -> list[Video]:
            msg = "no answer"
            raise RuntimeError(msg)

    with pytest.raises(PlaybackError, match=r"http://\[x/"):
        asyncio.run(playback._pick_video(Failing("Dub", "http://[x/player"), 720))  # type: ignore[arg-type]


def test_expired_videos_are_dropped():
    playback._videos_cache.clear()
    playback._videos_cache[("https://old.example/1", None)] = (time.monotonic() - playback.VIDEOS_TTL - 1, [])
    videos = asyncio.run(playback.source_videos(FakeSource("Dub", "https://player.example/1")))  # type: ignore[arg-type]
    assert len(videos) == 1
    assert list(playback._videos_cache) == [("https://player.example/1", None)]
    playback._videos_cache.clear()
