"""Watch history, stored as a JSON list (newest first) in the user data dir."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import attr

from anicli.common.config import get_history_path
from anicli.common.extractors import dynamic_load_extractor_module

if TYPE_CHECKING:
    from anicli_api.base import T_KW_HTTPS, BaseSource

logger = logging.getLogger(__name__)
HISTORY_LIMIT = 50


def read() -> list[dict[str, Any]]:
    """Read raw history entries."""
    try:
        with get_history_path().open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _write(entries: list[dict[str, Any]]) -> None:
    with get_history_path().open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=4)


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
    _write([entry, *entries])


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


def episode_ordinal(entry: dict[str, Any], episode_title: str) -> int | None:
    """Episode number of the history entry episode, if it was saved."""
    for source in entry.get("source") or []:
        if source.get("episode_title") == episode_title:
            return source.get("ordinal")
    return None


def load_source(entry: dict[str, Any], episode_title: str, http: T_KW_HTTPS | None = None) -> BaseSource:
    """Restore source object of the episode from history entry."""
    sources: list[dict[str, Any]] = entry.get("source") or []
    if not sources:
        msg = "history entry has no saved sources"
        raise LookupError(msg)
    payload = next((s for s in sources if s["episode_title"] == episode_title), sources[-1])
    module, kwargs = _model_kwargs(entry["extractor_name"], payload, http)
    return module.Source(title=payload["title"], url=payload["url"], **kwargs)
