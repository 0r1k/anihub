"""Proxy setting: which schemes work, a bad saved proxy does not stop the start, set_network is all or nothing."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from textual.widgets import Input

from anihub.common.utils import PROXY_SCHEMES, validate_proxy_url
from anihub.tui.app import AnihubApp
from anihub.tui.screens.settings import SettingsScreen
from anihub.tui.session import NetworkSettingsError, Session, Settings


def run(coro, timeout: float = 60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@pytest.mark.parametrize("scheme", PROXY_SCHEMES)
def test_accepted_schemes_work_in_httpx(scheme):
    url = validate_proxy_url(f"{scheme}://user:pa%40ss@127.0.0.1:1080")
    assert url == f"{scheme}://user:pa%40ss@127.0.0.1:1080"
    httpx.Client(proxy=url).close()
    asyncio.run(httpx.AsyncClient(proxy=url).aclose())


@pytest.mark.parametrize("scheme", ["socks4", "socks4a", "socks", "ftp", "foo", "ws"])
def test_other_schemes_are_rejected(scheme):
    # httpx would refuse these when a client is made: that is what crashed the app
    with pytest.raises(Exception):  # noqa: B017
        httpx.Client(proxy=f"{scheme}://h:1")
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        validate_proxy_url(f"{scheme}://h:1")


@pytest.mark.parametrize(
    ("value", "message"),
    [("h:1", "scheme"), ("//h:1", "scheme"), ("http://", "scheme"), ("http://h", "port"), ("http://:1", "hostname")],
)
def test_incomplete_urls_are_rejected(value, message):
    with pytest.raises(ValueError, match=message):
        validate_proxy_url(value)


def test_ipv6_host_keeps_brackets():
    assert validate_proxy_url("http://[::1]:8080") == "http://[::1]:8080"


def test_dead_helpers_are_gone():
    from anihub.common import utils

    assert not hasattr(utils, "is_arabic_slice") and not hasattr(utils, "str_to_slice")
    assert utils.is_arabic_digit("12")


def test_session_starts_with_a_bad_saved_proxy():
    async def check() -> None:
        session = Session(Settings(proxy="foo://h:1"))
        assert session.proxy is None
        assert session.settings.proxy == "foo://h:1"  # kept, the settings screen shows it
        [warning] = session.startup_warnings()
        assert "foo" in warning
        await session.aclose()

    run(check())


def test_session_with_good_proxy_has_no_warnings():
    async def check() -> None:
        session = Session(Settings(proxy="socks5://127.0.0.1:1080"))
        assert session.proxy == "socks5://127.0.0.1:1080" and session.startup_warnings() == []
        await session.aclose()

    run(check())


def test_set_network_with_a_bad_proxy_changes_nothing():
    async def check() -> None:
        session = Session(Settings(timeout=30))
        before = (session.http, session.http_async, session.mal, session.extractor)
        with pytest.raises(NetworkSettingsError, match="foo"):
            session.set_network(proxy="foo://h:1", timeout=5)
        assert (session.http, session.http_async, session.mal, session.extractor) == before
        assert (session.settings.proxy, session.settings.timeout) == (None, 30)
        await session.aclose()

    run(check())


def test_old_clients_work_after_set_network_until_aclose():
    async def check() -> None:
        session = Session(Settings(mal_client_id="abc"))
        old_sync, old_async, old_mal = session.http, session.http_async, session.mal
        session.set_network(proxy="http://127.0.0.1:3128", timeout=7)
        assert session.http is not old_sync and session.proxy == "http://127.0.0.1:3128"
        assert session.mal.client_id == "abc" and session.settings.timeout == 7
        # open screens still hold the old clients
        assert not old_sync.is_closed and not old_async.is_closed and not old_mal.http.is_closed
        await session.aclose()
        assert old_sync.is_closed and old_async.is_closed and session.http.is_closed

    run(check())


def test_bad_proxy_in_settings_screen_notifies_and_is_not_saved():
    async def check() -> None:
        session = Session(Settings(language="en", posters=False))

        async def no_ongoing() -> list:
            return []

        session.extractor.a_ongoing = no_ongoing  # type: ignore[method-assign]
        app = AnihubApp(session)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_settings()
            await pilot.pause(0.3)
            assert isinstance(app.screen, SettingsScreen)
            app.screen.query_one("#proxy", Input).value = "foo://h:1"
            await pilot.pause()
            assert Settings.load().proxy is None
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert session.settings.proxy is None and session.proxy is None
            assert Settings.load().proxy is None
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, SettingsScreen)
        await session.aclose()

    run(check())


def test_refused_network_settings_keep_the_screen_open_and_restore_the_fields(monkeypatch):
    async def check() -> None:
        session = Session(Settings(language="en", posters=False))

        def refuse(**_kwargs):
            raise NetworkSettingsError("refused")

        monkeypatch.setattr(session, "set_network", refuse)
        app = AnihubApp(session)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_settings()
            await pilot.pause(0.3)
            app.screen.query_one("#proxy", Input).value = "http://127.0.0.1:3128"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SettingsScreen)  # no crash, still open
            assert app.screen.query_one("#proxy", Input).value == ""
            assert Settings.load().proxy is None and session.settings.proxy is None
        await session.aclose()

    run(check())
