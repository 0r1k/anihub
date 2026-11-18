"""Ongoing list history: when a title got a new episode.

Sites return the ongoing list in their own order. Every load the last episode of each title is compared with
the saved one: a higher number means a new episode, the title gets `updated_at = now` and goes to the top.
A new dub or subtitles of an episode that is out already are not a release, a leaked episode (dubbed before
its release, after a gap in the numbering) is not either. The first load of a site only records the state.

A site that gives no episode numbers in its list (anilibria, anilibme, sameband) is compared by a progress
mark: a mark never seen before is a release.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from anihub.common import storage
from anihub.common.config import get_data_dir
from anihub.common.episodes import leaked_numbers
from anihub.common.history import item_key

logger = logging.getLogger(__name__)
# (number, single) of the last episode of a title in the list: see anihub.tui.ongoing.last_episode
LastEpisode = Callable[[Any], "tuple[int, bool] | None"]
# marks remembered per title: enough for a season of episodes in several dubs
SEEN_LIMIT = 100
# forget titles that left the ongoing list long ago
FORGET_AFTER = 90 * 24 * 3600


def _path() -> Path:
    return get_data_dir() / "ongoing.json"


def _read() -> dict[str, Any]:
    # a broken file is moved aside (see storage): starting over only means no NEW marks on this load
    return storage.read_json(_path(), {})


def _write(data: dict[str, Any]) -> None:
    # logged, not raised: the ongoing list is shown anyway, the marks are only not remembered
    try:
        storage.write_json(_path(), data, indent=2)
    except OSError:
        logger.warning("failed to save ongoing state", exc_info=True)


class Release(NamedTuple):
    # when the last new episode came out (0: no release seen)
    updated_at: float
    # the number of the last episode out, None: the site gives no numbers
    episode: int | None


def _release(entry: dict[str, Any]) -> Release:
    updated_at, episode = entry.get("updated_at"), entry.get("episode")
    return Release(
        float(updated_at) if isinstance(updated_at, (int, float)) else 0.0,
        episode if isinstance(episode, int) else None,
    )


def _number_of(item: Any, last_episode: LastEpisode) -> int:
    found = last_episode(item)
    return found[0] if found else 0


def track(
    extractor_name: str,
    items: Iterable[Any],
    mark: Callable[[Any], str],
    last_episode: LastEpisode = lambda _item: None,
    now: float | None = None,
) -> dict[str, Release]:
    """Update the saved state with the loaded list. Returns the releases by `item_key`."""
    now = time.time() if now is None else now
    data = _read()
    site = data.get(extractor_name)
    first_load = not isinstance(site, dict)
    titles: dict[str, Any] = {} if first_load else site.get("titles", {})  # type: ignore[union-attr]

    # animego lists every episode apart: the lower ones first, so a leak after them is seen as one
    for item in sorted(items, key=lambda i: _number_of(i, last_episode)):
        key = item_key(item)
        is_new_title = key not in titles
        entry = titles.setdefault(key, {"seen": [], "updated_at": 0.0})
        entry["title"] = item.title
        entry["last_seen"] = now
        found = last_episode(item)
        if found is None:
            item_mark = mark(item)
            if item_mark not in entry["seen"]:
                entry["seen"] = [*entry["seen"], item_mark][-SEEN_LIMIT:]
                # a new title in the list counts too: sites put titles there when an episode comes out
                if not first_load:
                    entry["updated_at"] = now
            continue
        number, single = found
        known = entry.get("episode")
        if not isinstance(known, int):
            entry["episode"] = number
            # a title new in the list: an episode came out. A title known before (state of 8.3 and older,
            # which remembered marks only) is only recorded
            if is_new_title and not first_load and number > 0:
                entry["updated_at"] = now
        elif number > known:
            # a single episode far after the known one is a leak: not remembered, the next real episode is new.
            # A count of the episodes has no gaps (a few episodes out at once are not a leak)
            if single and number in leaked_numbers((known, number)):
                continue
            entry["episode"] = number
            entry["updated_at"] = now
        # a lower or the same number: another dub of an episode that is out, or a site that lost one for a while

    titles = {url: e for url, e in titles.items() if now - e.get("last_seen", now) < FORGET_AFTER}
    data[extractor_name] = {"titles": titles}
    # counts of the Watching tab were here before 8.3: they live in watching.json now
    data.pop("@watching", None)
    _write(data)
    return {url: _release(e) for url, e in titles.items()}


def releases() -> dict[tuple[str, str], Release]:
    """The releases of the titles of every site by (extractor name, `item_key`). The state is not changed.

    The Watching and History tabs mark their titles NEW with it: the ongoing list notices releases first.
    """
    result: dict[tuple[str, str], Release] = {}
    for name, site in _read().items():
        titles = site.get("titles") if isinstance(site, dict) else None
        if not isinstance(titles, dict):
            continue
        for key, entry in titles.items():
            if isinstance(entry, dict) and (release := _release(entry)).updated_at > 0:
                result[name, key] = release
    return result
