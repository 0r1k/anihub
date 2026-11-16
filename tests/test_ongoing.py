"""The ongoing list: airing, announced or finished by the markers of every site, what tells a new release."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from anihub.tui import ongoing
from anihub.tui.ongoing import AIRING, ANNOUNCED, FINISHED


class Item(SimpleNamespace):
    def __str__(self) -> str:
        return self.title


def item(title: str = "Title", data: object = None, **kwargs: object) -> Item:
    return Item(title=title, data={} if data is None else data, **kwargs)


@pytest.mark.parametrize(
    ("title", "data", "expected"),
    [
        # animevost, hdrezka: the title
        ("Синяя шкатулка [1-3 из 12+]", {}, AIRING),
        ("Синяя шкатулка [Анонс]", {}, ANNOUNCED),
        ("Магическая битва (анонс)", {}, ANNOUNCED),
        ("Монолог фармацевта (2 сезон, 12 серия)", {}, AIRING),
        ("Монолог фармацевта (Завершен (все серии))", {}, FINISHED),
        # yummy-anime
        ("Title", {"episodes": {"aired": 0, "next_date": 1}}, ANNOUNCED),
        ("Title", {"episodes": {"aired": 3}}, AIRING),
        # dreamcast
        ("Title", {"series": 0}, ANNOUNCED),
        ("Title", {"series": 5}, AIRING),
        # anitube
        ("Title", {"episodes_text": "0 з 12 (24 хв.)"}, ANNOUNCED),
        ("Title", {"episodes_text": "3 з 12 (24 хв.)"}, AIRING),
        ("Title", {"episodes_text": "12 з 12 (24 хв.)"}, FINISHED),
        ("Title", {"episodes_text": " 4 з ХХ"}, AIRING),
        ("Title", {"episodes_text": "невідомо"}, AIRING),
        # no markers: the site lists it as ongoing
        ("Title", {}, AIRING),
        ("Title", None, AIRING),
        # a word that only contains the marker is not one
        ("Канонсы", {}, AIRING),
    ],
)
def test_status(title, data, expected):
    assert ongoing.status(item(title, data)) == expected


def test_status_of_an_item_with_odd_data():
    assert ongoing.status(Item(title="Title", data="not a dict")) == AIRING
    assert ongoing.status(SimpleNamespace()) == AIRING
    assert ongoing.status(item(data={"episodes": [1, 2]})) == AIRING


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (item(data={"episodes": {"aired": 4}}), "ep:4"),
        (item(data={"series": 7}), "ep:7"),
        (item(data={"fresh_at": 1700000000}), "fresh:1700000000"),
        (item(data={"added": "Додано 4 СУБ (Morys)"}), "added:Додано 4 СУБ (Morys)"),
        (item(episode=5, dub="AniLibria"), "ep:5|AniLibria"),
        (item("Синяя шкатулка [1-3 из 12+]"), "ep:3"),
        (item("Синяя шкатулка [1 из 12]"), "ep:1"),
        (item("Монолог фармацевта (2 сезон, 12 серия)"), "ep:12"),
        (item("Без серий"), "str:Без серий"),
    ],
)
def test_mark(value, expected):
    assert ongoing.mark(value) == expected


def test_the_mark_changes_with_a_new_episode_only():
    before, after = item("Title [1-3 из 12+]"), item("Title [1-4 из 12+]")
    assert ongoing.mark(before) != ongoing.mark(after)
    assert ongoing.mark(item("Other [1-3 из 12+]")) == ongoing.mark(before)
    # a new dub of the same episode is a release on animego
    assert ongoing.mark(item(episode=5, dub="A")) != ongoing.mark(item(episode=5, dub="B"))


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "1 min ago"),
        (59, "1 min ago"),
        (600, "10 min ago"),
        (3600, "1 h ago"),
        (5 * 3600, "5 h ago"),
        (3 * 86400, "3 d ago"),
    ],
)
def test_ago(seconds, expected):
    assert ongoing.ago(seconds) == expected


def test_premiere():
    later = datetime.now(tz=timezone.utc) + timedelta(days=3)
    date = ongoing.premiere(item(data={"episodes": {"aired": 0, "next_date": later.timestamp()}}))
    assert date is not None and abs(date.timestamp() - later.timestamp()) < 1
    for data in ({}, {"episodes": {"next_date": 0}}, {"episodes": {"next_date": "soon"}}, {"episodes": 3}):
        assert ongoing.premiere(item(data=data)) is None


def test_label():
    now = 10 * ongoing.NEW_FOR
    fresh = ongoing.label(item("Fresh"), updated_at=now - 7200, now=now).plain
    assert "NEW" in fresh and "Fresh" in fresh and "2 h ago" in fresh
    assert "NEW" not in ongoing.label(item("Old"), updated_at=now - ongoing.NEW_FOR - 1, now=now).plain
    assert "NEW" not in ongoing.label(item("Never"), updated_at=0, now=now).plain


def test_label_of_an_announcement():
    future = datetime.now(tz=timezone.utc) + timedelta(days=30)
    past = datetime.now(tz=timezone.utc) - timedelta(days=2)
    soon = ongoing.label(item(data={"episodes": {"aired": 0, "next_date": future.timestamp()}})).plain
    assert "ANNOUNCED" in soon and future.astimezone().strftime("%d.%m.%Y") in soon
    over = ongoing.label(item(data={"episodes": {"aired": 0, "next_date": past.timestamp()}})).plain
    assert "no episodes on the site yet" in over


def test_matches():
    announced, airing = item("Title [Анонс]"), item("Title [1-3 из 12+]")
    assert ongoing.matches(announced, ongoing.ALL) and ongoing.matches(airing, ongoing.ALL)
    assert ongoing.matches(announced, ANNOUNCED) and not ongoing.matches(airing, ANNOUNCED)
    assert ongoing.matches(airing, AIRING) and not ongoing.matches(announced, AIRING)
