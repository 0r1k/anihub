from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.theme import Theme

from anicli.common import mal_links
from anicli.common.mal import MalAnime, MalAuthError, MalError
from anicli.common.players import is_installed

from . import logs, mal
from .i18n import localized_bindings, set_language, t
from .playback import ConfirmWatched, OnWatched, PlaybackError
from .screens.anime import AnimeScreen
from .screens.dialogs import ConfirmScreen
from .screens.home import HomeScreen
from .screens.mal_dialogs import ScoreScreen
from .screens.settings import SettingsScreen
from .session import Session


@localized_bindings
class AnicliApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "anihub"
    BINDINGS = [
        Binding("f2", "settings", "Settings"),
        Binding("x", "stop_playback", "Stop"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    player_status: reactive[str] = reactive("")
    player_progress: reactive[tuple[int, int] | None] = reactive(None)

    def __init__(self, session: Session, *, query: str | None = None, ongoing: bool = False) -> None:
        # library logs written to the terminal would break the layout
        logs.setup()
        # translate class bindings before this instance copies them
        set_language(session.settings.language)
        super().__init__()
        self.session = session
        self._home = HomeScreen(query=query, ongoing=ongoing)
        self._play_ids = itertools.count()
        self._current_play = -1
        # list updates read-modify-write the entry: run them one by one
        self._mal_lock = asyncio.Lock()

    def on_mount(self) -> None:
        self.sub_title = self.session.settings.extractor_name
        if self.session.settings.theme in self.available_themes:
            self.theme = self.session.settings.theme
        self.theme_changed_signal.subscribe(self, self._save_theme)
        self.push_screen(self._home)
        self.check_player()
        self.check_updates()

    def check_player(self) -> None:
        command = self.session.settings.player_executable
        if not is_installed(command):
            self.notify(
                t("Player not found: {command}. Choose another one in settings (F2)", command=command or "?"),
                severity="error",
                timeout=10,
                markup=False,
            )

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
            self.notify(str(e), severity="error", markup=False)
        except Exception as e:
            self.log.error(e)
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

    def mal_tracker(self, extractor_name: str, url: str) -> OnWatched | None:
        """Playback callback counting watched episodes of the title on MyAnimeList."""
        if not self.session.mal_tracking:
            return None
        return lambda episode: self.mal_track(extractor_name, url, episode)

    def mal_confirm_watched(self, extractor_name: str, url: str) -> ConfirmWatched | None:
        if not self.session.mal_tracking:
            return None

        async def confirm(title: str) -> bool:
            if mal_links.get_by_url(extractor_name, url) is None:
                return False
            question = t("Mark «{title}» as watched on MyAnimeList?", title=title)
            return bool(await self.push_screen_wait(ConfirmScreen(question)))

        return confirm

    @work(group="mal-track", exit_on_error=False)
    async def mal_track(self, extractor_name: str, url: str, episode: int) -> None:
        link = mal_links.get_by_url(extractor_name, url)
        if link is None:
            self.notify(
                t("The episode is not counted on MyAnimeList: link the title first (l on the title page)"),
                severity="warning",
                markup=False,
            )
            return
        try:
            async with self._mal_lock:
                anime, fields = await mal.episode_watched(self.session, link["mal_id"], int(episode))
        except (TypeError, ValueError):
            return
        except MalAuthError as e:
            self.notify(t("{error}. Log in again in settings (F2)", error=e), severity="error", markup=False)
            return
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        for screen in self.screen_stack:
            if isinstance(screen, AnimeScreen) and screen.matches(extractor_name, url):
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

    # --- settings ------------------------------------------------------------

    def action_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            return

        def on_close(changed: set[str] | None) -> None:
            self.sub_title = self.session.settings.extractor_name
            changed = changed or set()
            if "language" in changed:
                self.apply_language()
            elif changed - {"mal"}:
                self._home.reset()
            elif changed:
                self._home.reset_mal()

        self.push_screen(SettingsScreen(), on_close)

    def apply_language(self) -> None:
        """Translate bindings and rebuild screens: texts are rendered at compose time."""
        set_language(self.session.settings.language)
        self._bindings = self._merged_bindings.copy()  # type: ignore[union-attr]
        while len(self.screen_stack) > 2:
            self.pop_screen()
        self._home = HomeScreen()
        self.switch_screen(self._home)
        self.refresh_bindings()

    @work(exit_on_error=False)
    async def check_updates(self) -> None:
        from anicli.common.updater import check_for_updates

        result = await check_for_updates()
        outdated = [
            f"{info['package_name']} {info['current_version']} → {info['latest_version']}"
            for info in result.values()
            if info["is_outdated"]
        ]
        if outdated:
            self.notify(
                "\n".join(outdated) + "\n" + t("Run `anihub update` to upgrade"),
                title=t("Update available"),
                timeout=8,
            )
