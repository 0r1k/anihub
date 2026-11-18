"""The Watching tab: MyAnimeList "watching" titles with new episodes on top, like the ongoing tab.

For every linked title the site of the link is asked how many episodes it has; more than watched on MAL
means new episodes. When the count of the site grows, the time is remembered (watching.json), so the latest
releases go first and are marked NEW.

NEW means an episode came out and it is not watched on MyAnimeList: a count seen for the first time (a title new
in the list, a new link, the first check that did not fail) is only remembered. A leaked episode is never NEW,
even when the setting shows them: the NEW count is the one without the leaked episodes (a title finished on
MyAnimeList has nothing to leak). A release the ongoing list of the site noticed (ongoing.json) counts too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.text import Text

from anihub.common import mal_links, ongoing_state, storage
from anihub.common.config import get_data_dir
from anihub.common.episodes import split_leaked
from anihub.common.history import item_key

from . import mal, ongoing
from .i18n import t

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from anihub.common.mal import MalAnime

    from .session import Session

logger = logging.getLogger(__name__)
SITE_TIMEOUT = 20
# parallel requests to the sites
CONCURRENCY = 6
# available episodes are cached: returning from the title page should not ask all sites again
CACHE_TTL = 600
# (time.time(), `Entry.counts`) by (extractor name, item key)
_cache: dict[tuple[str, str], tuple[float, tuple[int, int]]] = {}
# the counts of a link not checked for this long are forgotten: the title left the list or got another link
FORGET_AFTER = 90 * 24 * 3600
# names of `Entry.counts` in the saved state
_COUNTS = ("with_leaked", "released")


@dataclass
class Entry:
    anime: MalAnime
    link: dict[str, Any] | None
    item: Any = None
    # the last episode on the site with the leaked episodes and without them, None: not checked yet or failed
    counts: tuple[int, int] | None = None
    # the count with the leaked episodes is shown: the setting is on or the title is finished on MyAnimeList
    with_leaked: bool = False
    checking: bool = False
    failed: bool = False
    # when the shown count grew (0: never)
    updated_at: float = 0

    @property
    def available(self) -> int | None:
        """The last episode on the site, None: not checked yet or failed."""
        if self.counts is None:
            return None
        return self.counts[0 if self.with_leaked else 1]

    @property
    def finished(self) -> bool:
        """Finished on MyAnimeList: nothing to leak, see anihub.common.episodes."""
        return self.anime.airing_status == "finished_airing"

    @property
    def released(self) -> int | None:
        """The last episode on the site without the leaked ones (whatever the setting), None: not checked."""
        if self.counts is None:
            return None
        return self.counts[0 if self.finished else 1]

    @property
    def new_released(self) -> int:
        """Released episodes not watched on MyAnimeList: only they make the title NEW."""
        return max(0, self.released - self.watched) if self.released is not None else 0

    @property
    def watched(self) -> int:
        return self.anime.list_status.num_episodes_watched if self.anime.list_status else 0

    @property
    def unwatched(self) -> int:
        return max(0, self.available - self.watched) if self.available is not None else 0

    @property
    def extractor_name(self) -> str:
        return self.link["extractor_name"] if self.link else ""


def entries(session: Session, animes: list[MalAnime]) -> list[Entry]:
    result = []
    released = ongoing_state.releases()
    for anime in animes:
        link = mal_links.find_by_mal_id(anime.id, session.settings.extractor_name)
        item = None
        if link:
            try:
                item = mal_links.restore(link, session.http_kwargs)
            except Exception:
                # the extractor of the link was removed or changed its models
                link = None
        entry = Entry(anime, link, item, checking=item is not None)
        if link and item is not None and (release := released.get((link["extractor_name"], item_key(item)))):
            # an episode out already watched on MyAnimeList is not a release to show
            if release.episode is None or release.episode > entry.watched:
                entry.updated_at = release.updated_at
        result.append(entry)
    return result


def _last(episodes: Sequence[Any]) -> int:
    ordinals = []
    for episode in episodes:
        try:
            ordinals.append(int(episode.ordinal))
        except (TypeError, ValueError):
            continue
    return max(ordinals, default=len(episodes))


async def _available(item: Any) -> tuple[int, int]:
    """The last episode on the site: with the leaked ones and without them."""
    anime = await item.a_get_anime()
    episodes = await anime.a_get_episodes()
    return _last(episodes), _last(split_leaked(episodes)[0])


async def check(entry: Entry, semaphore: asyncio.Semaphore, *, force: bool = False, show_leaked: bool = False) -> None:
    """Fill `entry.counts` from the site of the link (cached for CACHE_TTL)."""
    key = (entry.extractor_name, item_key(entry.item))
    # a title finished on MyAnimeList has nothing to leak: see anihub.common.episodes
    entry.with_leaked = show_leaked or entry.finished
    cached = _cache.get(key)
    # wall clock: the monotonic one stops during suspend, a night's sleep would keep the counts fresh.
    # A clock set back must not keep an entry forever, hence the lower bound
    if cached and not force and 0 <= time.time() - cached[0] < CACHE_TTL:
        entry.counts, entry.checking = cached[1], False
        return
    try:
        async with semaphore:
            counts = await asyncio.wait_for(_available(entry.item), SITE_TIMEOUT)
        entry.counts = counts
        _cache_counts(key, counts)
    except Exception:
        entry.failed = True
    finally:
        entry.checking = False


def _cache_counts(key: tuple[str, str], counts: tuple[int, int]) -> None:
    now = time.time()
    # expired ones are dropped, not only replaced: titles leave the list, links change
    for expired in [k for k, (at, _) in _cache.items() if not 0 <= now - at < CACHE_TTL]:
        del _cache[expired]
    _cache[key] = (now, counts)


def _state_path() -> Path:
    return get_data_dir() / "watching.json"


def _time(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _grown_at(record: dict[str, Any], name: str, count: int, now: float) -> float:
    """Remember the count (the highest one seen), return when it grew the last time (0: never)."""
    known = record.get(name)
    if not isinstance(known, (int, float)):
        record[name], record[f"{name}_at"] = count, 0  # nothing to compare with yet
    elif count > known:
        record[name], record[f"{name}_at"] = count, now
    # fewer than before (the site lost an episode for a while) is not remembered: getting it back is not new
    return _time(record.get(f"{name}_at"))


def _recent(state: dict[str, Any], now: float) -> dict[str, Any]:
    """The links checked within FORGET_AFTER, by site."""
    kept: dict[str, Any] = {}
    for name, site in state.items():
        if not isinstance(site, dict):
            continue
        records = {
            key: record
            for key, record in site.items()
            if isinstance(record, dict) and now - _time(record.get("checked_at")) < FORGET_AFTER
        }
        if records:
            kept[name] = records
    return kept


def remember_releases(items: list[Entry], now: float | None = None) -> None:
    """Notice when the episodes count of the site grows: `Entry.updated_at` of the checked entries."""
    now = time.time() if now is None else now
    path = _state_path()
    state: dict[str, Any] = storage.read_json(path, {})
    for entry in items:
        if entry.counts is None:
            continue
        site = state.get(entry.extractor_name)
        if not isinstance(site, dict):
            site = state[entry.extractor_name] = {}
        key = item_key(entry.item)
        record = site.get(key)
        if not isinstance(record, dict):
            record = site[key] = {}
        grown = [_grown_at(record, name, count, now) for name, count in zip(_COUNTS, entry.counts, strict=True)]
        record["title"] = entry.anime.title
        record["checked_at"] = now
        # the count without the leaked episodes, whatever the setting: a leak is not a release.
        # The ongoing list may have noticed the release earlier (see `entries`)
        entry.updated_at = max(grown[0 if entry.finished else 1], entry.updated_at)
    try:
        storage.write_json(path, _recent(state, now), indent=2)
    except OSError:
        # only the times of the releases are lost: they are not marked NEW
        logger.warning("failed to save %s", path, exc_info=True)


def ordered(items: list[Entry]) -> list[Entry]:
    """New episodes (latest release first), then caught up, then not checked/unlinked; MAL order inside."""

    def group(entry: Entry) -> int:
        if entry.unwatched:
            return 0
        if entry.available is not None or entry.checking:
            return 1
        return 2

    return sorted(items, key=lambda e: (group(e), -e.updated_at if e.new_released else 0))


def is_new(entry: Entry, now: float) -> bool:
    """A released episode not watched on MyAnimeList came out lately."""
    return bool(entry.new_released and entry.updated_at and now - entry.updated_at < ongoing.NEW_FOR)


def label(entry: Entry, now: float) -> Text:
    text = Text()
    new = is_new(entry, now)
    if new:
        text.append(f" {t('NEW')} ", style="bold reverse")
        text.append(" ")
    text.append(entry.anime.title, style="bold")
    text.append(f" · {mal.progress_label(entry.anime)}", style="dim")
    if entry.unwatched:
        text.append(" · " + t("unwatched: {count}", count=entry.unwatched), style="bold")
        if new:
            text.append(" " + ongoing.ago(now - entry.updated_at), style="dim")
    elif entry.checking:
        text.append(" · " + t("checking the site…"), style="dim italic")
    elif entry.available is not None:
        text.append(" · " + t("caught up"), style="dim")
    elif entry.failed:
        text.append(" · " + t("the site did not answer"), style="dim")
    if entry.link:
        text.append(f"\n{entry.extractor_name}: {entry.link['data']['title']}", style="dim")
    else:
        text.append("\n" + t("not watched in anihub yet: Enter to find it on the site"), style="dim italic")
    return text
