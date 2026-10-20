"""Subtitles (original audio + russian subtitles) from the current and other sources.

Sites name such players like "Субтитры SubVost", "Kazoku Project.Subtitles", "Оригинал (+субтитры)".
If the current site has none, the same title is searched on the other sites: it is taken only on an exact
title match (russian, MAL or alternative titles), a wrong season is worse than nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules
from anicli.common.mal_links import normalize

if TYPE_CHECKING:
    from anicli_api.base import BaseAnime, BaseEpisode, BaseSource

    from .session import Session

logger = logging.getLogger(__name__)

_SUBTITLES = re.compile(r"субтитр|subtitle|\bsub|sub\b|оригинал", re.IGNORECASE)
_BRACKETS = re.compile(r"\[[^\]]*\]")
SITE_TIMEOUT = 20


def is_subtitles(source: BaseSource) -> bool:
    return bool(_SUBTITLES.search(source.title))


def subtitles_first(sources: Iterable[Any]) -> list[Any]:
    return sorted(sources, key=lambda s: not is_subtitles(s.source if isinstance(s, ForeignSource) else s))


@dataclass
class ForeignTitle:
    """The same title on another site."""

    extractor_name: str
    item: Any
    anime: BaseAnime
    episodes: dict[int, BaseEpisode]


@dataclass
class ForeignSource:
    site: ForeignTitle
    source: BaseSource
    ordinal: int


def _variants(title: str) -> set[str]:
    """animevost: "Русское (второй сезон) / Ao no Hako Season 2 [1-3 из 12+]" -> both names without brackets."""
    return {normalize(_BRACKETS.sub("", part)) for part in title.split(" / ")} - {""}


async def _find_on_site(session: Session, name: str, queries: list[str], wanted: set[str]) -> ForeignTitle | None:
    extractor = dynamic_load_extractor_module(name).Extractor(
        http_client=session.http, http_async_client=session.http_async
    )
    match = None
    # sites name seasons differently ("2" vs "(второй сезон)"): try the original title too
    for query in queries:
        results = await extractor.a_search(query)
        if match := next((r for r in results if _variants(r.title) & wanted), None):
            break
    if match is None:
        return None
    anime = await match.a_get_anime()
    episodes = await anime.a_get_episodes()
    by_ordinal: dict[int, BaseEpisode] = {}
    for episode in episodes:
        try:
            by_ordinal.setdefault(int(episode.ordinal), episode)
        except (TypeError, ValueError):
            continue
    return ForeignTitle(name, match, anime, by_ordinal)


async def find_titles(session: Session, current: str, queries: list[str], titles: Iterable[str]) -> list[ForeignTitle]:
    """Search the title on all sites except `current` in parallel. Broken or slow sites are skipped."""
    wanted = {normalize(t) for t in titles} - {""}
    names = [name for name in get_extractor_modules() if name != current]

    async def one(name: str) -> ForeignTitle | None:
        try:
            return await asyncio.wait_for(_find_on_site(session, name, queries, wanted), SITE_TIMEOUT)
        except Exception as e:
            # ddos guards, timeouts, layout changes: other sites still work
            logger.info("subtitles search on %s failed: %s", name, e)
            return None

    found = await asyncio.gather(*(one(name) for name in names))
    return [site for site in found if site is not None]


async def foreign_sources(sites: Iterable[ForeignTitle], ordinal: int) -> list[ForeignSource]:
    """Subtitles of the episode `ordinal` on the found sites."""

    async def one(site: ForeignTitle) -> list[ForeignSource]:
        episode = site.episodes.get(ordinal)
        if episode is None:
            return []
        try:
            sources = await asyncio.wait_for(episode.a_get_sources(), SITE_TIMEOUT)
        except Exception as e:
            logger.info("subtitles of %s episode %s failed: %s", site.extractor_name, ordinal, e)
            return []
        return [ForeignSource(site, s, ordinal) for s in sources if is_subtitles(s)]

    results = await asyncio.gather(*(one(site) for site in sites))
    return [source for chunk in results for source in chunk]
