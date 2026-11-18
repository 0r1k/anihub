"""The TUI headless: problems shown to the user, the footer language, MAL tracking, the search tab. No network."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

import attr
import pytest
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer

from anihub.common import mal_links, storage, updater
from anihub.common.history import item_key
from anihub.common.mal import MalAnime
from anihub.tui import AnihubApp, Session, Settings, i18n, ongoing
from anihub.tui import mal as tui_mal
from anihub.tui.screens.home import HomeScreen, _history_label, _MalHint


def run(coro, timeout: float = 60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@attr.s(auto_attribs=True)
class Item:
    title: str
    url: str
    thumbnail: str = ""

    def __str__(self) -> str:
        return self.title


class FakeExtractor:
    """Search results that come when `gate` is set."""

    def __init__(self, *titles: str) -> None:
        self.items = [Item(title, f"https://site/{title}") for title in titles]
        self.gate = asyncio.Event()
        self.gate.set()

    async def a_search(self, query: str) -> list[Item]:
        await self.gate.wait()
        return list(self.items)

    async def a_ongoing(self) -> list[Item]:
        return []


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No update check, no files broken by other tests, the language back to English."""

    async def up_to_date(proxy: str | None = None) -> dict[str, Any]:
        return {"anihub": {"is_outdated": False}, "anicli_api": {"is_outdated": False}}

    monkeypatch.setattr(updater, "check_for_updates", up_to_date)
    monkeypatch.setattr(storage, "broken_files", [])
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


def _app(*titles: str, language: str = "en") -> tuple[AnihubApp, list[tuple[str, dict[str, Any]]]]:
    """The app with a fake site and its notifications recorded."""
    session = Session(Settings(language=language, posters=False))
    session.extractor = FakeExtractor(*titles)  # type: ignore[assignment]
    app = AnihubApp(session)
    notes: list[tuple[str, dict[str, Any]]] = []
    notify = app.notify

    def record(message: str, **kwargs: Any) -> None:
        notes.append((str(message), kwargs))
        notify(message, **kwargs)

    app.notify = record  # type: ignore[method-assign]
    return app, notes


def _messages(notes: list[tuple[str, dict[str, Any]]]) -> str:
    return "\n".join(message for message, _ in notes)


# --- problems shown to the user ----------------------------------------------


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        ('mpv "--title=unclosed', "Wrong player arguments: No closing quotation"),
        ("no-such-player-anihub --fs", "Player not found: no-such-player-anihub --fs"),
    ],
)
def test_check_player_tells_wrong_arguments_from_a_missing_player(command, shown):
    """An unclosed quote in a custom command is not a missing player: the advice differs."""

    async def check() -> None:
        app, notes = _app()
        app.session.settings.player, app.session.settings.player_command = "custom", command
        async with app.run_test() as pilot:
            await pilot.pause()
        assert shown in _messages(notes)

    run(check())


def test_broken_files_are_shown_once(user_dirs):
    async def check() -> None:
        broken = user_dirs / "history.json"
        broken.write_text("[{", encoding="utf-8")
        storage.read_json(broken, [])
        app, notes = _app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert _messages(notes).count("history.json") == 1
            # polled while the app runs (the interval is not waited for: that would depend on timing)
            assert any(timer._callback == app.show_broken_files for timer in app._timers)
            # found broken while the app runs
            later = user_dirs / "mal_links.json"
            later.write_text("{", encoding="utf-8")
            storage.read_json(later, [])
            app.show_broken_files()
            text = _messages(notes)
            assert text.count("history.json") == 1 and text.count("mal_links.json") == 1, text
            assert all(kw.get("severity") == "warning" for m, kw in notes if "json" in m)

    run(check())


def test_broken_files_are_translated(user_dirs):
    async def check() -> None:
        broken = user_dirs / "history.json"
        broken.write_text("[{", encoding="utf-8")
        storage.read_json(broken, [])
        app, notes = _app(language="uk")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "пошкоджено" in _messages(notes)

    run(check())


def test_startup_warnings_are_shown(monkeypatch):
    async def check() -> None:
        app, notes = _app()
        monkeypatch.setattr(app.session, "startup_warnings", lambda: ["The saved proxy is not used"])
        async with app.run_test() as pilot:
            await pilot.pause()
            assert any(m == "The saved proxy is not used" and kw["severity"] == "warning" for m, kw in notes)

    run(check())


def test_playback_errors_are_logged(caplog):
    async def check() -> None:
        app, notes = _app()

        async def broken() -> None:
            raise RuntimeError("player exploded")

        async with app.run_test() as pilot:
            app.play(broken(), "Starting")
            await pilot.pause(0.2)
            assert "player exploded" in _messages(notes)

    with caplog.at_level(logging.WARNING):
        run(check())
    failed = [r for r in caplog.records if r.getMessage() == "playback failed"]
    assert failed and failed[0].exc_info is not None


# --- footer language ---------------------------------------------------------


