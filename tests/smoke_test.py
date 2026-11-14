"""Offline smoke test of the installed package: run on every OS in CI (python tests/smoke_test.py).

Starts the TUI headless, walks the tabs and the settings, imports every extractor, streams through the proxy.
No network: the ongoing loader is replaced, MyAnimeList is not logged in, the video hosting is local.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# keep the user (runner) config untouched: every OS reads its own variable
_tmp = tempfile.mkdtemp(prefix="anihub-smoke-")
for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA"):
    os.environ[var] = _tmp
if sys.platform == "darwin":
    os.environ["HOME"] = _tmp

import httpx  # noqa: E402
from anicli_api.player.base import Video  # noqa: E402
from textual.widgets import Checkbox  # noqa: E402

from anihub.common.config import APP_NAME, OLD_APP_NAME, _get_base_dir, get_config_dir, get_data_dir  # noqa: E402
from anihub.common.episodes import leaked_numbers  # noqa: E402
from anihub.common.extractors import dynamic_load_extractor_module, get_extractor_modules  # noqa: E402
from anihub.common.mal_profile import parse_friends, parse_profile  # noqa: E402
from anihub.common.players.base import split_args, split_command  # noqa: E402
from anihub.common.stream_proxy import StreamProxy  # noqa: E402
from anihub.tui import AnihubApp, Session, Settings  # noqa: E402
from anihub.tui.screens.settings import SettingsScreen  # noqa: E402
from anihub.tui.subtitles import _Wanted  # noqa: E402


def check_extractors() -> None:
    names = get_extractor_modules()
    assert "anitube" in names, names
    for name in names:
        module = dynamic_load_extractor_module(name)
        for cls in ("Extractor", "Search", "Ongoing", "Anime", "Episode", "Source"):
            assert hasattr(module, cls), f"{name}.{cls}"
    print(f"extractors ok: {', '.join(names)}")


def check_paths() -> None:
    for path in (get_config_dir(), get_data_dir()):
        probe = Path(path) / "probe.json"
        probe.write_text('{"ok": "тест"}', encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == '{"ok": "тест"}'
    # the directory of the old name moves to the new one with its files
    base = Path(tempfile.mkdtemp(prefix="anihub-move-"))
    (base / OLD_APP_NAME).mkdir()
    (base / OLD_APP_NAME / "history.json").write_text("[1]", encoding="utf-8")
    moved = _get_base_dir("", base)
    assert moved == base / APP_NAME and (moved / "history.json").read_text(encoding="utf-8") == "[1]", moved
    assert not (base / OLD_APP_NAME).exists() and _get_base_dir("", base) == moved
    # a busy directory (Windows: a file open in another anihub) stays in use until exit, even if freed meanwhile
    busy = Path(tempfile.mkdtemp(prefix="anihub-busy-"))
    (busy / OLD_APP_NAME).mkdir()
    rename = Path.rename

    def refuse(self: Path, _target: Path) -> Path:
        raise PermissionError(self)

    Path.rename = refuse  # type: ignore[method-assign]
    try:
        assert _get_base_dir("", busy) == busy / OLD_APP_NAME
    finally:
        Path.rename = rename  # type: ignore[method-assign]
    assert _get_base_dir("", busy) == busy / OLD_APP_NAME and not (busy / APP_NAME).exists()
    print(f"paths ok: {get_config_dir()} | {get_data_dir()}")


def check_commands() -> None:
    assert split_command("mpv --fs") == ["mpv", "--fs"]
    if os.name == "nt":
        assert split_args(r"--sub-file=C:\subs\a.ass") == [r"--sub-file=C:\subs\a.ass"]
    print("player commands ok")


def check_title_matching() -> None:
    """Sites name seasons differently: the same season matches, another one does not."""
    wanted = _Wanted(["Влюблённые учёные и научное доказательство любви 2"], None, {"2"})
    assert wanted.is_exact("Влюблённые учёные и научное доказательство любви [ТВ-2] / Наука влюблена [ТВ-2] 2 сезон")
    assert wanted.is_similar("Научное доказательство любви 2")
    for other in ("Научное доказательство любви", "Влюблённые учёные и научное доказательство любви [ТВ-1]", "Любви 2"):
        assert not wanted.is_exact(other) and not wanted.is_similar(other), other
    seasons = _Wanted(
        ["Наука влюблена (второй сезон)", "Наука закохалася (Сезон 2)", "Mushoku Tensei 2nd Season"], None, set()
    )
    assert (
        seasons.is_exact("Наука влюблена 2")
        and seasons.is_exact("Наука закохалася 2")
        and seasons.is_exact("Mushoku Tensei 2")
    )
    print("title matching ok")


def check_leaked_episodes() -> None:
    """A few episodes after a gap are a leak, many are a site missing an episode."""
    assert leaked_numbers([1, 2, 3, 4, 5, 12, 6]) == {12}
    assert leaked_numbers([*range(1, 7), 12, 13, 24]) == {12, 13, 24}
    assert leaked_numbers([1, 2, 3, 5, 6, 7, 8]) == set()
    assert leaked_numbers([1, 2, 3, 4, 6]) == set()
    assert leaked_numbers([*range(13, 20), "OVA", 0]) == set()
    assert leaked_numbers([]) == leaked_numbers([7]) == set()
    print("leaked episodes ok")


PROFILE_PAGE = """<html><body>
<div class="user-image mb8"><img class="lazyload" data-src="https://cdn/avatar.jpg"></div>
<ul class="user-status border-top pb8 mb4">
<li class="clearfix"><span class="user-status-title">Last Online</span><span class="user-status-data">Now</span></li>
<li class="clearfix"><span class="user-status-title">Joined</span>
<span class="user-status-data">May 19, 2024</span></li>
</ul>
<div class="user-profile-about"><div class="profile-about-user">Hello<br><br>world</div></div>
<div class="stats anime">
<div class="stat-score"><div><span>Days: </span>41.6</div><div><span>Mean Score: </span><span>8.48</span></div></div>
<ul class="stats-status fl-l">
<li><a class="circle anime watching">Watching</a><span>8</span></li>
<li><a class="circle anime completed">Completed</a><span>1,124</span></li>
</ul>
<ul class="stats-data fl-r"><li><span>Total Entries</span><span>1,132</span></li></ul>
</div>
<div class="updates anime"><div class="statistics-updates"><div class="data">
<a href="https://myanimelist.net/anime/61578/Seitokai">Seitokai ni mo Ana wa Aru!</a>
<div class="graph-content"><span class="fl-r fn-grey2">59 minutes ago</span>
<div class="graph"><span class="graph-inner anime watching"></span></div></div>
<div class="fn-grey2"> Watching <span>5</span>/12 · Scored <span>6</span></div>
</div></div></div>
<div id="anime_favorites"><ul><li title="Cowboy Bebop"><a href="https://myanimelist.net/anime/1/Cowboy_Bebop">
<span class="title">Cowboy Bebop</span><span class="users">TV·1998</span></a></li></ul></div>
</body></html>"""
FRIENDS_PAGE = """<div class="boxlist-container friend mb16"><div class="boxlist col-3"><div class="data">
<div class="title"><a href="https://myanimelist.net/profile/D_FNX">D_FNX</a></div>
<div class="di-ib mt4 fn-grey2"> Yesterday, 8:36 AM </div>
<div class="di-ib mt4 fn-grey2"> Friends since May 19, 2024 1:35 PM </div>
</div></div></div>"""


def check_mal_profile() -> None:
    """The profile and friends pages of the site (the MAL API has no other users)."""
    profile = parse_profile("or1k", PROFILE_PAGE)
    assert profile.avatar == "https://cdn/avatar.jpg", profile.avatar
    assert profile.status == [("Last Online", "Now"), ("Joined", "May 19, 2024")], profile.status
    assert profile.about == "Hello\n\nworld", profile.about
    assert (profile.anime.days, profile.anime.mean_score) == ("41.6", "8.48"), profile.anime
    assert [(s.status, s.count) for s in profile.anime.statuses] == [("watching", 8), ("completed", 1124)]
    assert profile.anime.data == [("Total Entries", "1,132")], profile.anime.data
    (update,) = profile.updates
    assert (update.anime_id, update.status, update.ago) == (61578, "watching", "59 minutes ago"), update
    assert update.details == "Watching 5/12 · Scored 6", update.details
    assert [(f.kind, f.title, f.anime_id) for f in profile.favorites] == [("anime", "Cowboy Bebop", 1)]
    (friend,) = parse_friends(FRIENDS_PAGE)
    assert friend.name == "D_FNX", friend
    assert (friend.last_online, friend.since) == ("Yesterday, 8:36 AM", "May 19, 2024 1:35 PM"), friend
    print("mal profile ok")


async def check_stream_proxy() -> None:
    """The proxy rewrites playlists, passes headers and ranges, resumes a body broken by the hosting.

    A resume that does not continue the body (an error page, another file) never gets into the video.
    """
    segment = bytes(range(256)) * 400
    requests: list[dict[str, str]] = []
    broken = {1, 4}  # request numbers of the downloads that break in the middle
    error_page, other_file = 5, 6  # wrong answers to the resume of the second broken download

    async def hosting(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        number = len(requests)
        requests.append({"path": path, **headers})
        if path == "/v/index.m3u8":
            body = b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\nseg-1.ts\n#EXT-X-ENDLIST\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%b" % (len(body), body))
        elif number == error_page:
            body = b"<html>error</html>" * 2000
            writer.write(b"HTTP/1.1 503 Busy\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%b" % (len(body), body))
        else:
            start = int(headers.get("range", "bytes=0-")[6:].split("-")[0])
            data = segment + b"x" if number == other_file else segment
            body = data[start:]
            head = b"HTTP/1.1 200 OK\r\n"
            if start:
                head = b"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes %d-%d/%d\r\n" % (
                    start,
                    len(data) - 1,
                    len(data),
                )
            writer.write(head + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
            writer.write(body[: len(body) // 2] if number in broken else body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(hosting, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = StreamProxy()
    try:
        video = Video(type="m3u8", quality=720, url=f"http://127.0.0.1:{port}/v/index.m3u8", headers={"Referer": "r"})
        (local,) = await proxy.wrap([video])
        assert local.url.startswith(f"http://127.0.0.1:{proxy.port}/") and not local.headers, local
        async with httpx.AsyncClient() as client:
            playlist = (await client.get(local.url)).text
            uris = [line for line in playlist.splitlines() if line.startswith("http")]
            assert len(uris) == 1 and uris[0].endswith("/v/seg-1.ts"), playlist
            assert f'URI="http://127.0.0.1:{proxy.port}/' in playlist, playlist
            assert (await client.get(uris[0])).content == segment
            response = await client.get(uris[0], headers={"Range": "bytes=1000-"})
            assert response.status_code == 206 and response.content == segment[1000:], response
            # broken again, the hosting answers the resume with an error page, then with another file
            assert (await client.get(uris[0])).content == segment
        assert requests[2].get("range") == f"bytes={len(segment) // 2}-", requests
        assert len(requests) == 8 and requests[7].get("range") == f"bytes={len(segment) // 2}-", requests
        assert all(r.get("referer") == "r" for r in requests), requests
    finally:
        await proxy.aclose()
        server.close()
    print("stream proxy ok")


async def check_tui() -> None:
    session = Session(Settings(language="uk", posters=False))

    async def no_ongoing() -> list:
        return []

    session.extractor.a_ongoing = no_ongoing  # type: ignore[method-assign]
    app = AnihubApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        home = app.screen
        for tab in ("ongoing", "watching", "history", "mal", "feed", "search"):
            home.action_show_tab(tab)  # type: ignore[attr-defined]
            await pilot.pause(0.2)
        app.action_settings()
        await pilot.pause(0.3)
        assert isinstance(app.screen, SettingsScreen), app.screen
        # saved as the fields change, without a Save button
        app.screen.query_one("#show-leaked", Checkbox).value = True
        await pilot.pause()
        assert Settings.load().show_leaked
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, SettingsScreen), app.screen
    await session.aclose()
    print("tui ok")


def main() -> None:
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    check_paths()
    check_extractors()
    check_commands()
    check_title_matching()
    check_leaked_episodes()
    check_mal_profile()
    asyncio.run(check_stream_proxy())
    asyncio.run(check_tui())


if __name__ == "__main__":
    main()
