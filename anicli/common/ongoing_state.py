"""Ongoing list history: when a title got a new episode (or a new dub/subtitles of it).

Sites return the ongoing list in their own order. Every load the progress marks of each title are
compared with the saved ones: a mark never seen before means a new release, the title gets `updated_at = now`
and goes to the top. The first load of a site only records the state (nothing is new yet).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from anicli.common.config import get_data_dir

logger = logging.getLogger(__name__)
# marks remembered per title: enough for a season of episodes in several dubs
SEEN_LIMIT = 100
# forget titles that left the ongoing list long ago
FORGET_AFTER = 90 * 24 * 3600


def _path() -> Path:
    return get_data_dir() / "ongoing.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("broken ongoing state %s, start over", _path(), exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    try:
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("failed to save ongoing state", exc_info=True)


def track(
    extractor_name: str, items: Iterable[Any], mark: Callable[[Any], str], now: float | None = None
) -> dict[str, float]:
    """Update the saved state with the loaded list. Returns `updated_at` by item url (0: no new release seen)."""
    now = time.time() if now is None else now
    data = _read()
    site = data.get(extractor_name)
    first_load = not isinstance(site, dict)
    titles: dict[str, Any] = {} if first_load else site.get("titles", {})  # type: ignore[union-attr]

    for item in items:
        entry = titles.setdefault(item.url, {"seen": [], "updated_at": 0.0})
        entry["title"] = item.title
        entry["last_seen"] = now
        item_mark = mark(item)
        if item_mark not in entry["seen"]:
            entry["seen"] = [*entry["seen"], item_mark][-SEEN_LIMIT:]
            # a new title in the list counts too: sites put titles there when an episode comes out
            if not first_load:
                entry["updated_at"] = now

    titles = {url: e for url, e in titles.items() if now - e.get("last_seen", now) < FORGET_AFTER}
    data[extractor_name] = {"titles": titles}
    _write(data)
    return {url: float(e.get("updated_at") or 0) for url, e in titles.items()}