def _footer(app: AnihubApp) -> set[str]:
    try:
        footer = app.screen.query_one(Footer)
    except NoMatches:  # the screen is being recomposed after the language changed
        return set()
    return {key.description for key in footer.query("FooterKey")}  # type: ignore[attr-defined]


async def _footer_when(pilot: Any, app: AnihubApp, ready: Any) -> set[str]:
    """The footer once `ready(descriptions)` holds: it is recomposed after the screen or the language changes,
    which takes more than one pause on a slow CI runner."""
    for _ in range(100):
        shown = _footer(app)
        if ready(shown):
            return shown
        await pilot.pause(0.05)
    return _footer(app)


def test_textual_shows_bindings_from_active_bindings():
    """i18n translates in `Screen.active_bindings`: if Textual changes it, the footer stays in English."""
    shown = inspect.getattr_static(Screen, "active_bindings", None)
    assert isinstance(shown, property), "Textual has changed Screen.active_bindings, see i18n.localized_bindings"
    patched = inspect.getattr_static(HomeScreen, "active_bindings")
    assert isinstance(patched, property) and patched.fget in i18n._translating


def test_footer_follows_the_language():
    async def check() -> None:
        app, _ = _app()
        async with app.run_test(size=(160, 40)) as pilot:
            english = {"Search", "Refresh", "Settings"}
            shown = await _footer_when(pilot, app, english.issubset)
            assert english <= shown, shown
            app.session.settings.language = "uk"
            app.apply_language()
            ukrainian = {"Пошук", "Оновити", "Налаштування"}
            shown = await _footer_when(pilot, app, ukrainian.issubset)
            assert ukrainian <= shown and "Search" not in shown, shown
            app.session.settings.language = "en"
            app.apply_language()
            shown = await _footer_when(pilot, app, lambda keys: "Search" in keys)
            assert "Search" in shown, shown

    run(check())


# --- MAL tracking ------------------------------------------------------------


def _linked() -> tuple[str, str]:
    item = Item("Frieren", "https://site/frieren")
    mal_links.save(item, "site", MalAnime.from_json({"id": 1, "title": "Frieren", "num_episodes": 28}))
    return "site", item_key(item)


@pytest.mark.parametrize("episode", ["12.5", "OVA", 0])
def test_mal_track_tells_about_uncounted_episodes(episode, monkeypatch, caplog):
    called: list[Any] = []

    async def episode_watched(*args: Any) -> Any:
        called.append(args)

    monkeypatch.setattr(tui_mal, "episode_watched", episode_watched)

    async def check() -> None:
        app, notes = _app()
        async with app.run_test() as pilot:
            await app.mal_track(*_linked(), episode).wait()  # type: ignore[arg-type]
            await pilot.pause()
            assert any(f"«{episode}»" in m and kw["severity"] == "warning" for m, kw in notes), notes

    with caplog.at_level(logging.WARNING):
        run(check())
    assert not called
    assert any("not counted on MyAnimeList" in r.getMessage() for r in caplog.records)


def test_mal_track_counts_the_episode(monkeypatch):
    anime = MalAnime.from_json({"id": 1, "title": "Frieren", "num_episodes": 28})
    called: list[Any] = []

    async def episode_watched(session: Any, mal_id: int, episode: int) -> Any:
        called.append((mal_id, episode))
        return anime, {"num_watched_episodes": episode}

    monkeypatch.setattr(tui_mal, "episode_watched", episode_watched)

    async def check() -> None:
        app, notes = _app()
        async with app.run_test() as pilot:
            await app.mal_track(*_linked(), "3").wait()  # type: ignore[arg-type]
            await pilot.pause()
            assert any(kw.get("title") == "MyAnimeList" for _, kw in notes), notes

    run(check())
    assert called == [(1, 3)]


def test_mal_track_logs_unexpected_errors(monkeypatch, caplog):
    async def episode_watched(*args: Any) -> Any:
        raise KeyError("list_status")

    monkeypatch.setattr(tui_mal, "episode_watched", episode_watched)

    async def check() -> None:
        app, notes = _app()
        async with app.run_test() as pilot:
            await app.mal_track(*_linked(), 3).wait()
            await pilot.pause()
            assert any("not counted" in m and kw["severity"] == "error" for m, kw in notes), notes

    with caplog.at_level(logging.WARNING):
        run(check())
    assert any(r.exc_info is not None and "not counted" in r.getMessage() for r in caplog.records)


# --- search tab --------------------------------------------------------------


async def _open(pilot: Any, home: HomeScreen, index: int) -> None:
    results = home.query_one("#search-results")
    results.focus()
    results.highlighted = index  # type: ignore[attr-defined]
    await pilot.press("enter")
    await pilot.pause()


