from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.theme import Theme

import anihub
from anihub.common import mal_links, storage
from anihub.common.mal import MalAnime, MalAuthError, MalError
from anihub.common.players import PlayerArgsError, PlayerNotFoundError, resolve_command

from . import logs, mal
from .i18n import localized_bindings, set_language, t
from .playback import ConfirmWatched, OnWatched, PlaybackError
from .screens.anime import AnimeScreen
from .screens.dialogs import ConfirmScreen
from .screens.home import HomeScreen
from .screens.mal_dialogs import ScoreScreen
from .screens.settings import SettingsScreen
from .session import Session

if TYPE_CHECKING:
    from collections.abc import Iterable

    from anihub.common.updater import TPACKAGE

logger = logging.getLogger(__name__)
# `AnihubApp.run()` result: the user chose to update anihub, the terminal runs the update after the app exits
EXIT_UPDATE = "update"
# how often to look for JSON files found broken (and moved aside) while the app runs
BROKEN_FILES_CHECK = 1.0
# the bottom of the screen stack: Textual's default screen and the home screen
_HOME_DEPTH = 2


@localized_bindings
class AnihubApp(App[str | None]):
    CSS_PATH = "app.tcss"
    TITLE = "anihub"
    BINDINGS = [
        Binding("f2", "settings", "Settings"),
        # shown only when a new release is found at start
        Binding("f9", "update", "Update anihub"),
        Binding("x", "stop_playback", "Stop"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    player_status: reactive[str] = reactive("")
    player_progress: reactive[tuple[int, int] | None] = reactive(None)

    def __init__(self, session: Session, *, query: str | None = None, ongoing: bool = False) -> None:
        # library logs written to the terminal would break the layout
        logs.setup()
        # texts made at compose time are translated: screens are made right below
        set_language(session.settings.language)
        super().__init__()
        self.session = session
        self._home = HomeScreen(query=query, ongoing=ongoing)
        self._play_ids = itertools.count()
        self._current_play = -1
        # list updates read-modify-write the entry: run them one by one
        self._mal_lock = asyncio.Lock()
        # the newer anihub release found at start (None: up to date or the check failed)
        self._new_version: str | None = None
        # broken files (storage.broken_files) the user was told about
        self._broken_shown = 0

    def on_mount(self) -> None:
        self.sub_title = self.session.settings.extractor_name
        if self.session.settings.theme in self.available_themes:
            self.theme = self.session.settings.theme
        self.theme_changed_signal.subscribe(self, self._save_theme)
        self.push_screen(self._home)
        # already translated: the language is set above
        for warning in self.session.startup_warnings():
            self.notify(warning, severity="warning", timeout=20, markup=False)
        # settings and the MAL token are read before the app starts, the history and the rest while it runs
        self.show_broken_files()
        self.set_interval(BROKEN_FILES_CHECK, self.show_broken_files)
        self.check_player()
        self.check_updates()

    def show_broken_files(self) -> None:
        """Tell about every JSON file found broken and moved aside since the start, once."""
        new = storage.broken_files[self._broken_shown :]
        self._broken_shown += len(new)
        for path, moved in new:
            self.notify(
                t(
                    "{file} was damaged and is reset. The damaged copy is kept next to it: {moved}",
                    file=str(path),
                    moved=moved.name,
                ),
                title=t("Damaged file"),
                severity="warning",
                timeout=20,
                markup=False,
            )

    def check_player(self) -> None:
        command = self.session.settings.player_executable
        try:
            resolve_command(command)
        except PlayerArgsError as e:
            # an unclosed quote in a custom command: the player may well be installed
            message = t("Wrong player arguments: {error}. Fix them in settings (F2)", error=e)
        except PlayerNotFoundError:
            message = t("Player not found: {command}. Choose another one in settings (F2)", command=command or "?")
        else:
            return
        self.notify(message, severity="error", timeout=10, markup=False)

    def _save_theme(self, theme: Theme) -> None:
        settings = self.session.settings
        if settings.theme != theme.name:
            settings.theme = theme.name
            with contextlib.suppress(OSError):
                settings.save()

    async def on_unmount(self) -> None:
        await self.session.aclose()

    # --- playback ------------------------------------------------------------

    def play(self, coro: Awaitable[None], status: str) -> None:
        """Run playback in background; a new playback stops the previous one. `status` is shown until it starts."""
        play_id = next(self._play_ids)
        self._current_play = play_id
        self.player_status = status
        self.run_worker(self._play(coro, play_id), group="player", exclusive=True, exit_on_error=False)

    async def _play(self, coro: Awaitable[None], play_id: int) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except PlaybackError as e:
            # its cause (the player, the site) is in the traceback
            logger.warning("playback failed: %s", e, exc_info=True)
            self.notify(str(e), severity="error", markup=False)
        except Exception as e:
            logger.exception("playback failed")
            self.notify(t("Playback error: {error}", error=e), severity="error", markup=False)
        finally:
            if play_id == self._current_play:
                self.player_status = ""
                self.player_progress = None
                if isinstance(self.screen, HomeScreen):
                    self.screen.on_screen_resume()

    def action_stop_playback(self) -> None:
        if self.player_status:
            self.workers.cancel_group(self, "player")
            self.player_status = ""
            self.player_progress = None

    # --- MyAnimeList ---------------------------------------------------------

    def mal_tracker(self, extractor_name: str, key: str) -> OnWatched | None:
        """Playback callback counting watched episodes of the title on MyAnimeList."""
        if not self.session.mal_tracking:
            return None

        def track(episode: int) -> None:
            # starts the worker: the playback does not wait for MyAnimeList
            self.mal_track(extractor_name, key, episode)

        return track

    def mal_confirm_watched(self, extractor_name: str, key: str) -> ConfirmWatched | None:
        if not self.session.mal_tracking:
            return None

        async def confirm(title: str) -> bool:
            if mal_links.get_by_key(extractor_name, key) is None:
                return False
            question = t("Mark «{title}» as watched on MyAnimeList?", title=title)
            # a typed local: inside bool() mypy would expect Screen[object]
            confirmed = await self.push_screen_wait(ConfirmScreen(question))
            return bool(confirmed)

        return confirm

    @work(group="mal-track", exit_on_error=False)
    async def mal_track(self, extractor_name: str, key: str, episode: int) -> None:
        link = mal_links.get_by_key(extractor_name, key)
        if link is None:
            self.notify(
                t("The episode is not counted on MyAnimeList: link the title first (l on the title page)"),
                severity="warning",
                markup=False,
            )
            return
        number = mal.episode_number(episode)
        if number is None:
            logger.warning("episode %r of %s %s is not counted on MyAnimeList", episode, extractor_name, key)
            self.notify(
                t(
                    "Episode «{episode}» is not counted on MyAnimeList: it counts episodes 1, 2, 3… "
                    "Set the watched episodes on the title page (e)",
                    episode=episode,
                ),
                severity="warning",
                markup=False,
            )
            return
        try:
            async with self._mal_lock:
                anime, fields = await mal.episode_watched(self.session, link["mal_id"], number)
        except MalAuthError as e:
            logger.warning("episode %s of %s %s is not counted on MyAnimeList: %s", number, extractor_name, key, e)
            self.notify(t("{error}. Log in again in settings (F2)", error=e), severity="error", markup=False)
            return
        except MalError as e:
            logger.warning("episode %s of %s %s is not counted on MyAnimeList: %s", number, extractor_name, key, e)
            self.notify(str(e), severity="error", markup=False)
            return
        except Exception as e:
            # a worker failing with exit_on_error=False is silent
            logger.exception("episode %s of %s %s is not counted on MyAnimeList", number, extractor_name, key)
            message = t("The episode is not counted on MyAnimeList: {error}", error=e)
            self.notify(message, severity="error", markup=False)
            return
        if anime.num_episodes and number > anime.num_episodes:
            # eg: the site numbers the episodes of all seasons in a row
            logger.warning("episode %s of %s %s is beyond the MAL entry %s", number, extractor_name, key, anime.id)
            self.notify(
                t(
                    "Episode {episode} is not counted on MyAnimeList: «{title}» has {total} episodes there. "
                    "Check the link on the title page (l)",
                    episode=number,
                    title=anime.title,
                    total=anime.num_episodes,
                ),
                severity="warning",
                markup=False,
            )
        for screen in self.screen_stack:
            if isinstance(screen, AnimeScreen) and screen.matches(extractor_name, key):
                screen.set_mal(anime)
        if not fields:
            return
        self.notify(mal.describe(anime, with_status=True).plain, title="MyAnimeList", markup=False)
        if fields.get("status") == "completed":
            self.offer_score(anime)

    def offer_score(self, anime: MalAnime) -> None:
        """The title is completed: ask for a score (the current one is preselected, Esc to skip)."""
        if any(isinstance(s, ScoreScreen) and s.anime_id == anime.id for s in self.screen_stack):
            return
        current = anime.list_status.score if anime.list_status else 0
        message = t("You have watched «{title}» to the end. Rate it on MyAnimeList?", title=anime.title)

        def on_score(score: int | None) -> None:
            if score is not None and score != current:
                self.mal_rate(anime, score)

        self.push_screen(ScoreScreen(anime.title, current, message=message, anime_id=anime.id), on_score)

    @work(group="mal-track", exit_on_error=False)
    async def mal_rate(self, anime: MalAnime, score: int) -> None:
        try:
            async with self._mal_lock:
                anime.list_status = await self.session.mal.update_list_status(anime.id, score=score)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        for screen in self.screen_stack:
            if isinstance(screen, AnimeScreen) and screen.mal is not None and screen.mal.id == anime.id:
                screen.set_mal(anime)
        self.notify(
            t("MyAnimeList updated: {summary}", summary=mal.describe(anime, with_status=True).plain), markup=False
        )

    def open_mal_anime(self, anime: MalAnime) -> None:
        """Open a MAL entry from any screen: the linked title page on top, otherwise search it from the home screen."""
        link = mal_links.find_by_mal_id(anime.id, self.session.settings.extractor_name)
        if link:
            try:
                item = mal_links.restore(link, self.session.http_kwargs)
            except Exception:
                logger.warning("broken MAL link %r", link, exc_info=True)
            else:
                self.push_screen(AnimeScreen(item, link["extractor_name"]))
                return
        self.go_home()
        self._home.open_mal_anime(anime)

    def go_home(self) -> None:
        while len(self.screen_stack) > _HOME_DEPTH:
            self.pop_screen()

    def show_home_tab(self, tab: str) -> None:
        self.go_home()
        self._home.action_show_tab(tab)

    # --- settings ------------------------------------------------------------

    def action_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            return

        def on_close(changed: set[str] | None) -> None:
            self.sub_title = self.session.settings.extractor_name
            changed = changed or set()
            for screen in self.screen_stack:
                if isinstance(screen, AnimeScreen):
                    screen.filter_episodes()
            if "language" in changed:
                self.apply_language()
            elif changed - {"mal"}:
                self._home.reset()
            elif changed:
                self._home.reset_mal()

        self.push_screen(SettingsScreen(), on_close)

    def apply_language(self) -> None:
        """Rebuild screens (texts are rendered at compose time), the footer translates bindings as it is redrawn."""
        set_language(self.session.settings.language)
        self.go_home()
        self._home = HomeScreen()
        self.switch_screen(self._home)
        self.refresh_bindings()

    @work(exit_on_error=False)
    async def check_updates(self) -> None:
        from anihub.common.updater import check_for_updates  # noqa: PLC0415 (looked up when called: tests replace it)

        result = await check_for_updates(proxy=self.session.proxy)
        if result["anihub"]["is_outdated"]:
            self._new_version = result["anihub"]["latest_version"]
            self.refresh_bindings()
        # mypy types the values of a TypedDict as object: here every one is a package
        packages = cast("Iterable[TPACKAGE]", result.values())
        outdated = [
            f"{info['package_name']} {info['current_version']} → {info['latest_version']}"
            for info in packages
            if info["is_outdated"]
        ]
        if outdated:
            # F9 stays in the footer after the notification is gone
            hint = t("F9 — update now") if self._new_version else t("Run `anihub update` to upgrade")
            self.notify("\n".join(outdated) + "\n" + hint, title=t("Update available"), timeout=20)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:  # noqa: ARG002 (Textual's signature)
        if action == "update":
            return self._new_version is not None
        return True

    def action_update(self) -> None:
        """Close the app and install the new release in the terminal (`anihub update`)."""
        if self._new_version is None:
            return
        question = t(
            "Update anihub {current} → {latest}? anihub will close and the update will run in the terminal.",
            current=anihub.__version__,
            latest=self._new_version,
        )

        def answer(confirmed: bool | None) -> None:  # noqa: FBT001 (the result of the screen)
            if confirmed:
                self.exit(EXIT_UPDATE)

        self.push_screen(ConfirmScreen(question, yes=t("Update"), no=t("Later")), answer)
