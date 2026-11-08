from __future__ import annotations

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
    """Settings form, saved as the fields change: no Save button.

    Changes that rebuild the session (network, source) or the screens (language) are applied on closing,
    the dialog dismisses with names of their groups: "language", "extractor", "network", "mal".
    """

    app: AnicliApp
    BINDINGS = [
        Binding("escape", "close", "Close"),
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
            if not posters.AVAILABLE:
                yield Static(
                    t("The terminal draws no images (kitty graphics protocol or sixel): posters are not shown"),
                    classes="dialog-hint",
                    markup=False,
                )
            yield Checkbox(t("Subtitles first in the dub list"), s.subtitles_first, id="subtitles-first")
            yield Checkbox(
                t("Show leaked episodes (12 when only 1-6 are out)"),
                s.show_leaked,
                id="show-leaked",
            )
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
                yield Button(t("Close"), variant="primary", id="close")

    def on_mount(self) -> None:
        s = self.app.session.settings
        self._toggle_custom_player(s.player)
        self._mal_logged_in = self.app.session.mal.logged_in
        # applied on closing if changed
        self._language, self._extractor, self._network = s.language, s.extractor_name, (s.proxy, s.timeout)
        self._save_failed = self._dismissing = False
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
        if event.button.id == "mal-login":
            self._mal_login()
        else:
            await self.action_close()

    @on(Checkbox.Changed)
    @on(Select.Changed)
    @on(Input.Changed)
    def _field_changed(self) -> None:
        self._autosave()

    def _valid(self, selector: str) -> Input | None:
        field = self.query_one(selector, Input)
        if field.validators and not field.validate(field.value).is_valid:  # type: ignore[union-attr]
            return None
        return field

    def _autosave(self) -> None:
        """Copy the fields to the settings and save them: fields with errors keep the previous values."""
        if self._dismissing:
            return
        s = self.app.session.settings
        before = s.persistent()

        s.language = str(self.query_one("#language", Select).value)
        s.posters = self.query_one("#posters", Checkbox).value
        s.subtitles_first = self.query_one("#subtitles-first", Checkbox).value
        s.show_leaked = self.query_one("#show-leaked", Checkbox).value
        s.extractor_name = str(self.query_one("#extractor", Select).value)
        s.quality = int(self.query_one("#quality", Select).value)  # type: ignore[arg-type]
        player = str(self.query_one("#player", Select).value)
        s.player_command = self.query_one("#player-command", Input).value.strip()
        if player != "custom" or s.player_command:
            s.player = player
        s.player_args = self.query_one("#player-args", Input).value.strip()
        s.stream_proxy = self.query_one("#stream-proxy", Checkbox).value
        if field := self._valid("#m3u-size"):
            s.m3u_size = int(field.value)
        if field := self._valid("#proxy"):
            s.proxy = field.value.strip() or None
        if field := self._valid("#timeout"):
            s.timeout = float(field.value)
        s.mal_client_id = self.query_one("#mal-client-id", Input).value.strip()
        s.mal_auto_update = self.query_one("#mal-auto", Checkbox).value

        if s.persistent() == before:
            return
        try:
            s.save()
        except OSError as e:
            if not self._save_failed:
                self._save_failed = True
                self.notify(t("Settings applied but not saved: {error}", error=e), severity="warning", markup=False)

    async def action_close(self) -> None:
        if self._dismissing:
            return
        self._autosave()
        self._dismissing = True
        session = self.app.session
        s = session.settings
        changed: set[str] = set()

        if (s.proxy, s.timeout) != self._network:
            await session.set_network(proxy=s.proxy, timeout=s.timeout)
            changed.add("network")
        if s.extractor_name != self._extractor:
            session.set_extractor(s.extractor_name)
            changed.add("extractor")
        if session.mal.logged_in and s.mal_client_id != session.mal.client_id:
            # tokens belong to the old API client
            session.mal.logout()
        session.mal.client_id = s.mal_client_id
        if session.mal.logged_in != self._mal_logged_in:
            changed.add("mal")
        if s.language != self._language:
            changed.add("language")

        invalid = [i for i in self.query(Input) if i.validators and not i.validate(i.value).is_valid]  # type: ignore[union-attr]
        if invalid or (str(self.query_one("#player", Select).value) == "custom" and not s.player_command):
            self.notify(t("Fields with errors were not saved"), severity="warning", markup=False)
        self.dismiss(changed)