def test_mal_hint_goes_to_one_result():
    """Searched from the MAL tab: the first opened result gets the entry, another result opened after Esc does not."""

    async def check() -> None:
        app, _ = _app("Frieren", "Frieren OVA")
        anime = MalAnime.from_json({"id": 1, "title": "Frieren"})
        async with app.run_test() as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            opened: list[tuple[str, str, MalAnime | None]] = []
            home.open_title = lambda item, name, mal_hint=None: opened.append((item.title, name, mal_hint))  # type: ignore[method-assign]
            home.action_show_tab("search")
            await home.search("Frieren", mal_hint=_MalHint(anime)).wait()
            await pilot.pause()
            for index in (0, 1, 0):
                await _open(pilot, home, index)
            assert [(title, hint) for title, _, hint in opened] == [
                ("Frieren", anime),
                ("Frieren OVA", None),
                ("Frieren", anime),
            ]
            # a new search is not for the MAL entry anymore
            await home.search("Frieren").wait()
            await pilot.pause()
            await _open(pilot, home, 0)
            assert opened[-1][2] is None

    run(check())


def test_mal_hint_survives_a_failed_search_and_refresh():
    """The site search failed after the hint was shown: F5 repeats it for the same MAL entry."""

    async def check() -> None:
        app, _ = _app("Frieren", "Frieren OVA")
        anime = MalAnime.from_json({"id": 1, "title": "Frieren"})
        extractor = app.session.extractor
        search = extractor.a_search
        failing = [True]

        async def a_search(query: str) -> Any:
            if failing.pop(0) if failing else False:
                msg = "timeout"
                raise RuntimeError(msg)
            return await search(query)

        extractor.a_search = a_search  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            opened: list[Any] = []
            home.open_title = lambda item, name, mal_hint=None: opened.append(mal_hint)  # type: ignore[method-assign]
            home.action_show_tab("search")
            home.query_one("#query").value = "Frieren"  # type: ignore[attr-defined]
            await home.search("Frieren", mal_hint=_MalHint(anime)).wait()
            await pilot.pause()
            home.action_refresh()
            await pilot.pause()
            await home.workers.wait_for_complete()
            await _open(pilot, home, 0)
            assert opened == [anime]
            # reset() forgets it
            home.reset()
            assert home._pending_hint is None

    run(check())


def test_mal_hint_skips_linked_results():
    """A result linked already keeps its link: the hint waits for the result it was meant for."""

    async def check() -> None:
        app, _ = _app("Frieren", "Frieren OVA")
        anime = MalAnime.from_json({"id": 1, "title": "Frieren"})
        async with app.run_test() as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            extractor_name = app.session.settings.extractor_name
            mal_links.save(app.session.extractor.items[0], extractor_name, MalAnime.from_json({"id": 2, "title": "x"}))  # type: ignore[attr-defined]
            opened: list[MalAnime | None] = []
            home.open_title = lambda item, name, mal_hint=None: opened.append(mal_hint)  # type: ignore[method-assign]
            home.action_show_tab("search")
            await home.search("Frieren", mal_hint=_MalHint(anime)).wait()
            await pilot.pause()
            for index in (0, 1, 0):
                await _open(pilot, home, index)
            assert opened == [None, anime, None]

    run(check())


def test_source_change_cancels_the_search():
    """A slow search started before the source was changed does not show its results with the new source."""

    async def check() -> None:
        app, _ = _app("Old site title")
        extractor = app.session.extractor
        extractor.gate.clear()  # type: ignore[attr-defined]
        async with app.run_test() as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            worker = home.search("title")
            await pilot.pause()
            home.reset()
            extractor.gate.set()  # type: ignore[attr-defined]
            await pilot.pause(0.2)
            assert worker.is_cancelled
            assert home.query_one("#search-results").items == []  # type: ignore[attr-defined]

    run(check())


def test_results_keep_their_source():
    """Results opened after the source was changed open with the site they came from."""

    async def check() -> None:
        app, _ = _app("Title")
        async with app.run_test() as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            first = app.session.settings.extractor_name
            opened: list[str] = []
            home.open_title = lambda item, name, mal_hint=None: opened.append(name)  # type: ignore[method-assign]
            home.action_show_tab("search")
            await home.search("Title").wait()
            app.session.settings.extractor_name = "another-site"
            await pilot.pause()
            await _open(pilot, home, 0)
            assert opened == [first]

    run(check())


def test_history_label_is_new_until_the_release_is_watched():
    entry = {"data": {"title": "Naruto"}, "extractor_name": "animego", "episode": "3", "watched_at": 1000}
    now = 5000
    assert "NEW" in _history_label(entry, released=2000, now=now).plain
    assert "NEW" not in _history_label(entry, released=500, now=now).plain  # watched after the release
    assert "NEW" not in _history_label(entry, released=0, now=now).plain
    assert "NEW" not in _history_label(entry, released=2000, now=2000 + ongoing.NEW_FOR + 1).plain
    # saved before watched_at was remembered: a recent release is new
    assert "NEW" in _history_label({**entry, "watched_at": None}, released=2000, now=now).plain
