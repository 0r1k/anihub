"""The ongoing list: airing, announced or finished by the markers of every site, what tells a new release."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from anihub.common import ongoing_state
from anihub.common.history import Watched
from anihub.common.ongoing_state import Release
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
        (item(data={"episodes": {"aired": 4}}), (4, False)),
        (item(data={"episodes": {"aired": 0}}), (0, False)),
        (item(data={"series": 7}), (7, False)),
        (item(data={"added": "Додано 4 СУБ (Morys)", "episodes_text": "4 з 12 (24 хв.)"}), (4, False)),
        (item(episode="5", dub="AniLibria"), (5, True)),
        (item("Синяя шкатулка [1-3 из 12+]"), (3, False)),
        (item("Синяя шкатулка [1 из 12]"), (1, False)),
        (item("Монолог фармацевта (2 сезон, 12 серия)"), (12, True)),
        (item(data={"fresh_at": 1700000000}), None),
        (item(data={"series": True}), None),
        (item("Без серий"), None),
    ],
)
def test_last_episode(value, expected):
    assert ongoing.last_episode(value) == expected


def test_a_new_dub_does_not_change_the_last_episode():
    assert ongoing.last_episode(item(episode="5", dub="A")) == ongoing.last_episode(item(episode="5", dub="B"))
    before = item(data={"added": "Додано 4 СУБ (Morys)", "episodes_text": "4 з 12"})
    after = item(data={"added": "Додано 4 СУБ (Morys) / 4 ОЗВ (Рідний Голос)", "episodes_text": "4 з 12"})
    assert ongoing.last_episode(before) == ongoing.last_episode(after)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (item(data={"fresh_at": 1700000000}), "fresh:1700000000"),
        (item(data={"added": "Додано 4 СУБ (Morys)"}), "added:Додано 4 СУБ (Morys)"),
        (item("Без серий"), "str:Без серий"),
    ],
)
def test_mark_of_a_site_without_numbers(value, expected):
    assert ongoing.mark(value) == expected


def test_is_new():
    now = 10 * ongoing.NEW_FOR
    release = Release(now - 3600, 7)
    assert ongoing.is_new(release, now)
    assert not ongoing.is_new(Release(now - ongoing.NEW_FOR - 1, 7), now)
    assert not ongoing.is_new(Release(0, 7), now) and not ongoing.is_new(None, now)
    # watched in anihub: by the episode numbers
    assert ongoing.is_new(release, now, Watched(6, now))
    assert not ongoing.is_new(release, now, Watched(7, 0))
    # no numbers: by the time it was watched; never watched before 8.4 is not known
    assert not ongoing.is_new(Release(now - 3600, None), now, Watched(None, now - 60))
    assert ongoing.is_new(Release(now - 3600, None), now, Watched(None, now - 7200))
    assert ongoing.is_new(Release(now - 3600, None), now, Watched(None, None))


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
    fresh = ongoing.label(item("Fresh"), Release(now - 7200, 3), now=now).plain
    assert "NEW" in fresh and "Fresh" in fresh and "2 h ago" in fresh
    assert "NEW" not in ongoing.label(item("Old"), Release(now - ongoing.NEW_FOR - 1, 3), now=now).plain
    assert "NEW" not in ongoing.label(item("Never"), None, now=now).plain
    assert "NEW" not in ongoing.label(item("Watched"), Release(now - 7200, 3), now, Watched(3, now)).plain


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


# --- releases of the ongoing list (ongoing.json) ------------------------------


def _track(*items: Item, now: float) -> dict:
    return ongoing_state.track("site", items, ongoing.mark, ongoing.last_episode, now=now)


def _ep(number: int, url: str = "https://site.example/1", dub: str = "A") -> Item:
    """An animego entry: one per episode."""
    return item(url=url, episode=str(number), dub=dub)


def _count(aired: int, url: str = "https://site.example/1") -> Item:
    """A yummy-anime entry: the count of the episodes out."""
    return item(url=url, data={"episodes": {"aired": aired}})


def test_a_new_episode_is_a_release():
    assert _track(_count(6), now=100)[_count(6).url] == Release(0, 6)  # the first load only remembers
    assert _track(_count(7), now=200)[_count(7).url] == Release(200, 7)
    assert _track(_count(7), now=300)[_count(7).url] == Release(200, 7)


def test_a_new_dub_is_not_a_release():
    anitube = item(url="https://site.example/2", data={"added": "Додано 4 СУБ", "episodes_text": "4 з 12"})
    _track(_ep(6, dub="A"), anitube, now=100)
    anitube.data = {**anitube.data, "added": "Додано 4 СУБ / 4 ОЗВ"}
    releases = _track(_ep(6, dub="A, B"), anitube, now=200)
    assert releases[_ep(6).url].updated_at == 0 and releases[anitube.url].updated_at == 0


def test_a_leaked_episode_is_not_a_release():
    _track(_ep(6), now=100)
    assert _track(_ep(12), now=200)[_ep(6).url] == Release(0, 6)
    # the next real episode is new, with the leak in the same list
    assert _track(_ep(12), _ep(7), now=300)[_ep(6).url] == Release(300, 7)


def test_a_count_growing_by_a_few_is_a_release():
    """A count has no gaps: three episodes out at once are not a leak."""
    _track(_count(6), now=100)
    assert _track(_count(9), now=200)[_count(9).url] == Release(200, 9)


def test_a_lower_count_is_not_a_release():
    _track(_count(7), now=100)
    _track(_count(6), now=200)  # the site lost an episode for a while
    assert _track(_count(7), now=300)[_count(7).url] == Release(0, 7)


def test_a_title_new_in_the_list_is_a_release():
    _track(_count(3), now=100)
    other = _count(1, url="https://site.example/new")
    assert _track(_count(3), other, now=200)[other.url] == Release(200, 1)
    announced = _count(0, url="https://site.example/announced")
    assert _track(announced, now=300)[announced.url].updated_at == 0


def test_a_title_known_before_episode_numbers_is_not_a_release():
    """The state of 8.3 remembered marks only: the first number is recorded, not a release."""
    ongoing_state._write({"site": {"titles": {_count(6).url: {"seen": ["ep:6"], "updated_at": 0, "last_seen": 50}}}})
    assert _track(_count(6), now=100)[_count(6).url] == Release(0, 6)
    assert _track(_count(7), now=200)[_count(7).url] == Release(200, 7)


def test_a_site_without_numbers_is_compared_by_marks():
    title = item(url="https://site.example/3", data={"fresh_at": 1})
    _track(title, now=100)
    title.data = {"fresh_at": 2}
    assert _track(title, now=200)[title.url] == Release(200, None)
