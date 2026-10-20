from __future__ import annotations

import os
import platform
import webbrowser
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.validation import Number
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from anicli.common.mal import (
    APICONFIG_URL,
    REDIRECT_URI,
    SCORE_LABELS,
    AuthRequest,
    MalAnime,
    MalError,
    wait_for_redirect,
)

from ..i18n import localized_bindings, t
from ..mal import describe, score_label
from ..widgets import ItemList, Navigate, stop_loading

if TYPE_CHECKING:
    from ..app import AnicliApp


@localized_bindings
class ScoreScreen(ModalScreen[int | None]):
    """Pick a score 10..1 or remove it (0). Dismisses with None on cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, current: int = 0, *, message: str = "", anime_id: int | None = None) -> None:
        super().__init__()
        self.anime_title = title
        self.current = current
        # shown above the scores, eg: why the dialog was opened by itself
        self.message = message
        self.anime_id = anime_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(t("Rate «{title}»", title=self.anime_title), classes="dialog-title")
            if self.message:
                yield Static(self.message, classes="dialog-message", markup=False)
            options = [Option(score_label(score), id=str(score)) for score in sorted(SCORE_LABELS, reverse=True)]
            options.append(Option(Text(t("No score"), style="dim"), id="0"))
            yield OptionList(*options, id="scores")
            hint = t("Digits 1-9, 0 for 10")
            yield Static(f"{hint} · {t('Esc — later')}" if self.message else hint, classes="dialog-hint")

    def on_mount(self) -> None:
        scores = self.query_one(OptionList)
        scores.highlighted = scores.get_option_index(str(self.current)) if self.current else 0
        scores.focus()

    def on_key(self, event: events.Key) -> None:
        if event.character and event.character.isdigit():
            event.stop()
            self.dismiss(int(event.character) or 10)

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id or 0))

    def action_cancel(self) -> None:
        self.dismiss(None)


@localized_bindings
class EpisodesScreen(ModalScreen[int | None]):
    """Edit the watched episodes count."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, current: int, total: int) -> None:
        super().__init__()
        self.anime_title = title
        self.current = current
        self.total = total

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(t("Watched episodes of «{title}»", title=self.anime_title), classes="dialog-title")
            yield Input(
                str(self.current),
                type="integer",
                placeholder=t("of {total}", total=self.total) if self.total else "",
                validators=[Number(minimum=0, maximum=self.total or None)],
                id="episodes-count",
            )
            if self.total:
                yield Static(t("of {total}, Enter to save", total=self.total), classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        if event.validation_result and not event.validation_result.is_valid:
            self.notify(t("Fix the highlighted fields"), severity="error", markup=False)
            return
        self.dismiss(int(event.value or 0))

    def action_cancel(self) -> None:
        self.dismiss(None)


@localized_bindings
class MalSearchScreen(ModalScreen[MalAnime | None]):
    """Choose the MAL entry of the title manually."""

    app: AnicliApp
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, queries: list[str]) -> None:
        super().__init__()
        self.queries = queries

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="mal-search"):
            yield Label(t("Find the title on MyAnimeList"), classes="dialog-title")
            yield Input(
                self.queries[0] if self.queries else "",
                placeholder=t("Original or english title, Enter to search"),
                id="mal-query",
            )
            yield ItemList(id="mal-candidates", empty_text=t("Type a query and press Enter"))

    def on_mount(self) -> None:
        self.query_one(ItemList).set_items([])
        self.query_one(Input).focus()
        if self.queries:
            self.search(self.queries[0])

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        if query := event.value.strip():
            self.search(query)

    @on(Navigate)
    def _navigate(self, event: Navigate) -> None:
        if event.direction == "up":
            self.query_one(Input).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down" and self.focused is self.query_one(Input):
            event.stop()
            self.query_one(ItemList).focus()

    @work(exclusive=True)
    async def search(self, query: str) -> None:
        results: ItemList[MalAnime] = self.query_one(ItemList)
        results.loading = True
        try:
            found = await self.app.session.mal.search(query)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        results.empty_text = (
            t("Nothing found for «{query}»", query=query) if len(query) >= 3 else t("At least 3 characters")
        )
        results.set_items(found, [describe(i, with_status=True) for i in found])
        if found:
            results.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        results: ItemList[MalAnime] = self.query_one(ItemList)
        if results.items:
            self.dismiss(results.items[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


def _can_open_browser() -> bool:
    # without a graphical session webbrowser falls back to console browsers which break the TUI
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@localized_bindings
class MalLoginScreen(ModalScreen[str | None]):
    """OAuth login: the browser redirects to the local server, or the user pastes the redirect url.

    Dismisses with the user name, None on cancel.
    """

    app: AnicliApp
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.auth: AuthRequest = AuthRequest("")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(t("Log in to MyAnimeList"), classes="dialog-title")
            yield Static(id="mal-login-help", markup=False, classes="dialog-message")
            yield Static(id="mal-login-url", markup=False, classes="dialog-message")
            yield Label(t("Or paste the address of the page you were redirected to"))
            yield Input(placeholder=f"{REDIRECT_URI}?code=…", id="mal-redirect")
            with Horizontal(classes="dialog-buttons"):
                yield Button(t("Open browser"), id="open")
                yield Button(t("Copy link"), id="copy")
                yield Button(t("Cancel"), id="cancel")

    def on_mount(self) -> None:
        self.auth = self.app.session.mal.new_auth_request()
        self.query_one("#mal-login-url", Static).update(Text(self.auth.url, style="link " + self.auth.url))
        opened = _can_open_browser() and self._open_browser()
        self.query_one("#mal-login-help", Static).update(
            t("Allow access in the browser, it will return you here automatically.")
            if opened
            else t("Open this link in a browser and allow access:")
        )
        self.query_one("#open" if not opened else "#mal-redirect").focus()
        self.wait_redirect()

    def _open_browser(self) -> bool:
        try:
            return webbrowser.open(self.auth.url)
        except webbrowser.Error:
            return False

    @work(exclusive=True, group="mal-redirect")
    async def wait_redirect(self) -> None:
        try:
            code = await wait_for_redirect(self.auth)
        except OSError:
            self.notify(
                t("Port of {url} is busy: paste the redirect address manually", url=REDIRECT_URI),
                severity="warning",
                markup=False,
            )
            return
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        self.finish(code)

    @on(Input.Submitted, "#mal-redirect")
    def _pasted(self, event: Input.Submitted) -> None:
        try:
            code = self.auth.parse_redirect(event.value)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        self.finish(code)

    @work(exclusive=True, group="mal-login")
    async def finish(self, code: str) -> None:
        try:
            username = await self.app.session.mal.login(self.auth, code)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        self.workers.cancel_group(self, "mal-redirect")
        self.dismiss(username)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            if not self._open_browser():
                self.notify(t("Could not open the browser, copy the link"), severity="warning", markup=False)
        elif event.button.id == "copy":
            self.app.copy_to_clipboard(self.auth.url)
            self.notify(t("Link copied"))
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.workers.cancel_group(self, "mal-redirect")
        self.dismiss(None)


def client_id_help() -> str:
    return t(
        "Create an API client at {url}: App Type «other», App Redirect URL {redirect}. Paste its Client ID here.",
        url=APICONFIG_URL,
        redirect=REDIRECT_URI,
    )
