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

from anicli.common.config import get_config_dir, get_data_dir  # noqa: E402
from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules  # noqa: E402
from anicli.common.players.base import split_args, split_command  # noqa: E402
from anicli.common.stream_proxy import StreamProxy  # noqa: E402
from anicli.tui import AnicliApp, Session, Settings  # noqa: E402
from anicli.tui.subtitles import _Wanted  # noqa: E402
from anicli.tui.screens.settings import SettingsScreen  # noqa: E402


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


async def check_stream_proxy() -> None:
    """The proxy rewrites playlists, passes headers and ranges, resumes a body broken by the hosting."""
    segment = bytes(range(256)) * 400
    requests: list[dict[str, str]] = []

    async def hosting(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        requests.append({"path": path, **headers})
        if path == "/v/index.m3u8":
            body = b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\nseg-1.ts\n#EXT-X-ENDLIST\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%b" % (len(body), body))
        else:
            start = int(headers.get("range", "bytes=0-")[6:].split("-")[0])
            body = segment[start:]
            status = b"206 Partial Content" if start else b"200 OK"
            writer.write(b"HTTP/1.1 %b\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (status, len(body)))
            # the first download breaks in the middle
            writer.write(body[: len(body) // 2] if len(requests) == 2 else body)
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
        assert requests[2].get("range") == f"bytes={len(segment) // 2}-", requests
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
    app = AnicliApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        home = app.screen
        for tab in ("ongoing", "watching", "history", "mal", "search"):
            home.action_show_tab(tab)  # type: ignore[attr-defined]
            await pilot.pause(0.2)
        app.action_settings()
        await pilot.pause(0.3)
        assert isinstance(app.screen, SettingsScreen), app.screen
        await pilot.press("escape")
        await pilot.pause(0.2)
    await session.aclose()
    print("tui ok")


def main() -> None:
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    check_paths()
    check_extractors()
    check_commands()
    check_title_matching()
    asyncio.run(check_stream_proxy())
    asyncio.run(check_tui())


if __name__ == "__main__":
    main()
