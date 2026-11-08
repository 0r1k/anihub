"""Related titles feed: the "New Related Anime" notifications of the MAL site, which the API does not give.

Sequels, prequels, side stories… of the titles in the list (watching, completed, on hold) that are not
in the list, the newest in the MAL database first: `created_at` of a title is when it was added there.
Related lists are cached for a day, the details of related titles for a week: one request per title.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .config import get_data_dir
from .mal import ANIME_FIELDS, MalAnime, MalAuthError, MalError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .mal import MalClient

logger = logging.getLogger(__name__)

# list statuses whose related titles are wanted: not dropped, not planned
SOURCE_STATUSES = ("watching", "completed", "on_hold")
RELATED_TTL = 24 * 3600
DETAILS_TTL = 7 * 24 * 3600
CONCURRENCY = 4
# music videos, promo videos and commercials: noise, the site shows them but nobody watches them
SKIP_MEDIA_TYPES = {"music", "pv", "cm"}
# the first time the feed is shown, titles added to MAL during this period are unread
FIRST_UNREAD = 30 * 24 * 3600
_DETAILS_FIELDS = ANIME_FIELDS.replace(",my_list_status", "") + ",created_at"
# MAL site wording
RELATIONS = {
    "sequel": "Sequel",
    "prequel": "Prequel",
    "alternative_setting": "Alternative Setting",
    "alternative_version": "Alternative Version",
    "side_story": "Side Story",
    "parent_story": "Parent Story",
    "summary": "Summary",
    "full_story": "Full Story",
    "spin_off": "Spin-Off",
    "character": "Character",
    "other": "Other",
}


@dataclass
class FeedItem:
    anime: MalAnime
    # sequel, prequel… of `source`
    relation: str
    source: MalAnime
    # when the title was added to the MAL database, unix time (0: unknown)
    added: float


def _path() -> Path:
    return get_data_dir() / "mal_feed.json"


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


class Feed:
    """The feed state: cached related lists and title details, what is read and hidden."""

    def __init__(self) -> None:
        self.related: dict[str, list[Any]] = {}
        self.details: dict[str, list[Any]] = {}
        self.read_at = 0.0
        self.hidden: set[int] = set()
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            self.related = data.get("related", {})
            self.details = data.get("details", {})
            self.read_at = float(data.get("read_at", 0))
            self.hidden = set(data.get("hidden", []))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("broken feed file %s, start over", _path(), exc_info=True)
        if not self.read_at:
            self.read_at = time.time() - FIRST_UNREAD

    def save(self) -> None:
        data = {
            "read_at": self.read_at,
            "hidden": sorted(self.hidden),
            "related": self.related,
            "details": self.details,
        }
        with contextlib.suppress(OSError):
            _path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def is_unread(self, item: FeedItem) -> bool:
        return item.added > self.read_at

    def mark_read(self) -> None:
        self.read_at = time.time()
        self.save()

    def hide(self, anime_id: int) -> None:
        self.hidden.add(anime_id)
        self.save()

    async def load(
        self, client: MalClient, *, force: bool = False, on_progress: Callable[[int, int], None] | None = None
    ) -> list[FeedItem]:
        """Related titles not in the list, the newest in the MAL database first."""
        entries = await client.user_list()
        in_list = {a.id for a in entries}
        sources = [a for a in entries if a.list_status and a.list_status.status in SOURCE_STATUSES]
        semaphore = asyncio.Semaphore(CONCURRENCY)
        now = time.time()
        done = 0

        async def related(anime: MalAnime) -> list[tuple[int, str]]:
            nonlocal done
            cached = self.related.get(str(anime.id))
            if not cached or force or now - cached[0] > RELATED_TTL:
                async with semaphore:
                    try:
                        self.related[str(anime.id)] = [now, await client.related(anime.id)]
                    except MalAuthError:
                        raise
                    except MalError:
                        # one title failed: keep the old data, the rest of the feed is still useful
                        logger.warning("related titles of %s", anime.id, exc_info=True)
            done += 1
            if on_progress:
                on_progress(done, len(sources))
            return [(int(i), r) for i, r in self.related.get(str(anime.id), [0, []])[1]]

        found: dict[int, tuple[str, MalAnime]] = {}
        for anime, pairs in zip(sources, await asyncio.gather(*(related(a) for a in sources)), strict=True):
            for related_id, relation in pairs:
                if related_id not in in_list and related_id not in self.hidden:
                    found.setdefault(related_id, (relation, anime))

        async def details(anime_id: int) -> None:
            cached = self.details.get(str(anime_id))
            if cached and now - cached[0] < DETAILS_TTL:
                return
            async with semaphore:
                try:
                    self.details[str(anime_id)] = [now, await client.get_anime_json(anime_id, _DETAILS_FIELDS)]
                except MalAuthError:
                    raise
                except MalError:
                    logger.warning("details of %s", anime_id, exc_info=True)

        await asyncio.gather(*(details(i) for i in found))
        # forget titles that are not related anymore
        self.details = {k: v for k, v in self.details.items() if int(k) in found}
        self.save()

        items = []
        for anime_id, (relation, source) in found.items():
            if (cached := self.details.get(str(anime_id))) is None:
                continue
            anime = MalAnime.from_json(cached[1])
            if anime.media_type in SKIP_MEDIA_TYPES:
                continue
            items.append(FeedItem(anime, relation, source, _timestamp(anime.created_at)))
        return sorted(items, key=lambda i: -i.added)
