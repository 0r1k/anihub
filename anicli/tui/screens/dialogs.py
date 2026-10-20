from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..i18n import localized_bindings, t


@localized_bindings
class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, message: str, yes: str | None = None, no: str | None = None) -> None:
        super().__init__()
        self.message = message
        self.yes, self.no = yes or t("Yes"), no or t("No")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message, classes="dialog-message", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button(f"{self.yes} (y)", variant="primary", id="yes")
                yield Button(f"{self.no} (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)
