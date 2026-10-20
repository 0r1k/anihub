"""MyAnimeList glue for the TUI: link resolving, progress tracking and labels."""

from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Any

from rich.text import Text

from anicli.common import mal_links
from anicli.common.mal import SCORE_LABELS, STATUS_LABELS, MalAnime

from .i18n import t

if TYPE_CHECKING:
    from .session import Session

_CYRILLIC = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)


def status_label(status: str | None) -> str:
    return t(STATUS_LABELS[status]) if status in STATUS_LABELS else t("Not in list")


def score_label(score: int) -> str:
    return f"{score} — {t(SCORE_LABELS[score])}" if score in SCORE_LABELS else t("No score")


def progress_label(mal: MalAnime) -> str:
    watched = mal.list_status.num_episodes_watched if mal.list_status else 0
    return f"{watched}/{mal.num_episodes or '?'}"


def describe(mal: MalAnime, *, with_status: bool = False) -> Text:
    """One-line summary: title · [status ·] episodes · score · type year."""
    text = Text(mal.title, style="bold")
    details: list[str] = []
    if with_status:
        details.append(status_label(mal.list_status.status if mal.list_status else None))
    details.append(t("{progress} ep.", progress=progress_label(mal)))
    if mal.list_status and mal.list_status.score:
        details.append(f"★ {mal.list_status.score}")
    if kind := " ".join(str(i) for i in (mal.media_type.upper(), mal.year) if i):
        details.append(kind)
    text.append(" · " + " · ".join(details), style="dim")
    return text


def search_queries(titles: list[str], limit: int = 4) -> list[str]:
    """MAL knows romaji/english/japanese titles: try them before the russian one."""
    ordered = sorted(titles, key=lambda title: bool(_CYRILLIC.search(title)))
    return [title for title in ordered if len(title.strip()) >= 3][:limit]


def search_text(anime: MalAnime, extra: str = "") -> str:
    """Every title of the entry (romaji, english, japanese, synonyms, the linked site one) for `matches`.

    Titles are separated by new lines: a query cannot match across two of them.
    """
    return "\n".join(mal_links.normalize(title) for title in (*anime.all_titles, extra) if title)


def matches(text: str, query: str) -> bool:
    """`query` in `search_text`, ignoring case, spaces and punctuation: "dr stone" finds "Dr. Stone: Terraforming"."""
    return mal_links.normalize(query) in text


async def resolve(
    session: Session, item: Any, extractor_name: str, anime: Any = None, hint: MalAnime | None = None
) -> tuple[MalAnime | None, list[str]]:
    """MAL entry of the extractor title and the titles to search it manually (if not found).

    Order: saved link, `hint` (chosen in the MAL tab), MAL id from the site, exact title match.
    """
    mal = session.mal
    link = mal_links.get(item, extractor_name)
    mal_id, titles = mal_links.hints(item, *([anime] if anime is not None else []))
    if link:
        return await mal.get_anime(link["mal_id"]), titles
    if hint is not None:
        found = await mal.get_anime(hint.id)
        mal_links.save(item, extractor_name, found)
        return found, titles
    if mal_id:
        found = await mal.get_anime(mal_id)
        mal_links.save(item, extractor_name, found)
        return found, titles
    for query in search_queries(titles):
        if match := mal_links.exact_match(await mal.search(query), titles):
            # search results carry my_list_status too
            mal_links.save(item, extractor_name, match)
            return match, titles
    return None, titles


async def link(session: Session, item: Any, extractor_name: str, chosen: MalAnime) -> MalAnime:
    """Manually chosen MAL entry: refetch for the list status and remember."""
    found = await session.mal.get_anime(chosen.id)
    mal_links.save(item, extractor_name, found)
    return found


async def episode_watched(session: Session, mal_id: int, episode: int) -> tuple[MalAnime, dict[str, Any]]:
    """Update the list after the episode was watched. Returns the fresh entry and the changed fields."""
    # refetch: the list could be changed on the site or by another anihub instance
    anime = await session.mal.get_anime(mal_id)
    fields = mal_links.watched_update(anime, episode)
    if fields:
        anime.list_status = await session.mal.update_list_status(mal_id, **fields)
    return anime, fields
