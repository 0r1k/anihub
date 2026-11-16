"""Episodes typed by the user: "1-12", "1 3 5-7", "12.5", "OVA"."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from .i18n import t

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# "1 - 3" and "1–3" are ranges too
_DASH = re.compile(r"\s*[-–—]\s*")


def _numeric(ordinal: object) -> float | None:
    try:
        number = float(str(ordinal).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def episode_number(ordinal: object) -> int | None:
    """The whole episode number (12 for 12 or "12"), None for "12.5", "OVA" and the like."""
    number = _numeric(ordinal)
    return int(number) if number is not None and number.is_integer() else None


def parse_selection_mask(s: str, ordinals: Sequence[object]) -> list[bool]:
    """Mask over the episode list of the episodes with the typed numbers (`ordinals`: their numbers in list order).

    Numbers are the episode numbers, not positions: "13-24" in a season numbered from 13. A range takes every
    numbered episode between its ends, 12.5 included; a number marks all episodes with it (duplicates); a missing
    number in a range (a gap) is skipped. Names like "OVA" match the episode number as text. Spaces or commas
    separate the parts. ValueError with a translated message: a part is not a number, or no episode has it.
    """
    numbers = [_numeric(o) for o in ordinals]
    names = [str(o).strip().casefold() for o in ordinals]
    mask = [False] * len(ordinals)
    bad: list[str] = []
    missing: list[str] = []
    for token in _DASH.sub("-", s).replace(",", " ").split():
        found = False
        start, sep, end = token.partition("-")
        if sep and _NUMBER.fullmatch(start) and _NUMBER.fullmatch(end):
            low, high = sorted((float(start), float(end)))
            for i, number in enumerate(numbers):
                if number is not None and low <= number <= high:
                    mask[i] = found = True
        elif _NUMBER.fullmatch(token):
            for i, number in enumerate(numbers):
                if number == float(token):
                    mask[i] = found = True
        else:
            for i, name in enumerate(names):
                if name == token.casefold():
                    mask[i] = found = True
            if not found:
                bad.append(token)
                continue
        if not found:
            missing.append(token)
    if bad:
        raise ValueError(t("Not an episode number: {parts}", parts=", ".join(bad)))
    if missing:
        raise ValueError(t("No such episodes in the list: {parts}", parts=", ".join(missing)))
    return mask
