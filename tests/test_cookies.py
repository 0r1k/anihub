from __future__ import annotations

import logging
import sys
import types
from http.cookiejar import CookieJar
from pathlib import Path

import httpx
import pytest

from anihub import main
from anihub.common.cookies_config import (
    BROWSER_SUPPORTS,
    parse_netscape_cookies_file,
    read_from_browser,
    read_from_netscape_file,
)
from anihub.common.extractors import _site_domain, dynamic_load_extractor_module, get_extractor_modules, source_domains

FUTURE = 4102444800  # 2100-01-01


def _line(domain: str, name: str, value: str, *, secure: str = "FALSE", expires: object = FUTURE) -> str:
    return "\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", "/", secure, str(expires), name, value])


def _sent_cookies(jar: CookieJar, url: str) -> set[str]:
    """Cookies httpx sends to `url` from `jar`."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie", ""))
        return httpx.Response(200)

    with httpx.Client(cookies=jar, transport=httpx.MockTransport(handler)) as client:
        client.get(url)
    return set(filter(None, sent[0].split("; ")))


def test_httponly_cookies_are_loaded(tmp_path):
    path = tmp_path / "cookies.txt"
    lines = [
        "# Netscape HTTP Cookie File",
        "#HttpOnly_" + _line(".animego.me", "session", "abc", secure="TRUE"),
        _line("animego.me", "plain", "1", expires=0),
        "# a comment",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    jar = read_from_netscape_file(path)

    cookies = {c.name: c for c in jar}
    assert set(cookies) == {"session", "plain"}
    assert cookies["session"].domain == ".animego.me"
    assert cookies["session"].has_nonstandard_attr("HTTPOnly")
    assert not cookies["plain"].has_nonstandard_attr("HTTPOnly")
    assert cookies["plain"].expires is None  # 0: a session cookie
    assert _sent_cookies(jar, "https://animego.me/anime/1") == {"session=abc", "plain=1"}
    # secure: https only
    assert _sent_cookies(jar, "http://animego.me/") == {"plain=1"}
    assert _sent_cookies(jar, "https://example.com/") == set()


def test_broken_lines_are_skipped(tmp_path, caplog):
    path = tmp_path / "cookies.txt"
    lines = [
        _line(".animego.me", "good", "1"),
        _line(".animego.me", "bad_expires", "2", expires="never"),
        ".animego.me\tTRUE\t/\tFALSE",
        # an empty value: the line ends with a tab
        _line(".animego.me", "empty", ""),
    ]
    data = "\n".join(lines).encode() + b"\n" + _line(".animego.me", "latin1", "caf\xe9").encode("latin-1") + b"\n"
    path.write_bytes(data)

    with caplog.at_level(logging.WARNING, logger="anihub.common.cookies_config"):
        cookies = parse_netscape_cookies_file(path)

    assert [(c["name"], c["value"]) for c in cookies] == [("good", "1"), ("empty", "")]
    warnings = [r.getMessage() for r in caplog.records]
    assert [w.split(" is skipped")[0].rsplit(" ", 1)[-1] for w in warnings] == ["2", "3", "5"]
    # the values of the cookies are secret: not in the log
    assert all("caf" not in w for w in warnings)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_bom_and_line_ends(tmp_path, newline):
    path = tmp_path / "cookies.txt"
    text = newline.join(["#HttpOnly_" + _line(".anitube.in.ua", "a", "1"), _line(".anitube.in.ua", "b", "2"), ""])
    path.write_bytes("﻿".encode() + text.encode())

    cookies = parse_netscape_cookies_file(path)

    assert [(c["domain"], c["name"], c["value"], c["http_only"]) for c in cookies] == [
        (".anitube.in.ua", "a", "1", True),
        (".anitube.in.ua", "b", "2", False),
    ]


def test_not_a_netscape_file(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text('[{"domain": ".animego.me", "name": "a", "value": "b"}]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no cookies in Netscape format"):
        read_from_netscape_file(path)


def test_file_without_cookies_is_empty(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n\n", encoding="utf-8")
    assert list(read_from_netscape_file(path)) == []


# --- browser ---------------------------------------------------------------


@pytest.fixture
def fake_rookiepy(monkeypatch):
    """rookiepy (it has no build for every Python) that records what it is asked to read."""
    calls: list[tuple[str, list[str] | None]] = []
    module = types.ModuleType("rookiepy")

    def reader(name: str):
        def read(domains: list[str] | None = None) -> list[dict]:
            calls.append((name, domains))
            return [{"name": "sid"}]

        return read

    module.firefox = reader("firefox")  # type: ignore[attr-defined]
    module.load = reader("load")  # type: ignore[attr-defined]
    module.to_cookiejar = lambda cookies: ("jar", cookies)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rookiepy", module)
    return calls


def test_browser_cookies_of_given_domains(fake_rookiepy):
    assert read_from_browser("firefox", ["animego.me"]) == ("jar", [{"name": "sid"}])
    assert fake_rookiepy == [("firefox", ["animego.me"])]


def test_any_browser_reads_every_installed_browser(fake_rookiepy):
    # rookiepy's own any_browser needs the path of a cookie database
    read_from_browser("any_browser", ["animego.me"])
    assert fake_rookiepy == [("load", ["animego.me"])]


def test_no_domains_nothing_is_read(fake_rookiepy):
    assert list(read_from_browser("firefox", [])) == []
    assert fake_rookiepy == []


def test_browser_choices_work_without_paths():
    assert "any_browser" in BROWSER_SUPPORTS
    assert not {"chromium_based", "firefox_based"} & set(BROWSER_SUPPORTS)


def test_cli_reads_browser_cookies_of_the_sources_only(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "read_from_browser", lambda name, domains=None: calls.append((name, domains)))
    main._cb_check_read_browser_cookie_opt("firefox")
    assert calls == [("firefox", source_domains())]
    assert calls[0][1]


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("animego.me", "animego.me"),
        ("site.yummyani.me", "yummyani.me"),
        ("api.animevost.org", "animevost.org"),
        ("anitube.in.ua", "anitube.in.ua"),
        ("www.example.co.uk", "example.co.uk"),
        ("www.abc.ru", "abc.ru"),
        ("video.sibnet.ru", "sibnet.ru"),
        ("a.b.example.com", "example.com"),
        ("192.168.0.10", "192.168.0.10"),
    ],
)
def test_site_domain(host, domain):
    assert _site_domain(host) == domain


def test_source_domains_cover_every_source():
    domains = source_domains()
    assert domains == sorted(set(domains))
    for name in get_extractor_modules():
        host = httpx.URL(dynamic_load_extractor_module(name).Extractor.BASE_URL).host
        # the site with its subdomains: rookiepy matches a part of the cookie domain
        assert any(host == d or host.endswith("." + d) for d in domains), (name, host)
    for domain in domains:
        assert domain.count(".") >= 1
        assert all(c.isalnum() or c in ".-" for c in domain)


def test_source_domains_include_api_and_player_hosts():
    domains = source_domains()
    for host in ("api.yani.tv", "kodik.info", "kodikplayer.com", "aniboom.one", "video.sibnet.ru", "player.aksor.tv"):
        assert any(host == d or host.endswith("." + d) for d in domains), host
    assert {"animego.org", "anilibria.top"} <= set(domains)


def test_source_domains_cover_hosts_in_anicli_api():
    """A new host in an anicli-api update must be added to EXTRA_HOSTS."""
    import re

    import anicli_api

    domains = source_domains()
    root = Path(next(iter(anicli_api.__path__)))
    hosts = set()
    for sub in ("source", "player"):
        for file in (root / sub).rglob("*.py"):
            hosts.update(re.findall(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", file.read_text(encoding="utf-8")))
    # documentation links and examples, not requests
    hosts -= {"github.com", "aniliberty.top", "anilibria.top"}
    hosts = {h for h in hosts if not h.endswith(".")}
    missing = {h for h in hosts if not any(h == d or h.endswith("." + d) for d in domains)}
    assert not missing
