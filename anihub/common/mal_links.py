"""Links between extractor titles and MyAnimeList entries, title matching and progress rules.

Extractors are russian sites, so the item title rarely matches MAL. Links chosen once (automatically
or by the user) are stored in the data dir and reused: also to open a MAL list entry in the extractor.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import attr

from anihub.common import storage
from anihub.common.config import get_data_dir
from anihub.common.history import entry_key, item_key, restore_item, serialize_item
from anihub.common.mal import ListStatus

if TYPE_CHECKING:
    from anicli_api.base import T_KW_HTTPS

    from anihub.common.mal import MalAnime

logger = logging.getLogger(__name__)
LINKS_LIMIT = 2000

# keys of extractor raw data with a MAL id or original/english titles
_MAL_ID_KEYS = ("myanimelist_id", "mal_id")
_TITLE_KEYS = (
    "alternateName",
    "original",
    "english",
    "name_english",
    "eng_name",
    "alternative",
    "alt_name",
    "other_names",
    "other_titles",
    "title_orig",
    "title_en",
    "romaji",
)


# --- storage -----------------------------------------------------------------


def _path() -> Path:
    return get_data_dir() / "mal_links.json"


def read() -> list[dict[str, Any]]:
    """The links, the latest first. A broken file is moved aside (see storage), not overwritten by the next link."""
    links: list[Any] = storage.read_json(_path(), [])
    return [link for link in links if isinstance(link, dict)]


def _write(links: list[dict[str, Any]]) -> None:
    # logged, not raised: links are saved from title pages and MAL workers, where a file error would crash the app.
    # The title is matched again the next time it is opened
    try:
        storage.write_json(_path(), links[:LINKS_LIMIT])
    except (OSError, TypeError, ValueError):
        logger.warning("failed to save MAL links", exc_info=True)


def _same_item(link: dict[str, Any], extractor_name: str, key: str) -> bool:
    return link.get("extractor_name") == extractor_name and entry_key(link) == key


def get(item: Any, extractor_name: str) -> dict[str, Any] | None:
    return get_by_key(extractor_name, item_key(item))


def get_by_key(extractor_name: str, key: str) -> dict[str, Any] | None:
    """The link of the title by its `item_key`."""
    return next((link for link in read() if _same_item(link, extractor_name, key)), None)


def save(item: Any, extractor_name: str, mal: MalAnime) -> dict[str, Any]:
    link = {**serialize_item(item, extractor_name), "mal_id": mal.id, "mal_title": mal.title}
    links = [i for i in read() if not _same_item(i, extractor_name, item_key(item))]
    _write([link, *links])
    return link


def delete(item: Any, extractor_name: str) -> None:
    _write([i for i in read() if not _same_item(i, extractor_name, item_key(item))])


def find_by_mal_id(mal_id: int, extractor_name: str | None = None) -> dict[str, Any] | None:
    """The latest link of the MAL entry, of the given extractor if it has one."""
    links = [i for i in read() if i.get("mal_id") == mal_id]
    if extractor_name:
        links.sort(key=lambda i: i.get("extractor_name") != extractor_name)
    return links[0] if links else None


def restore(link: dict[str, Any], http: T_KW_HTTPS | None = None) -> Any:
    return restore_item(link, http)


# --- matching ----------------------------------------------------------------


# how deep `_walk` looks into the nested data of an item
_WALK_DEPTH = 4


def _walk(value: Any, depth: int = 0):
    """Yield (key, value, parent dict) of nested dicts and lists."""
    if depth > _WALK_DEPTH:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v, value
            yield from _walk(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v, depth + 1)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    try:
        return {k: v for k, v in attr.asdict(obj).items() if not k.startswith("_")}
    except Exception:
        return {}


def hints(*objects: Any) -> tuple[int | None, list[str]]:
    """MAL id (if the site knows it) and original/english titles from extractor objects."""
    mal_id: int | None = None
    titles: list[str] = []

    def add(value: Any) -> None:
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if not isinstance(v, str):
                continue
            for part in (p.strip() for p in [v, *v.split(" / ")]):
                if part and part not in titles:
                    titles.append(part)

    for obj in objects:
        for key, value, parent in _walk(_as_dict(obj)):
            if key in _MAL_ID_KEYS and mal_id is None:
                try:
                    mal_id = int(value) or None
                except (TypeError, ValueError):
                    pass
            elif key in _TITLE_KEYS:
                add(value)
            # animelib: "name" is romaji next to "rus_name"
            elif key == "name" and "rus_name" in parent:
                add(value)
    for obj in objects:
        if isinstance(title := getattr(obj, "title", None), str):
            add(title)
    return mal_id, titles


def normalize(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.casefold())


def exact_match(candidates: list[MalAnime], titles: list[str]) -> MalAnime | None:
    """The candidate with a title equal (ignoring case and punctuation) to one of `titles`."""
    wanted = {normalize(t) for t in titles} - {""}
    for candidate in candidates:
        if wanted & {normalize(t) for t in candidate.all_titles}:
            return candidate
    return None


# --- progress ----------------------------------------------------------------


def watched_update(mal: MalAnime, episode: int) -> dict[str, Any]:
    """List status fields to send after the episode number `episode` was watched, empty if nothing changes.

    - plan to watch, on-hold, dropped or not in the list -> watching
    - the last episode -> completed
    - completed titles are not touched unless they are being rewatched
    """
    current = mal.list_status or ListStatus()
    total = mal.num_episodes
    # extractor numbering does not match this MAL entry (eg: continuous numbering of seasons)
    if episode < 1 or (total and episode > total):
        return {}
    if current.status == "completed" and not current.is_rewatching:
        return {}

    fields: dict[str, Any] = {}
    watched = max(current.num_episodes_watched, episode)
    if watched > current.num_episodes_watched:
        fields["num_watched_episodes"] = watched
    if total and watched >= total:
        fields["status"] = "completed"
        if current.is_rewatching:
            fields["is_rewatching"] = False
    elif current.status in (None, "plan_to_watch", "on_hold", "dropped"):
        fields["status"] = "watching"
    return fields
