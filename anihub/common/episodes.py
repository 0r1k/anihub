"""Leaked episodes: dubbed before their release, ahead of the episodes that are out (1-6 and 12).

The sites give no release dates, so a leak is seen by a gap in the numbering: a few episodes after the gap
are a leak, many are the next episodes of a site that misses one (1-3 and 5-8 is not a leak).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")


def _number(ordinal: Any) -> int | None:
    try:
        return int(ordinal)
    except (TypeError, ValueError):
        return None


def leaked_numbers(ordinals: Iterable[Any]) -> set[int]:
    """Episode numbers after a gap that are fewer than the missing numbers of the gap, from the last gap."""
    # episode 0 is a special: a season numbered from 13 with it is not a leak
    numbers = sorted({n for n in map(_number, ordinals) if n is not None and n > 0})
    leaked: set[int] = set()
    while len(numbers) > 1:
        cut = next((i for i in range(len(numbers) - 1, 0, -1) if numbers[i] - numbers[i - 1] > 1), None)
        if cut is None:
            break
        missing = numbers[cut] - numbers[cut - 1] - 1
        if len(numbers) - cut >= missing:
            break
        leaked.update(numbers[cut:])
        numbers = numbers[:cut]
    return leaked


def split_leaked(episodes: Sequence[T], key: Any = lambda e: e.ordinal) -> tuple[list[T], list[T]]:
    """(released, leaked) in the site order."""
    leaked = leaked_numbers(key(e) for e in episodes)
    released = [e for e in episodes if _number(key(e)) not in leaked]
    return released, [e for e in episodes if _number(key(e)) in leaked]
