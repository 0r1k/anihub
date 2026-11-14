"""Ongoing list status: already airing, only announced or finished.

Every site reports it differently (or not at all):

- yummy-anime: data.episodes.aired == 0 for announced titles, data.episodes.next_date is the first episode date
- dreamcast: data.series is the aired episodes count
- animevost: "[1-3 из 12+]" in the title, "анонс" for announcements
- hdrezka: "2 сезон, 3 серия" or "Завершен (все серии)" in the title
- anitube: data.episodes_text "3 з 12 (24 хв.)" (0 aired: announced, all aired: finished), data.added for releases
- animego, anilibria, sameband: everything in the list is airing

A title without any marker is airing: the site lists it as ongoing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from rich.text import Text

from .i18n import t

AIRING = "airing"
ANNOUNCED = "announced"
FINISHED = "finished"
# filter values of the ongoing tab
ALL = "all"
FILTERS = (ALL, AIRING, ANNOUNCED)

_ANNOUNCED_TITLE = re.compile(r"\bанонс", re.IGNORECASE)
_FINISHED_TITLE = re.compile(r"\bзавершен", re.IGNORECASE)
# animevost: "[1-3 из 12+]", "[1 из 12]"
_VOST_EPISODES = re.compile(r"\[(?:\d+-)?(\d+) из")
# hdrezka: "2 сезон, 3 серия"
_REZKA_EPISODE = re.compile(r"(\d+) серия")
# anitube: "3 з 12 (24 хв.)", "4 з ХХ"
_UA_EPISODES = re.compile(r"^(\d+)\s*з\s*(\d+)?")
# a title updated less than this ago is marked as new
NEW_FOR = 7 * 24 * 3600


def _data(item: Any) -> dict[str, Any]:
    data = getattr(item, "data", None)
    return data if isinstance(data, dict) else {}


def status(item: Any) -> str:
    title = getattr(item, "title", "")
    if _ANNOUNCED_TITLE.search(title):
        return ANNOUNCED
    if _FINISHED_TITLE.search(title):
        return FINISHED
    data = _data(item)
    episodes = data.get("episodes")
    if isinstance(episodes, dict) and episodes.get("aired") == 0:
        return ANNOUNCED
    if data.get("series") == 0:
        return ANNOUNCED
    if isinstance(text := data.get("episodes_text"), str) and (found := _UA_EPISODES.match(text.strip())):
        aired, total = int(found.group(1)), found.group(2)
        if aired == 0:
            return ANNOUNCED
        if total and aired >= int(total):
            return FINISHED
    return AIRING


def mark(item: Any) -> str:
    """What changes in the list item when a new episode (or a new dub of it) comes out."""
    data = _data(item)
    episodes = data.get("episodes")
    if isinstance(episodes, dict) and "aired" in episodes:  # yummy-anime
        return f"ep:{episodes['aired']}"
    if isinstance(data.get("series"), int):  # dreamcast
        return f"ep:{data['series']}"
    if data.get("fresh_at"):  # anilibria: time of the last released episode
        return f"fresh:{data['fresh_at']}"
    if data.get("added"):  # anitube: "Додано 4 СУБ (Morys) / 4 СУБ 4 ОЗВ (Рідний Голос)"
        return f"added:{data['added']}"
    if (episode := getattr(item, "episode", None)) is not None:  # animego: an entry per episode and dub
        return f"ep:{episode}|{getattr(item, 'dub', '')}"
    title = getattr(item, "title", "")
    if found := _VOST_EPISODES.search(title) or _REZKA_EPISODE.search(title):
        return f"ep:{found.group(1)}"
    return f"str:{item}"


def ago(seconds: float) -> str:
    if seconds < 3600:
        return t("{count} min ago", count=max(1, int(seconds // 60)))
    if seconds < 24 * 3600:
        return t("{count} h ago", count=int(seconds // 3600))
    return t("{count} d ago", count=int(seconds // (24 * 3600)))


def premiere(item: Any) -> datetime | None:
    """The first episode date of an announced title, if the site knows it."""
    episodes = _data(item).get("episodes")
    timestamp = episodes.get("next_date") if isinstance(episodes, dict) else None
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()


def label(item: Any, updated_at: float = 0, now: float = 0) -> Text:
    """`updated_at`: when a new release of the title was noticed (0: never)."""
    text = Text()
    item_status = status(item)
    is_new = updated_at > 0 and now - updated_at < NEW_FOR
    if is_new:
        text.append(f" {t('NEW')} ", style="bold reverse")
        text.append(" ")
    if item_status == ANNOUNCED:
        text.append(f" {t('ANNOUNCED')} ", style="bold reverse")
        text.append(" ")
    text.append(str(item))
    if item_status == ANNOUNCED and (date := premiere(item)):
        day = date.strftime("%d.%m.%Y")
        # yummy-anime counts translated episodes: the premiere can be over while the site has none yet
        if date <= datetime.now(tz=timezone.utc):
            text.append(" · " + t("premiered {date}, no episodes on the site yet", date=day), style="dim")
        else:
            text.append(" · " + t("from {date}", date=day), style="dim")
    if is_new:
        text.append(" · " + t("new episode {ago}", ago=ago(now - updated_at)), style="dim")
    return text


def matches(item: Any, wanted: str) -> bool:
    return wanted == ALL or status(item) == wanted
