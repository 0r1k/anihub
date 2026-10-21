from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.validation import Function, Number
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from anicli.common.extractors import get_extractor_modules
from anicli.common.players import PLAYERS
from anicli.common.utils import validate_proxy_url

from .. import posters
from ..i18n import LANGUAGES, localized_bindings, t
from ..session import QUALITIES
from .mal_dialogs import MalLoginScreen, client_id_help

if TYPE_CHECKING:
    from ..app import AnicliApp

PLAYER_LABELS = {"mpv": "mpv", "vlc": "VLC", "custom": "Other (command)"}
POSTER_RENDER_LABELS = {
    "auto": "Auto (no sixel)",
    "graphics": "Graphics (kitty protocol / sixel)",
    "blocks": "Colored blocks",
}


def _is_proxy(value: str) -> bool:
    if not value.strip():
        return True
    try:
        validate_proxy_url(value.strip())
    except ValueError:
        return False
    return True


class NavSelect(Select):
    """Select that opens on Enter/Space only: up/down move focus between fields like in the rest of the form."""

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
    ]


class FormScroll(VerticalScroll, can_focus=False):
    """Scrollable form: arrows move focus between fields (the focused field is scrolled into view)."""

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
    ]


@localized_bindings
class SettingsScreen(ModalScreen[set[str]]):
    """Settings form. Dismisses with names of changed groups: "language", "extractor", "network", "mal"."""

    app: AnicliApp
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("up", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("left", "button_focus(-1)", show=False),
        Binding("right", "button_focus(1)", show=False),
    ]

    def compose(self) -> ComposeResult:
        s = self.app.session.settings
        qualities = sorted({*QUALITIES, s.quality}, reverse=True)
        with FormScroll(classes="dialog", id="settings"):
            yield Label(t("Settings"), classes="dialog-title")
            yield Label(t("Language"))
            yield NavSelect(
                [(name, code) for code, name in LANGUAGES.items()], value=s.language, allow_blank=False, id="language"
            )
            yield Checkbox(t("Show posters on the title page"), s.posters, id="posters")
            yield Label(t("Poster rendering"))
            yield NavSelect(
                [(t(POSTER_RENDER_LABELS[mode]), mode) for mode in posters.RENDER_MODES],
                value=s.poster_render if s.poster_render in posters.RENDER_MODES else "graphics",
                allow_blank=False,
                id="poster-render",
            )
            yield Checkbox(t("Subtitles first in the dub list"), s.subtitles_first, id="subtitles-first")
            yield Label(t("Source"))
            yield NavSelect(
                [(name, name) for name in get_extractor_modules()],
                value=s.extractor_name,
                allow_blank=False,
                id="extractor",
            )
            yield Label(t("Quality (or the closest available)"))
            yield NavSelect([(f"{q}p", q) for q in qualities], value=s.quality, allow_blank=False, id="quality")
            yield Label(t("Player"))
            yield NavSelect([(t(PLAYER_LABELS[p]), p) for p in PLAYERS], value=s.player, allow_blank=False, id="player")
            yield Label(t("Player command"), classes="custom-player")
            yield Input(
                s.player_command,
                placeholder="celluloid, haruna, /path/to/player",
                id="player-command",
                classes="custom-player",
            )
            yield Label(t("Extra player arguments"))
            yield Input(s.player_args, placeholder="--fullscreen", id="player-args")
            yield Checkbox(t("Load video through anihub (fast start, reconnects)"), s.stream_proxy, id="stream-proxy")
            yield Label(t("Episodes per player playlist"))
            yield Input(str(s.m3u_size), type="integer", validators=[Number(minimum=1, maximum=100)], id="m3u-size")
            yield Label(t("Proxy"))
            yield Input(
                s.proxy or "",
                placeholder="socks5://user:password@host:port",
                validators=[Function(_is_proxy, t("format: scheme://user:password@host:port"))],
                id="proxy",
            )
            yield Label(t("HTTP timeout, seconds"))
            yield Input(str(int(s.timeout)), type="number", validators=[Number(minimum=1)], id="timeout")
            yield Label("MyAnimeList", classes="dialog-section")
            yield Static(client_id_help(), classes="dialog-hint", markup=False)
            yield Label("Client ID")
            yield Input(s.mal_client_id, placeholder="0123456789abcdef0123456789abcdef", id="mal-client-id")
            yield Checkbox(t("Count watched episodes and update the status"), s.mal_auto_update, id="mal-auto")
            with Horizontal(id="mal-account"):
                yield Static(id="mal-account-label", markup=False)
                yield Button(id="mal-login")
            with Horizontal(classes="dialog-buttons"):
                yield Button(t("Save"), variant="primary", id="save")
                yield Button(t("Cancel"), id="cancel")

    def on_mount(self) -> None:
        self._toggle_custom_player(self.app.session.settings.player)
        self._mal_logged_in = self.app.session.mal.logged_in
        self._update_mal_account()
        self.query_one("#language").focus()

    def _update_mal_account(self) -> None:
        client = self.app.session.mal
        label = self.query_one("#mal-account-label", Static)
        button = self.query_one("#mal-login", Button)
        if client.logged_in:
            label.update(t("Logged in as {name}", name=client.username or "?"))
            button.label = t("Log out")
        else:
            label.update(t("Not logged in"))
            button.label = t("Log in")

    def _mal_login(self) -> None:
        client = self.app.session.mal
        if client.logged_in:
            client.logout()
            self._update_mal_account()
            return
        client_id = self.query_one("#mal-client-id", Input).value.strip()
        if not client_id:
            self.query_one("#mal-client-id").focus()
            self.notify(t("Enter the Client ID first"), severity="error", markup=False)
            return
        client.client_id = client_id

        def on_login(username: str | None) -> None:
            self._update_mal_account()
            if username:
                self.notify(t("Logged in as {name}", name=username), markup=False)

        self.app.push_screen(MalLoginScreen(), on_login)

    @on(Select.Changed, "#player")
    def _player_changed(self, event: Select.Changed) -> None:
        self._toggle_custom_player(str(event.value))

    def _toggle_custom_player(self, player: str) -> None:
        for widget in self.query(".custom-player"):
            widget.display = player == "custom"

    def action_button_focus(self, step: int) -> None:
        if isinstance(self.focused, Button):
            if step > 0:
                self.app.action_focus_next()
            else:
                self.app.action_focus_previous()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            await self.action_save()
        elif event.button.id == "mal-login":
            self._mal_login()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        session = self.app.session
        if not session.mal.logged_in:
            session.mal.client_id = session.settings.mal_client_id
        elif session.settings.mal_client_id != session.mal.client_id:
            # login is applied immediately: keep the client id its tokens belong to
            session.settings.mal_client_id = session.mal.client_id
            with contextlib.suppress(OSError):
                session.settings.save()
        self.dismiss({"mal"} if session.mal.logged_in != self._mal_logged_in else set())

    async def action_save(self) -> None:
        invalid = [i for i in self.query(Input) if i.validators and not i.validate(i.value).is_valid]
        if invalid:
            invalid[0].focus()
            self.notify(t("Fix the highlighted fields"), severity="error", markup=False)
            return

        player = str(self.query_one("#player", Select).value)
        player_command = self.query_one("#player-command", Input).value.strip()
        if player == "custom" and not player_command:
            self.query_one("#player-command").focus()
            self.notify(t("Enter the player command"), severity="error", markup=False)
            return

        session = self.app.session
        s = session.settings
        changed: set[str] = set()

        s.quality = int(self.query_one("#quality", Select).value)  # type: ignore[arg-type]
        s.player = player
        s.player_command = player_command
        s.player_args = self.query_one("#player-args", Input).value.strip()
        s.m3u_size = int(self.query_one("#m3u-size", Input).value)
        s.stream_proxy = self.query_one("#stream-proxy", Checkbox).value

        proxy = self.query_one("#proxy", Input).value.strip() or None
        timeout = float(self.query_one("#timeout", Input).value)
        if proxy != s.proxy or timeout != s.timeout:
            await session.set_network(proxy=proxy, timeout=timeout)
            changed.add("network")

        extractor = str(self.query_one("#extractor", Select).value)
        if extractor != s.extractor_name:
            session.set_extractor(extractor)
            changed.add("extractor")

        client_id = self.query_one("#mal-client-id", Input).value.strip()
        if client_id != s.mal_client_id:
            if session.mal.logged_in and client_id != session.mal.client_id:
                # tokens belong to the old API client
                session.mal.logout()
            s.mal_client_id = session.mal.client_id = client_id
        s.mal_auto_update = self.query_one("#mal-auto", Checkbox).value
        s.posters = self.query_one("#posters", Checkbox).value
        s.poster_render = str(self.query_one("#poster-render", Select).value)
        s.subtitles_first = self.query_one("#subtitles-first", Checkbox).value
        if session.mal.logged_in != self._mal_logged_in:
            changed.add("mal")

        language = str(self.query_one("#language", Select).value)
        if language != s.language:
            s.language = language
            changed.add("language")

        try:
            s.save()
        except OSError as e:
            self.notify(t("Settings applied but not saved: {error}", error=e), severity="warning", markup=False)
        self.dismiss(changed)
