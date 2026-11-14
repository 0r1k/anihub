"""MyAnimeList profile of any user, read from the public pages of the site: the API v2 has only own data.

/profile/<name>: avatar, status lines (last online, gender, birthday, location, joined), about,
anime and manga statistics, last anime updates, favorites. /profile/<name>/friends: friends.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote

from lxml import html

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from .mal import MalClient

_ANIME_ID = re.compile(r"/anime/(\d+)")
_STATUS_CLASSES = ("watching", "completed", "on_hold", "dropped", "plan_to_watch", "reading", "plan_to_read")


@dataclass
class StatusCount:
    # watching, completed, on_hold, dropped, plan_to_watch (reading, plan_to_read for manga)
    status: str
    label: str
    count: int


@dataclass
class Stats:
    days: str = "0"
    mean_score: str = "0"
    statuses: list[StatusCount] = field(default_factory=list)
    # Total Entries, Rewatched, Episodes (Reread, Chapters, Volumes for manga)
    data: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Update:
    """A last list update: «Watching 5/12 · Scored 6», 59 minutes ago."""

    anime_id: int
    title: str
    status: str
    details: str
    ago: str


@dataclass
class Favorite:
    # anime, manga, character, person
    kind: str
    title: str
    # "TV·1998" for anime and manga, the title for characters
    details: str
    anime_id: int | None = None


@dataclass
class Friend:
    name: str
    last_online: str = ""
    since: str = ""


@dataclass
class Profile:
    name: str
    avatar: str = ""
    # Last Online, Gender, Birthday, Location, Joined: in the site order
    status: list[tuple[str, str]] = field(default_factory=list)
    about: str = ""
    anime: Stats = field(default_factory=Stats)
    manga: Stats = field(default_factory=Stats)
    updates: list[Update] = field(default_factory=list)
    favorites: list[Favorite] = field(default_factory=list)
    friends: list[Friend] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://myanimelist.net/profile/{quote(self.name)}"


def _text(node: HtmlElement | None) -> str:
    return " ".join(node.text_content().split()) if node is not None else ""


def _first(node: HtmlElement, xpath: str) -> HtmlElement | None:
    found = node.xpath(xpath)
    return found[0] if found else None


def _has_class(name: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _stats(block: HtmlElement | None) -> Stats:
    stats = Stats()
    if block is None:
        return stats
    for part in block.xpath(f".//div[{_has_class('stat-score')}]/div"):
        label, _, value = _text(part).partition(":")
        if label.strip() == "Days":
            stats.days = value.strip()
        elif label.strip() == "Mean Score":
            stats.mean_score = value.strip()
    for item in block.xpath(f".//ul[{_has_class('stats-status')}]/li"):
        link = _first(item, "./a")
        count = _first(item, "./span")
        if link is None:
            continue
        classes = (link.get("class") or "").split()
        status = next((c for c in classes if c in _STATUS_CLASSES), "")
        stats.statuses.append(StatusCount(status, _text(link), int(_text(count).replace(",", "") or 0)))
    for item in block.xpath(f".//ul[{_has_class('stats-data')}]/li"):
        spans = item.xpath("./span")
        if len(spans) >= 2:  # noqa: PLR2004
            stats.data.append((_text(spans[0]), _text(spans[1])))
    return stats


def _updates(doc: HtmlElement) -> list[Update]:
    updates = []
    for entry in doc.xpath(f"//div[{_has_class('updates')} and {_has_class('anime')}]//div[{_has_class('data')}]"):
        link = _first(entry, "./a")
        match = _ANIME_ID.search(link.get("href", "")) if link is not None else None
        if match is None:
            continue
        bar = _first(entry, f".//span[{_has_class('graph-inner')}]")
        classes = (bar.get("class") or "").split() if bar is not None else []
        updates.append(
            Update(
                anime_id=int(match.group(1)),
                title=_text(link),
                status=next((c for c in classes if c in _STATUS_CLASSES), ""),
                details=_text(_first(entry, "./div[last()]")),
                ago=_text(_first(entry, f".//span[{_has_class('fl-r')}]")),
            )
        )
    return updates


def _favorites(doc: HtmlElement) -> list[Favorite]:
    favorites = []
    for kind in ("anime", "manga", "character", "person"):
        for item in doc.xpath(f"//div[@id='{kind}_favorites']//li"):
            link = _first(item, "./a")
            href = link.get("href", "") if link is not None else ""
            match = _ANIME_ID.search(href) if kind == "anime" else None
            favorites.append(
                Favorite(
                    kind=kind,
                    title=_text(_first(item, f".//span[{_has_class('title')}]")) or item.get("title", ""),
                    details=_text(_first(item, f".//span[{_has_class('users')}]")),
                    anime_id=int(match.group(1)) if match else None,
                )
            )
    return favorites


def parse_profile(name: str, page: str) -> Profile:
    doc = html.fromstring(page)
    profile = Profile(name=name)
    avatar = _first(doc, f"//div[{_has_class('user-image')}]//img")
    if avatar is not None:
        profile.avatar = avatar.get("data-src") or avatar.get("src") or ""
    # the first status block: Last Online, Gender, Birthday, Location, Joined (not the links below it)
    for item in doc.xpath(f"(//ul[{_has_class('user-status')}])[1]/li[{_has_class('clearfix')}]"):
        title = _text(_first(item, f"./span[{_has_class('user-status-title')}]"))
        value = _text(_first(item, f"./span[{_has_class('user-status-data')}]"))
        if title and value:
            profile.status.append((title, value))
    about = _first(doc, f"//div[{_has_class('profile-about-user')}]")
    if about is not None:
        for br in about.xpath(".//br"):
            br.tail = "\n" + (br.tail or "")
        lines = (" ".join(line.split()) for line in about.text_content().splitlines())
        profile.about = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    profile.anime = _stats(_first(doc, f"//div[{_has_class('stats')} and {_has_class('anime')}]"))
    profile.manga = _stats(_first(doc, f"//div[{_has_class('stats')} and {_has_class('manga')}]"))
    profile.updates = _updates(doc)
    profile.favorites = _favorites(doc)
    return profile


def parse_friends(page: str) -> list[Friend]:
    doc = html.fromstring(page)
    friends = []
    for box in doc.xpath(f"//div[{_has_class('boxlist-container')} and {_has_class('friend')}]/div"):
        name = _text(_first(box, f".//div[{_has_class('title')}]/a"))
        if not name:
            continue
        details = [_text(d) for d in box.xpath(f".//div[{_has_class('fn-grey2')}]")]
        since = next((d for d in details if d.startswith("Friends since")), "")
        online = next((d for d in details if d != since), "")
        friends.append(Friend(name, online, since.removeprefix("Friends since").strip()))
    return friends


async def load(client: MalClient, name: str) -> Profile:
    """The profile with friends: both pages at once."""
    page, friends_page = await asyncio.gather(
        client.page(f"/profile/{quote(name)}"), client.page(f"/profile/{quote(name)}/friends")
    )
    profile = parse_profile(name, page)
    profile.friends = parse_friends(friends_page)
    return profile
