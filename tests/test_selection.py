"""Episodes typed in the range input are matched by their numbers, not by their positions in the list."""

from __future__ import annotations

import pytest

from anihub.tui.selection import episode_number, parse_selection_mask


def marked(s: str, ordinals: list[object]) -> list[object]:
    return [o for o, m in zip(ordinals, parse_selection_mask(s, ordinals), strict=True) if m]


def test_numbers_from_one():
    ordinals = list(range(1, 11))
    assert marked("1-3 5", ordinals) == [1, 2, 3, 5]
    assert marked("1-3 2 5", ordinals) == [1, 2, 3, 5]
    assert marked("", ordinals) == []


def test_season_numbered_from_13():
    ordinals = list(range(13, 25))
    assert marked("13-24", ordinals) == ordinals
    assert marked("13 15", ordinals) == [13, 15]
    with pytest.raises(ValueError, match="1-3"):
        parse_selection_mask("1-3", ordinals)


def test_episode_zero_does_not_shift_the_numbers():
    ordinals = [0, 1, 2, 3]
    assert marked("1-2", ordinals) == [1, 2]
    assert marked("0", ordinals) == [0]


def test_gaps_duplicates_and_special_numbers():
    ordinals: list[object] = [1, 2, 4, 5, "5", 12.5, "OVA", "SP-1"]
    # a range skips the missing number and takes 12.5 between its ends
    assert marked("1-13", ordinals) == [1, 2, 4, 5, "5", 12.5]
    # the same number twice (two seasons in one list): both are marked
    assert marked("5", ordinals) == [5, "5"]
    assert marked("12.5", ordinals) == [12.5]
    assert marked("ova sp-1", ordinals) == ["OVA", "SP-1"]


def test_separators_and_reversed_range():
    ordinals = list(range(1, 8))
    assert marked("1,3, 5 - 6", ordinals) == [1, 3, 5, 6]
    assert marked("3–1", ordinals) == [1, 2, 3]
    assert marked("7-5", ordinals) == [5, 6, 7]


def test_errors_list_every_wrong_part():
    ordinals = list(range(1, 4))
    with pytest.raises(ValueError, match="Not an episode number: abc, 1-x"):
        parse_selection_mask("1 abc 1-x", ordinals)
    with pytest.raises(ValueError, match="No such episodes in the list: 7, 10-12"):
        parse_selection_mask("1 7 10-12", ordinals)


def test_episode_number():
    assert episode_number(12) == 12
    assert episode_number("12") == 12
    assert episode_number(12.0) == 12
    assert episode_number(12.5) is None
    assert episode_number("OVA") is None
    assert episode_number(None) is None
    assert episode_number("nan") is None
