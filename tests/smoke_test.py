"""Offline smoke test of the installed package: run on every OS in CI (python tests/smoke_test.py).

Starts the TUI headless, walks the tabs and the settings, imports every extractor.
No network: the ongoing loader is replaced, MyAnimeList is not logged in.
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

from anicli.common.config import get_config_dir, get_data_dir  # noqa: E402
from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules  # noqa: E402
from anicli.common.players.base import split_args, split_command  # noqa: E402
from anicli.tui import AnicliApp, Session, Settings  # noqa: E402
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
    asyncio.run(check_tui())


if __name__ == "__main__":
    main()
