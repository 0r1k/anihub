"""The Watching tab: MyAnimeList "watching" titles with new episodes on top, like the ongoing tab.

For every linked title the site of the link is asked how many episodes it has; more than watched on MAL
means new episodes. When the available count grows, the time is remembered (ongoing_state, "@watching"),
so the latest releases go first.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from rich.text import Text

from anicli.common import mal_links, ongoing_state

from . import mal, ongoing
from .i18n import t

if TYPE_CHECKING:
    from anicli.common.mal import MalAnime

    from .session import Session

STATE_KEY = "@watching"
SITE_TIMEOUT = 20
# parallel requests to the sites
CONCURRENCY = 6
# available episodes are cached: returning from the title page should not ask all sites again
CACHE_TTL = 600
_cache: dict[tuple[str, str], tuple[float, int]] = {}


@dataclass
class Entry:
    anime: MalAnime
    link: dict[str, Any] | None
    item: Any = None
    # episodes on the site, None: not checked yet or failed
    available: int | None = None
    checking: bool = False
    failed: bool = False
    # when a new episode was noticed (0: never)
    updated_at: float = 0

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
    for anime in animes:
        link = mal_links.find_by_mal_id(anime.id, session.settings.extractor_name)
        item = None
        if link:
            try:
                item = mal_links.restore(link, session.http_kwargs)
            except Exception:
                # the extractor of the link was removed or changed its models
                link = None
        result.append(Entry(anime, link, item, checking=item is not None))
    return result


async def _available(item: Any) -> int:
    anime = await item.a_get_anime()
    episodes = await anime.a_get_episodes()
    ordinals = []
    for episode in episodes:
        try:
            ordinals.append(int(episode.ordinal))
        except (TypeError, ValueError):
            continue
    return max(ordinals, default=len(episodes))


async def check(entry: Entry, semaphore: asyncio.Semaphore, *, force: bool = False) -> None:
    """Fill `entry.available` from the site of the link (cached for CACHE_TTL)."""
    key = (entry.extractor_name, entry.item.url)
    cached = _cache.get(key)
    if cached and not force and time.time() - cached[0] < CACHE_TTL:
        entry.available, entry.checking = cached[1], False
        return
    try:
        async with semaphore:
            entry.available = await asyncio.wait_for(_available(entry.item), SITE_TIMEOUT)
        _cache[key] = (time.time(), entry.available)
    except Exception:
        entry.failed = True
    finally:
        entry.checking = False


def remember_releases(items: list[Entry]) -> None:
    """Notice when the available episodes count grows."""
    checked = [e for e in items if e.available is not None]
    fake = [SimpleNamespace(url=f"mal:{e.anime.id}", title=e.anime.title, available=e.available) for e in checked]
    updated = ongoing_state.track(STATE_KEY, fake, lambda i: f"ep:{i.available}")
    for entry in items:
        entry.updated_at = updated.get(f"mal:{entry.anime.id}", 0)


def ordered(items: list[Entry]) -> list[Entry]:
    """New episodes (latest release first), then caught up, then not checked/unlinked; MAL order inside."""

    def group(entry: Entry) -> int:
        if entry.unwatched:
            return 0
        if entry.available is not None or entry.checking:
            return 1
        return 2

    return sorted(items, key=lambda e: (group(e), -e.updated_at if e.unwatched else 0))


def label(entry: Entry, now: float) -> Text:
    text = Text()
    if entry.unwatched and entry.updated_at and now - entry.updated_at < ongoing.NEW_FOR:
        text.append(f" {t('NEW')} ", style="bold reverse")
        text.append(" ")
    text.append(entry.anime.title, style="bold")
    text.append(f" · {mal.progress_label(entry.anime)}", style="dim")
    if entry.unwatched:
        text.append(" · " + t("new: {count}", count=entry.unwatched), style="bold")
        if entry.updated_at and now - entry.updated_at < ongoing.NEW_FOR:
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
