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

from anihub.common.history import Watched
from anihub.common.ongoing_state import Release

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


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def last_episode(item: Any) -> tuple[int, bool] | None:
    """(number, single) of the last episode out, None: the list of the site has no numbers.

    single: the number of one episode (a leaked one can come after a gap), otherwise the count of the episodes
    out, which has no gaps. A new dub or subtitles of an episode do not change it.
    """
    data = _data(item)
    episodes = data.get("episodes")
    if isinstance(episodes, dict) and (aired := _int(episodes.get("aired"))) is not None:  # yummy-anime
        return aired, False
    if (series := _int(data.get("series"))) is not None:  # dreamcast
        return series, False
    # anitube: "3 з 12 (24 хв.)", the "added" text changes with every dub
    if isinstance(text := data.get("episodes_text"), str) and (found := _UA_EPISODES.match(text.strip())):
        return int(found.group(1)), False
    # animego: an entry per episode, its dubs in one
    if (episode := _int(getattr(item, "episode", None))) is not None:
        return episode, True
    title = getattr(item, "title", "")
    if found := _VOST_EPISODES.search(title):  # animevost: "[1-3 из 12+]"
        return int(found.group(1)), False
    if found := _REZKA_EPISODE.search(title):  # hdrezka: "2 сезон, 3 серия"
        return int(found.group(1)), True
    return None


def mark(item: Any) -> str:
    """What changes in the list item of a site without episode numbers (see `last_episode`) on a release."""
    data = _data(item)
    if data.get("fresh_at"):  # anilibria: time of the last released episode
        return f"fresh:{data['fresh_at']}"
    if data.get("added"):  # anitube without the episodes text: "Додано 4 СУБ (Morys)"
        return f"added:{data['added']}"
    return f"str:{item}"


def is_new(release: Release | None, now: float, watched: Watched | None = None) -> bool:
    """A new episode came out lately and it was not watched in anihub (`watched`, None: never watched)."""
    if release is None or not release.updated_at or now - release.updated_at >= NEW_FOR:
        return False
    if watched is None:
        return True
    if release.episode is not None and watched.episode is not None:
        return release.episode > watched.episode
    # the episode numbers are unknown: new if it came out after the title was watched
    return watched.at is None or watched.at < release.updated_at


_HOUR = 3600
_DAY = 24 * _HOUR


def ago(seconds: float) -> str:
    if seconds < _HOUR:
        return t("{count} min ago", count=max(1, int(seconds // 60)))
    if seconds < _DAY:
        return t("{count} h ago", count=int(seconds // _HOUR))
    return t("{count} d ago", count=int(seconds // _DAY))


def premiere(item: Any) -> datetime | None:
    """The first episode date of an announced title, if the site knows it."""
    episodes = _data(item).get("episodes")
    timestamp = episodes.get("next_date") if isinstance(episodes, dict) else None
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()


def label(item: Any, release: Release | None = None, now: float = 0, watched: Watched | None = None) -> Text:
    """`release`: the last new episode of the title, `watched`: the title in the history of anihub."""
    text = Text()
    item_status = status(item)
    new = is_new(release, now, watched)
    if new:
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
    if new and release is not None:
        text.append(" · " + t("new episode {ago}", ago=ago(now - release.updated_at)), style="dim")
    return text


def matches(item: Any, wanted: str) -> bool:
    return wanted == ALL or status(item) == wanted
