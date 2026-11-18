"""The Watching tab: episodes on the site, NEW marks only for real releases, the cache of the counts."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from anihub.common.config import get_data_dir
from anihub.tui import watching
from anihub.tui.watching import Entry

DAY = 24 * 3600


@pytest.fixture(autouse=True)
def clean_cache():
    watching._cache.clear()
    yield
    watching._cache.clear()


class Site:
    """A title of a site: `ordinals` are its episodes, `error` makes it fail."""

    def __init__(self, ordinals, url: str = "https://site.example/anime/1") -> None:
        self.ordinals = list(ordinals)
        self.url = url
        self.data: dict = {}
        self.error: Exception | None = None
        self.requests = 0

    async def a_get_anime(self):
        self.requests += 1
        if self.error:
            raise self.error
        return self

    async def a_get_episodes(self):
        return [SimpleNamespace(ordinal=n) for n in self.ordinals]


def entry(site: Site, *, watched: int = 0, airing_status: str = "currently_airing", mal_id: int = 1) -> Entry:
    anime = SimpleNamespace(
        id=mal_id,
        title=f"Title {mal_id}",
        airing_status=airing_status,
        num_episodes=12,
        list_status=SimpleNamespace(num_episodes_watched=watched),
    )
    link = {"extractor_name": "site", "data": {"title": anime.title}}
    return Entry(anime, link, site, checking=True)  # type: ignore[arg-type]


def load(site: Site, now: float, *, show_leaked: bool = False, force: bool = True, **kwargs) -> Entry:
    """What the tab does on every load: check the entry, then remember the releases."""
    result = entry(site, **kwargs)
    asyncio.run(watching.check(result, asyncio.Semaphore(1), force=force, show_leaked=show_leaked))
    watching.remember_releases([result], now=now)
    return result


def is_new(result: Entry, now: float) -> bool:
    return "NEW" in watching.label(result, now).plain


def test_the_counts_with_and_without_the_leaked_episodes():
    site = Site([1, 2, 3, 4, 5, 6, 12])  # 12 leaked before 7-11
    assert load(site, 0).available == 6
    assert load(site, 0, show_leaked=True).available == 12
    # nothing leaks once the title is finished on MyAnimeList
    assert load(site, 0, airing_status="finished_airing").available == 12


def test_a_new_episode_is_new():
    site = Site(range(1, 7))
    first = load(site, 1000, watched=6)
    assert first.updated_at == 0 and not first.unwatched
    site.ordinals.append(7)
    later = load(site, 2000, watched=6)
    assert later.updated_at == 2000 and later.unwatched == 1 and is_new(later, 2001)
    # remembered: the next load still shows when it came out
    assert load(site, 3000, watched=6).updated_at == 2000
    assert not is_new(later, 2000 + watching.ongoing.NEW_FOR + 1)


def test_the_first_count_is_not_new():
    """A title new in the list (or with a new link) has episodes, none of them is a release."""
    result = load(Site(range(1, 13)), 1000)
    assert result.unwatched == 12 and result.updated_at == 0 and not is_new(result, 1001)


def test_turning_on_the_leaked_episodes_is_not_a_release():
    site = Site([1, 2, 3, 4, 5, 6, 12])
    assert load(site, 1000, watched=6).available == 6
    result = load(site, 2000, watched=6, show_leaked=True)
    assert result.available == 12 and result.unwatched == 6
    assert result.updated_at == 0 and not is_new(result, 2001)
    # and turning it off again is not either
    assert load(site, 3000, watched=6).updated_at == 0


def test_a_title_that_failed_at_first_is_not_new_when_it_loads():
    site = Site(range(1, 7))
    site.error = ConnectionError("down")
    failed = load(site, 1000)
    assert failed.failed and failed.available is None and failed.updated_at == 0
    site.error = None
    loaded = load(site, 2000)
    assert loaded.available == 6 and loaded.updated_at == 0 and not is_new(loaded, 2001)


def test_a_failed_check_keeps_the_known_count():
    site = Site(range(1, 7))
    load(site, 1000)
    site.error = ConnectionError("down")
    load(site, 2000)
    site.error = None
    assert load(site, 3000).updated_at == 0  # the same 6 episodes


def test_an_episode_back_after_the_site_lost_it_is_not_new():
    site = Site(range(1, 8))
    load(site, 1000)
    site.ordinals.pop()
    assert load(site, 2000).updated_at == 0
    site.ordinals.append(7)
    assert load(site, 3000).updated_at == 0


def test_titles_not_checked_for_long_are_forgotten():
    load(Site(range(1, 3), url="https://site.example/old"), 0)
    load(Site(range(1, 3), url="https://site.example/new"), watching.FORGET_AFTER + 1)
    state = json.loads((get_data_dir() / "watching.json").read_text(encoding="utf-8"))
    assert [record["title"] for record in state["site"].values()] == ["Title 1"]
    assert all("new" in key for key in state["site"])


def test_a_broken_state_file_starts_over():
    path = get_data_dir() / "watching.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert load(Site(range(1, 3)), 1000).updated_at == 0
    assert json.loads(path.read_text(encoding="utf-8"))["site"]


def test_the_counts_are_cached():
    site = Site(range(1, 4))
    load(site, 0)
    site.ordinals.append(4)
    assert load(site, 0, force=False).available == 3 and site.requests == 1
    assert load(site, 0, force=True).available == 4 and site.requests == 2


def test_expired_counts_are_dropped_from_the_cache():
    expired = time.time() - watching.CACHE_TTL - 1
    watching._cache[("site", "https://site.example/gone")] = (expired, (5, 5))
    watching._cache[("site", "https://site.example/anime/1")] = (expired, (5, 5))
    site = Site(range(1, 4))
    # an expired count is not used, and the other expired ones are dropped on the next store
    assert load(site, 0, force=False).available == 3 and site.requests == 1
    assert list(watching._cache) == [("site", "https://site.example/anime/1")]


def test_a_clock_set_back_does_not_keep_counts_fresh():
    site = Site(range(1, 4))
    load(site, 0)
    (key,) = watching._cache
    watching._cache[key] = (time.time() + 3600, (3, 3))  # stored "in the future"
    site.ordinals.append(4)
    assert load(site, 0, force=False).available == 4 and site.requests == 2


def test_the_cache_uses_the_wall_clock(monkeypatch):
    """The monotonic clock does not advance while the computer sleeps: the counts would stay fresh."""
    site = Site(range(1, 4))
    load(site, 0)
    monkeypatch.setattr(watching.time, "monotonic", lambda: 0.0)
    real = time.time()
    monkeypatch.setattr(watching.time, "time", lambda: real + watching.CACHE_TTL + 1)  # slept overnight
    site.ordinals.append(4)
    assert load(site, 0, force=False).available == 4 and site.requests == 2


def test_ordered_new_first():
    old, fresh, caught_up = Site(range(1, 3), "u:old"), Site(range(1, 3), "u:fresh"), Site(range(1, 3), "u:up")
    entries = [load(caught_up, 0, watched=2, mal_id=3), load(old, 0, mal_id=1), load(fresh, 0, mal_id=2)]
    entries[1].updated_at, entries[2].updated_at = 10, 20
    assert [e.anime.id for e in watching.ordered(entries)] == [2, 1, 3]


def test_a_release_the_ongoing_list_noticed_is_new():
    """The ongoing list saw the episode come out: the first check of the title (nothing to compare) knows it."""
    site = Site(range(1, 8))
    result = entry(site, watched=6)
    result.updated_at = 1500  # `entries` takes it from ongoing_state.releases()
    asyncio.run(watching.check(result, asyncio.Semaphore(1), force=True))
    watching.remember_releases([result], now=2000)
    assert result.updated_at == 1500 and is_new(result, 2000)
    # nothing unwatched: not new, whatever the ongoing list saw
    result.anime.list_status.num_episodes_watched = 7
    assert not is_new(result, 2000)


def test_a_leaked_episode_is_never_new():
    """Shown with the setting on, counted as unwatched, but not a release: NEW waits for the real next episode."""
    site = Site(range(1, 7))
    load(site, 1000, watched=6, show_leaked=True)
    site.ordinals.append(12)
    leaked = load(site, 2000, watched=6, show_leaked=True)
    assert leaked.available == 12 and leaked.unwatched == 6
    assert leaked.updated_at == 0 and not is_new(leaked, 2001)
    site.ordinals.append(7)
    released = load(site, 3000, watched=6, show_leaked=True)
    assert released.updated_at == 3000 and is_new(released, 3001)
    # the same without the setting
    assert is_new(load(site, 3000, watched=6), 3001)


def test_a_finished_title_has_nothing_to_leak():
    site = Site(range(1, 7))
    load(site, 1000, watched=6, airing_status="finished_airing")
    site.ordinals.append(12)  # a gap in a finished title is a site that misses episodes, not a leak
    assert is_new(load(site, 2000, watched=6, airing_status="finished_airing"), 2001)
