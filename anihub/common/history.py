"""Watch history, stored as a JSON list (newest first) in the user data dir."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple

import attr

from anihub.common import storage
from anihub.common.config import get_history_path
from anihub.common.extractors import dynamic_load_extractor_module
from anihub.common.sanitizer import sanitize_filename

if TYPE_CHECKING:
    from anicli_api.base import T_KW_HTTPS, BaseSource

logger = logging.getLogger(__name__)
HISTORY_LIMIT = 50
# the last write failed: the player saves the position every few seconds, one warning per failure is enough
_write_failed = False


def read() -> list[dict[str, Any]]:
    """Read raw history entries. A broken file is moved aside (see storage), not overwritten by the next write."""
    entries: list[Any] = storage.read_json(get_history_path(), [])
    return [entry for entry in entries if isinstance(entry, dict)]


def _write(entries: list[dict[str, Any]]) -> None:
    """Replace the file atomically: a crash or power loss while watching leaves the previous history.

    A failure (no space, no permission, a value of a site that is not JSON) is logged, not raised: the history
    must not stop the playback or crash a screen.
    """
    global _write_failed  # noqa: PLW0603
    try:
        storage.write_json(get_history_path(), entries)
    except (OSError, TypeError, ValueError):
        if not _write_failed:
            logger.warning("failed to save the history", exc_info=True)
        _write_failed = True
    else:
        _write_failed = False


def _public_fields(item: Any) -> dict[str, Any]:
    return {k: v for k, v in attr.asdict(item).items() if not k.startswith("_")}


def serialize_item(item: Any, extractor_name: str) -> dict[str, Any]:
    """Search/Ongoing object as a JSON-compatible dict, see `restore_item`."""
    data = _public_fields(item)
    return {
        "extractor_name": extractor_name,
        "type": item.__class__.__name__,
        "data": {
            "title": data.pop("title", "Unknown"),
            "thumbnail": data.pop("thumbnail", ""),
            "url": data.pop("url", ""),
            "data": data,
        },
    }


# sites without title pages (anilibria, animevost) give every title the url "_": the site id tells them apart
_NO_URL = ("", "_")


def _key(url: str, data: Any) -> str:
    if url not in _NO_URL or not isinstance(data, dict):
        return url
    value = data.get("id") or data.get("alias")
    return f"{url}#{value}" if value else url


def item_key(item: Any) -> str:
    """What tells the title apart from the others of its site: the url, or the id if the site gives no urls."""
    return _key(item.url, getattr(item, "data", None))


def entry_key(entry: dict[str, Any]) -> str:
    """`item_key` of a saved title (`serialize_item`)."""
    data = entry.get("data", {})
    return _key(data.get("url", ""), data.get("data", {}).get("data"))


def serialize_source(episode_title: str, source: BaseSource, ordinal: int | None = None) -> dict[str, Any]:
    data = _public_fields(source)
    return {
        "episode_title": episode_title,
        # episode number, used to track progress on MyAnimeList after resume
        "ordinal": ordinal,
        "title": data.pop("title", "Unknown"),
        "url": data.pop("url", ""),
        "data": data,
    }


def save(
    item: Any,
    extractor_name: str,
    source: dict[str, BaseSource] | None = None,
    episode: str = "Unknown",
    limit: int = HISTORY_LIMIT,
    ordinal: int | None = None,
) -> None:
    """Put item on top of the history (deduplicated by title)."""
    entry = {
        **serialize_item(item, extractor_name),
        "episode": episode,
        "time": None,
        # when the title was watched the last time: a release after it is NEW in the History tab
        "watched_at": time.time(),
        "source": [serialize_source(title, s, ordinal) for title, s in (source or {}).items()],
    }
    entries = [e for e in read() if e.get("data", {}).get("title") != entry["data"]["title"]]
    entries.insert(0, entry)
    _write(entries[:limit])


def update_last(element: dict[str, Any]) -> None:
    entries = read()
    if not entries:
        return
    entries[0].update(element)
    _write(entries)


def append_source(episode_title: str, source: BaseSource, ordinal: int | None = None) -> None:
    """Remember a source of the last (currently watched) entry for resume."""
    entries = read()
    if not entries:
        return
    entries[0].setdefault("source", []).append(serialize_source(episode_title, source, ordinal))
    _write(entries)


def move_to_top(entry: dict[str, Any]) -> None:
    """Make entry the last watched one (playback progress is written into the first entry)."""
    entries = [e for e in read() if e != entry]
    _write([{**entry, "watched_at": time.time()}, *entries])


def delete(entry: dict[str, Any]) -> None:
    _write([e for e in read() if e != entry])


def clear() -> None:
    _write([])


def _model_kwargs(extractor_name: str, payload: dict[str, Any], http: T_KW_HTTPS | None) -> tuple[Any, dict]:
    module = dynamic_load_extractor_module(extractor_name)
    if http is None:
        http = module.Extractor()._kwargs_http
    return module, {**payload.get("data", {}), **http}


def restore_item(entry: dict[str, Any], http: T_KW_HTTPS | None = None) -> Any:
    """Recreate the extractor object from `serialize_item` output (or a history entry)."""
    payload = entry["data"]
    module, kwargs = _model_kwargs(entry["extractor_name"], payload, http)
    model_cls = getattr(module, entry["type"])
    return model_cls(title=payload["title"], thumbnail=payload["thumbnail"], url=payload["url"], **kwargs)


def load(http: T_KW_HTTPS | None = None) -> list[tuple[dict[str, Any], Any]]:
    """Restore extractor objects from history.

    Returns pairs of (raw entry, restored object). Broken entries (eg: removed extractor) are skipped.
    """
    results = []
    for entry in read():
        try:
            obj = restore_item(entry, http)
        except Exception:
            logger.warning("skip broken history entry: %r", entry.get("data", {}).get("title"), exc_info=True)
            continue
        results.append((entry, obj))
    return results


def _source(entry: dict[str, Any], episode_title: str) -> dict[str, Any] | None:
    """The saved source of the episode.

    Before 8.3 the player wrote the episode title into the history sanitized as a file name ("2: Наследие" as
    "2- Наследие"), while the sources keep the title as it is: such entries are matched by the sanitized title.
    """
    sources = [s for s in entry.get("source") or [] if isinstance(s, dict)]
    found = next((s for s in sources if s.get("episode_title") == episode_title), None)
    if found is None:
        found = next((s for s in sources if sanitize_filename(s.get("episode_title") or "") == episode_title), None)
    return found


def episode_ordinal(entry: dict[str, Any], episode_title: str) -> int | None:
    """Episode number of the history entry episode, if it was saved."""
    source = _source(entry, episode_title)
    return source.get("ordinal") if source else None


class Watched(NamedTuple):
    # the number of the last episode watched (started), None: unknown (saved before 7.1)
    episode: int | None
    # when the title was watched the last time, None: unknown (saved before 8.4)
    at: float | None


def watched(entry: dict[str, Any]) -> Watched:
    ordinal = episode_ordinal(entry, entry.get("episode", ""))
    try:
        episode = int(ordinal) if ordinal is not None else None
    except (TypeError, ValueError):
        episode = None
    at = entry.get("watched_at")
    return Watched(episode, float(at) if isinstance(at, (int, float)) else None)


def watched_titles() -> dict[tuple[str, str], Watched]:
    """`watched` of the titles in the history by (extractor name, `item_key`)."""
    return {(entry.get("extractor_name", ""), entry_key(entry)): watched(entry) for entry in read()}


def load_source(entry: dict[str, Any], episode_title: str, http: T_KW_HTTPS | None = None) -> BaseSource:
    """Restore source object of the episode from history entry."""
    sources: list[dict[str, Any]] = entry.get("source") or []
    if not sources:
        msg = "history entry has no saved sources"
        raise LookupError(msg)
    payload = _source(entry, episode_title) or sources[-1]
    module, kwargs = _model_kwargs(entry["extractor_name"], payload, http)
    return module.Source(title=payload["title"], url=payload["url"], **kwargs)
