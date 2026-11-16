"""JSON files of anihub: settings, MAL token, history, MAL links and caches.

A file is replaced atomically: a crash, a closed terminal or a power loss in the middle of a write leaves
the previous file, never a truncated one. A broken file (not JSON, or JSON of another type) is moved aside
as `<name>.broken-<time>.json` with a warning instead of being read as empty and overwritten by the next
write: nothing is lost silently, the user can restore it by hand.
"""

from __future__ import annotations

import contextlib
import glob
import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

# Windows: replacing fails while another process (an antivirus, a second anihub) has the file open
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY = 0.05
# new files get the usual mode: mkstemp creates them readable by the owner only
_UMASK = os.umask(0)
os.umask(_UMASK)

# files moved aside since the start, as (broken file, where it was moved): the TUI tells the user
broken_files: list[tuple[Path, Path]] = []
# files that were found broken by a read-only `read_json` (the CLI): left in place for the TUI to set aside
unusable_files: list[Path] = []
# files that exist but could not be read (no permission, a lock): `write_json` must not replace them with data
# built from the empty default, until a read succeeds
_unreadable: set[Path] = set()
# Windows: an antivirus or a second anihub can hold the file for a moment
_READ_ATTEMPTS = 4
_STALE_TMP_AGE = 3600


def _real(path: Path) -> Path:
    """The file behind symlinks (a dotfiles repo links settings.json): write there, keep the link."""
    return Path(os.path.realpath(path))


def write_json(path: Path, data: Any, *, indent: int | None = 4, private: bool = False) -> None:
    """Write `data` to `path` atomically. `private`: readable by the owner only (tokens).

    Raises TypeError for data that is not JSON and OSError (no space, no permission, a file that could not
    be read before): callers decide whether a failed save matters.
    """
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    target = _real(path)
    if target in _unreadable:
        msg = f"{path} could not be read, not replacing it with new data"
        raise OSError(msg)
    _remove_stale_tmp(target)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if not private:
            os.chmod(tmp, _mode(target))
        _replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _remove_stale_tmp(target: Path) -> None:
    """Delete temp files that a crashed write left. A fresh one may belong to another running anihub."""
    limit = time.time() - _STALE_TMP_AGE
    with contextlib.suppress(OSError):
        for tmp in target.parent.glob(f".{glob.escape(target.name)}.*.tmp"):
            with contextlib.suppress(OSError):
                if tmp.stat().st_mtime < limit:
                    tmp.unlink()


def read_json(path: Path, default: T, *, set_aside: bool = True) -> T:
    """The data of `path`, `default` if there is no file.

    A file that is not JSON of the type of `default` (a list, a dict) is moved aside, `default` is returned.
    `set_aside=False` (the CLI): the file stays where it is and is listed in `unusable_files` instead, so that
    the TUI, which tells the user, finds it.
    An unreadable file (no permission) is left as is: `default` with a warning, and `write_json` refuses to
    replace it until it can be read.
    """
    real = _real(path)
    try:
        text = _read_text(path)
    except FileNotFoundError:
        _unreadable.discard(real)
        return default
    except OSError:
        _unreadable.add(real)
        logger.warning("cannot read %s", path, exc_info=True)
        return default
    _unreadable.discard(real)
    try:
        data = json.loads(text)
    except ValueError:  # truncated or edited by hand
        data = None
    if not isinstance(data, type(default)):
        if set_aside:
            _set_aside(path)
        else:
            if path not in unusable_files:
                unusable_files.append(path)
            logger.warning("broken file %s, left in place", path)
        return default
    return data


def _read_text(path: Path) -> str:
    for attempt in range(_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "\0"  # not JSON: handled like any broken file
        except PermissionError:
            # Windows reports a sharing violation or a delete-pending file this way
            if os.name != "nt" or attempt == _READ_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)
    raise AssertionError  # pragma: no cover


def _mode(path: Path) -> int:
    """The mode of the replaced file, the usual one for a new file."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return 0o666 & ~_UMASK


def _replace(source: str, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)


def set_aside(path: Path) -> None:
    """Keep a file that cannot be used next to itself and remember it for the user, see `broken_files`."""
    moved = path.with_name(f"{path.stem}.broken-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    try:
        os.replace(path, moved)
    except OSError:
        logger.warning("broken file %s, could not move it aside", path, exc_info=True)
        return
    broken_files.append((path, moved))
    logger.warning("broken file %s moved to %s, starting over", path, moved.name)


# `read_json` has a parameter of the same name
_set_aside = set_aside
