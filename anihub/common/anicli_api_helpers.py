import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anicli_api.player.base import Video
from anicli_api.tools.helpers import get_video_by_quality

if TYPE_CHECKING:
    from anicli_api.base import BaseAnime, BaseEpisode, BaseSource


def source_hash(source: "BaseSource") -> int:
    """default hash function helper for build playlist"""
    return hash((source.title, urlsplit(source.url).netloc))


def default_batch_gen_title(
    anime: "BaseAnime",
    episode: "BaseEpisode",
    _source: "BaseSource",
    _video: Video,
) -> str:
    return f"{anime.title} - {episode.num} {episode.title}"


async def default_get_videos(source: "BaseSource") -> Sequence[Video]:
    return await source.a_get_videos()


T_TITLE_GEN_CB = Callable[["BaseAnime", "BaseEpisode", "BaseSource", Video], str]
T_GET_VIDEOS = Callable[["BaseSource"], Awaitable[Sequence[Video]]]


async def videos_iterator(
    episodes: list["BaseEpisode"],
    *,
    initial_anime: "BaseAnime",
    initial_source: "BaseSource",
    initial_video: Video,
    cb_title: T_TITLE_GEN_CB = default_batch_gen_title,
    get_videos: T_GET_VIDEOS = default_get_videos,
    lookahead: int = 1,
) -> AsyncGenerator[tuple[Video, str, "BaseSource"], None]:
    """video iterator helper functions for reuse in playlist-like features

    how it works:
    1. iterate in episodes
    2. iterate in source and compare dubber name (source.title) and base player url.netloc
    3. iterate until not found player provider (apologize, not exists target video with required dubber and player provider)

    Args:
        episodes: episodes to iterate over
        initial_anime: initial anime object (for title)
        initial_source: initial source (for compare in search)
        initial_video: initial video (for compare in search)
        cb_title: callback generator title for video
        get_videos: videos of the matched source (eg: cached)
        lookahead: episodes fetched in parallel ahead of the yielded one

    Returns:
        async iterator with video and title objects
    """
    # predicate compare sources in iterator
    base_hash = source_hash(initial_source)

    async def fetch(episode: "BaseEpisode") -> tuple["BaseSource", Sequence[Video]] | None:
        sources = await episode.a_get_sources()
        match_source = next((s for s in sources if source_hash(s) == base_hash), None)
        if not match_source:
            return None
        return match_source, await get_videos(match_source)

    # the next episodes are requested while the previous ones are yielded (and played)
    pending = [asyncio.ensure_future(fetch(episode)) for episode in episodes[: max(1, lookahead)]]
    try:
        for i, episode in enumerate(episodes):
            if i + len(pending) < len(episodes):
                pending.append(asyncio.ensure_future(fetch(episodes[i + len(pending)])))
            found = await pending.pop(0)
            # Stop iteration if the specific dubber/provider is not found
            if not found or not found[1]:
                return
            match_source, videos = found
            # same quality or the closest one
            match_video = get_video_by_quality(list(videos), initial_video.quality)
            yield match_video, cb_title(initial_anime, episode, match_source, match_video), match_source
    finally:
        for task in pending:
            task.cancel()
        # cancelled or failed requests of episodes that are not played anymore
        await asyncio.gather(*pending, return_exceptions=True)
