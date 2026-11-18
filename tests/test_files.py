"""JSON files of the user: round trips, broken files moved aside, failed writes leave the previous file."""

from __future__ import annotations

import json

import pytest

from anihub.common import history, mal_feed, mal_links, ongoing_state, storage
from anihub.common.config import get_data_dir
from anihub.tui.session import Settings

ENTRY = {"data": {"title": "Дюрарара!!"}, "episode": "1", "source": []}


@pytest.fixture(autouse=True)
def _clean_storage_state():
    storage.broken_files.clear()
    history._write_failed = False
    yield
    storage.broken_files.clear()


def _fail_replace(monkeypatch):
    def replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", replace)


def _broken_copies(path):
    return sorted(path.parent.glob(f"{path.stem}.broken-*{path.suffix}"))


def test_history_round_trip():
    history._write([ENTRY])
    assert history.read() == [ENTRY]
    history.update_last({"episode": "2"})
    assert history.read()[0]["episode"] == "2"
    history.clear()
    assert history.read() == []


def test_history_broken_file_is_moved_aside():
    path = history.get_history_path()
    path.write_text('[{"data": {"title": "Дюр', encoding="utf-8")
    assert history.read() == []
    [copy] = _broken_copies(path)
    assert copy.read_text(encoding="utf-8").startswith('[{"data"')
    assert storage.broken_files == [(path, copy)]
    # the next write starts a new file, the broken one stays
    history._write([ENTRY])
    assert history.read() == [ENTRY] and copy.exists()


def test_history_failed_write_keeps_file_and_does_not_raise(monkeypatch):
    history._write([ENTRY])
    path = history.get_history_path()
    before = path.read_text(encoding="utf-8")
    _fail_replace(monkeypatch)
    history._write([])  # must not raise: it is called while the player runs
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_ongoing_round_trip_and_broken_file():
    class Item:
        title = "Naruto"
        url = "https://example.org/naruto"

    marks = ongoing_state.track("animego", [Item()], lambda _i: "1")
    assert list(marks.values()) == [ongoing_state.Release(0.0, None)]
    path = ongoing_state._path()
    assert json.loads(path.read_text(encoding="utf-8"))["animego"]["titles"]

    path.write_text('{"animego": {"tit', encoding="utf-8")
    ongoing_state.track("animego", [Item()], lambda _i: "1")
    assert len(_broken_copies(path)) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["animego"]["titles"]


def test_ongoing_failed_write_keeps_file(monkeypatch):
    ongoing_state._write({"a": {}})
    path = ongoing_state._path()
    before = path.read_text(encoding="utf-8")
    _fail_replace(monkeypatch)
    ongoing_state._write({"b": {}})
    assert path.read_text(encoding="utf-8") == before


def test_mal_feed_round_trip():
    feed = mal_feed.Feed()
    feed.hidden.add(7)
    feed.related = {"1": [[2, "x"]]}
    feed.read_at = 123.0
    feed.save()
    again = mal_feed.Feed()
    assert again.hidden == {7} and again.related == {"1": [[2, "x"]]} and again.read_at == 123.0


@pytest.mark.parametrize("content", ['{"related": {"1"', "[1, 2]", '{"related": [], "details": {}}', '{"hidden": 5}'])
def test_mal_feed_broken_file_starts_over(content):
    path = mal_feed._path()
    path.write_text(content, encoding="utf-8")
    feed = mal_feed.Feed()
    assert feed.related == {} and feed.hidden == set()
    assert feed.read_at > 0
    if content.startswith(('{"related": {"1"', "[")):
        assert len(_broken_copies(path)) == 1  # not JSON of the right type: kept for the user


def test_mal_feed_failed_write_keeps_file(monkeypatch):
    feed = mal_feed.Feed()
    feed.save()
    path = mal_feed._path()
    before = path.read_text(encoding="utf-8")
    feed.hidden.add(1)
    _fail_replace(monkeypatch)
    feed.save()
    assert path.read_text(encoding="utf-8") == before


def test_mal_links_round_trip_and_limit():
    links = [{"mal_id": i, "data": {}} for i in range(mal_links.LINKS_LIMIT + 5)]
    mal_links._write(links)
    assert mal_links.read() == links[: mal_links.LINKS_LIMIT]


def test_mal_links_broken_file_is_moved_aside():
    path = mal_links._path()
    path.write_text("[{", encoding="utf-8")
    assert mal_links.read() == []
    assert len(_broken_copies(path)) == 1
    mal_links._write([{"mal_id": 1}])
    assert mal_links.read() == [{"mal_id": 1}]


def test_mal_links_failed_write_keeps_file(monkeypatch):
    mal_links._write([{"mal_id": 1}])
    path = mal_links._path()
    before = path.read_text(encoding="utf-8")
    _fail_replace(monkeypatch)
    mal_links._write([{"mal_id": 2}])
    assert path.read_text(encoding="utf-8") == before


def test_settings_round_trip():
    Settings(language="uk", quality=720, proxy="socks5://h:1", timeout=2.5).save()
    s = Settings.load()
    assert (s.language, s.quality, s.proxy, s.timeout) == ("uk", 720, "socks5://h:1", 2.5)


@pytest.mark.parametrize("content", ['{"quality": 72', "", "[1]", "null", b"\xff\xfe"])
def test_settings_broken_file_is_moved_aside(content):
    path = Settings.path()
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    assert Settings.load() == Settings()
    [copy] = _broken_copies(path)
    assert not path.exists() and storage.broken_files == [(path, copy)]
    Settings(quality=480).save()
    assert Settings.load().quality == 480 and copy.exists()


def test_settings_old_and_unknown_keys():
    Settings.path().write_text(
        json.dumps({"mpv_opts": "--fs", "future_key": 1, "quality": "high", "timeout": 30, "show_leaked": True}),
        encoding="utf-8",
    )
    s = Settings.load()
    assert s.player_args == "--fs"  # renamed key migrated
    assert s.quality == Settings().quality  # wrong type: default
    assert s.timeout == 30 and s.show_leaked is True
    assert not hasattr(s, "future_key")


def test_settings_overrides_win_and_none_is_ignored():
    Settings(quality=480).save()
    assert Settings.load(quality=360).quality == 360
    assert Settings.load(quality=None).quality == 480


def test_settings_failed_write_keeps_file_and_raises(monkeypatch):
    Settings(quality=480).save()
    _fail_replace(monkeypatch)
    with pytest.raises(OSError):
        Settings(quality=360).save()
    assert Settings.load().quality == 480


def test_files_are_in_the_temporary_data_dir(user_dirs):
    assert history.get_history_path().is_relative_to(user_dirs)
    assert mal_links._path().parent == get_data_dir()


def test_ongoing_releases_of_every_site():
    class Item:
        title = "Naruto"
        url = "https://example.org/naruto"

    ongoing_state.track("animego", [Item()], lambda _i: "1", now=100)
    assert ongoing_state.releases() == {}  # the first load only remembers
    ongoing_state.track("animego", [Item()], lambda _i: "2", now=200)
    assert ongoing_state.releases() == {("animego", Item.url): ongoing_state.Release(200.0, None)}


def test_history_remembers_when_the_title_was_watched():
    history._write([ENTRY])
    history.move_to_top(ENTRY)
    assert history.read()[0]["watched_at"] > 0
