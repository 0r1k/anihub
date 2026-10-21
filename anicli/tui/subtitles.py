"""Subtitles (original audio + russian subtitles) from the current and other sources, the title on other sites.

Sites name such players like "Субтитры SubVost", "Kazoku Project.Subtitles", "Оригинал (+субтитры)".
If the current site has none, the same title is searched on the other sites: it is taken only on an exact
match (the name, the original name or the MAL id on the title page), a wrong season is worse than nothing.
To choose another site by hand, similar names are found too: the user sees the name on the site.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from anicli.common import mal_links
from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules
from anicli.common.mal_links import normalize

if TYPE_CHECKING:
    from anicli_api.base import BaseAnime, BaseEpisode, BaseSource

    from .session import Session

logger = logging.getLogger(__name__)

_SUBTITLES = re.compile(r"субтитр|subtitle|\bsub|sub\b|оригинал", re.IGNORECASE)
_BRACKETS = re.compile(r"\[[^\]]*\]")
# seasons are written in many ways: "[ТВ-2]", "2 сезон", "Сезон 2", "(второй сезон)", "2nd Season"
_SEASON = re.compile(
    r"\b(?:тв|tv)-?\s*(\d+)\b"
    r"|\b(\d+)(?:-?(?:й|ой|st|nd|rd|th))?\s+(?:сезон|season)\w*"
    r"|\b(?:сезон|season)\w*\s+(\d+)\b"
)
_ORDINAL_SEASON = re.compile(r"\b(перв|перш|втор|друг|трет|четв|п'?ят|шест|шост)\w*\s+сезон\w*")
_ORDINALS = {
    "перв": 1,
    "перш": 1,
    "втор": 2,
    "друг": 2,
    "трет": 3,
    "четв": 4,
    "пят": 5,
    "п'ят": 5,
    "шест": 6,
    "шост": 6,
}
# "[1-12 из 12]", "2 сезон, 12 серия", "Завершен (все серии)"
_EPISODES = re.compile(r"\[[^\]]*\d+\s*-\s*\d+[^\]]*\]|\d+\s*(?:сери|епізод|episode)\w*|все серии|завершен\w*")
SITE_TIMEOUT = 20
PLAYERS_TIMEOUT = 10
# search results of a site looked at, and title pages opened to check the original name or the MAL id
_CANDIDATES = 6
_CHECKED_PAGES = 3
# a shorter similar name has at least this many words besides numbers: "Любви 2" is too vague
_SIMILAR_MIN_WORDS = 2
# how long the sites without the title wait for the original names from the found ones
_LEARN_WAIT = 8
_CYRILLIC = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)


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
    # named as one of the known titles; otherwise only the words and the numbers match
    exact: bool = True


@dataclass
class ForeignSource:
    site: ForeignTitle
    source: BaseSource
    ordinal: int


@dataclass
class SiteSearch:
    """The search of the title on one site: searching, found, missing or failed."""

    name: str
    state: str = "searching"
    site: ForeignTitle | None = None
    error: str = ""


def other_sites(current: str) -> list[str]:
    return [name for name in get_extractor_modules() if name != current]


def _variants(title: str) -> set[str]:
    """animevost: "Русское (второй сезон) / Ao no Hako Season 2 [1-3 из 12+]" -> both names without brackets."""
    return {normalize(_BRACKETS.sub("", part)) for part in title.split(" / ")} - {""}


def _season(match: re.Match[str]) -> str:
    number = int(next(group for group in match.groups() if group))
    # the first season is usually not numbered at all
    return f" {number} " if number > 1 else " "


def _ordinal_season(match: re.Match[str]) -> str:
    number = _ORDINALS.get(match[1], 1)
    return f" {number} " if number > 1 else " "


def _words(title: str) -> set[str]:
    """Words of the title with seasons as numbers: "Наука влюблена (второй сезон)" -> {наука, влюблена, 2}."""
    text = _EPISODES.sub(" ", title.casefold().replace("ё", "е"))
    text = _SEASON.sub(_season, _ORDINAL_SEASON.sub(_ordinal_season, text))
    return set(re.findall(r"\w+", text))


def _numbers(words: set[str]) -> set[str]:
    return {w for w in words if w.isdigit()}


@dataclass
class _Wanted:
    """What the title is known as."""

    titles: list[str]
    mal_id: int | None
    # season and part numbers of the title on the current site
    numbers: set[str]
    exact: set[str] = field(default_factory=set)
    words: list[set[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        titles, self.titles = self.titles, []
        self.add(titles)

    def add(self, titles: Iterable[str]) -> None:
        for title in titles:
            self.titles.append(title)
            self.exact |= {normalize(title)} - {""}
            if (words := _words(title)) and words not in self.words:
                self.words.append(words)

    def is_exact(self, title: str) -> bool:
        return bool(_variants(title) & self.exact) or any(_words(part) in self.words for part in title.split(" / "))

    def is_similar(self, title: str) -> bool:
        """One name contains all words of the other and the numbers (season, part) are the same.

        "Научное доказательство любви 2" ~ "Влюблённые учёные и научное доказательство любви 2", not the first season.
        """
        for part in title.split(" / "):
            words = _words(part)
            for known in self.words:
                short, long = sorted((words, known), key=len)
                if (
                    _numbers(words) == _numbers(known)
                    and len(short - _numbers(short)) >= _SIMILAR_MIN_WORDS
                    and short <= long
                ):
                    return True
        return False


async def _foreign(name: str, item: Any, anime: Any, *, exact: bool) -> ForeignTitle:
    anime = anime or await item.a_get_anime()
    by_ordinal: dict[int, BaseEpisode] = {}
    for episode in await anime.a_get_episodes():
        try:
            by_ordinal.setdefault(int(episode.ordinal), episode)
        except (TypeError, ValueError):
            continue
    return ForeignTitle(name, item, anime, by_ordinal, exact)


async def _find_on_site(
    session: Session, name: str, queries: list[str], wanted: _Wanted, *, similar: bool
) -> ForeignTitle | None:
    extractor = dynamic_load_extractor_module(name).Extractor(
        http_client=session.http, http_async_client=session.http_async
    )
    # search results by url and name (anilibria, animevost: no urls): the result and the queries that found it
    found: dict[tuple[str, str], tuple[Any, set[int]]] = {}
    # sites name the title differently in russian, the original name finds it on more of them
    for i, query in enumerate(queries):
        results = await extractor.a_search(query)
        if match := next((r for r in results if wanted.is_exact(r.title)), None):
            return await _foreign(name, match, None, exact=True)
        for result in results[:_CANDIDATES]:
            found.setdefault((result.url, result.title), (result, set()))[1].add(i)
    candidates = [result for result, _ in found.values()]

    # the title page knows the original name or the MAL id: check the results of the same season first
    rejected: list[Any] = []
    likely = sorted(candidates, key=lambda r: _numbers(_words(r.title)) != wanted.numbers)
    for result in likely[:_CHECKED_PAGES]:
        anime = await result.a_get_anime()
        mal_id, titles = mal_links.hints(result, anime)
        if (mal_id and mal_id == wanted.mal_id) or any(wanted.is_exact(title) for title in titles):
            return await _foreign(name, result, anime, exact=True)
        if mal_id and wanted.mal_id:
            rejected.append(result)
    if not similar:
        return None
    candidates = [r for r in candidates if r not in rejected]
    if match := next((r for r in candidates if wanted.is_similar(r.title)), None):
        return await _foreign(name, match, None, exact=False)
    # the site search finds it both by the russian and by the original name, the season is the same
    for result, hits in found.values():
        same_season = _numbers(_words(result.title)) == wanted.numbers
        if result not in rejected and 0 in hits and len(hits) > 1 and same_season:
            return await _foreign(name, result, None, exact=False)
    return None


async def find_titles(
    session: Session,
    current: str,
    queries: list[str],
    titles: Iterable[str],
    *,
    mal_id: int | None = None,
    similar: bool = False,
    on_site: Callable[[SiteSearch], None] | None = None,
) -> list[ForeignTitle]:
    """Search the title on all sites except `current` in parallel. Broken or slow sites are skipped.

    `queries`: the name on the current site first, then the original names.
    `similar`: without an exact match take a name with the same words and numbers (`ForeignTitle.exact` is False).
    `on_site` is called as each site answers.
    """
    wanted = _Wanted(list(titles), mal_id, _numbers(_words(queries[0])) if queries else set())

    async def attempt(name: str, attempt_queries: list[str], known: _Wanted) -> tuple[ForeignTitle | None, str]:
        """The found title or None, the error name if the site failed."""
        try:
            search = _find_on_site(session, name, attempt_queries, known, similar=similar)
            return await asyncio.wait_for(search, SITE_TIMEOUT), ""
        except Exception as e:
            # ddos guards, timeouts, layout changes: other sites still work
            logger.info("search on %s failed: %r", name, e)
            return None, type(e).__name__

    first = {name: asyncio.ensure_future(attempt(name, queries, wanted)) for name in other_sites(current)}

    async def learn() -> tuple[list[str], bool]:
        """Original names from the pages of the found titles, not searched yet; True if found exactly.

        Without MyAnimeList only the russian name can be known, and sites name the title differently in russian:
        the original name from one site finds it on the others.
        """
        if first:
            await asyncio.wait(first.values(), timeout=_LEARN_WAIT)
        learned: dict[bool, list[str]] = {True: [], False: []}
        for task in first.values():
            if task.done() and (site := task.result()[0]) is not None:
                originals = [t for t in mal_links.hints(site.item, site.anime)[1] if not _CYRILLIC.search(t)]
                learned[site.exact] += [t for t in originals if t not in queries and t not in learned[site.exact]]
        exact = bool(learned[True])
        return learned[exact][:2], exact

    learning = asyncio.ensure_future(learn())

    async def one(name: str) -> ForeignTitle | None:
        site, error = await first[name]
        if site is None and not error:
            learned, exact = await asyncio.shield(learning)
            if learned:
                site, error = await attempt(name, learned, _Wanted([*wanted.titles, *learned], mal_id, wanted.numbers))
                # names learned from a similar title: what they find is not more certain than it
                if site is not None and not exact:
                    site.exact = False
        search = SiteSearch(name, "failed" if error else "found" if site else "missing", site, error)
        if on_site:
            on_site(search)
        return site

    try:
        found = await asyncio.gather(*(one(name) for name in first))
    finally:
        for task in (*first.values(), learning):
            task.cancel()
    return [site for site in found if site is not None]


async def site_players(site: ForeignTitle, ordinal: int) -> list[str]:
    """Video hostings of the episode `ordinal` on the site (empty: no such episode or the site failed)."""
    episode = site.episodes.get(ordinal)
    if episode is None:
        return []
    try:
        sources = await asyncio.wait_for(episode.a_get_sources(), PLAYERS_TIMEOUT)
    except Exception as e:
        logger.info("players of %s episode %s failed: %s", site.extractor_name, ordinal, e)
        return []
    # anilibria, dreamcast: the site's own player has no url, its name instead
    return sorted({urlsplit(s.url).netloc or s.title for s in sources} - {""})


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
