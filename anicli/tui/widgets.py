from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any, ClassVar, Generic, TypeVar

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.dom import NoScreen
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import OptionList, ProgressBar, SelectionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from .i18n import localized_bindings, t

T = TypeVar("T")


def stop_loading(widget: Widget) -> None:
    """Hide the loading indicator in a worker `finally`.

    The screen can be closed while the worker waits (the worker is cancelled then): the widget has no screen
    anymore and Textual raises NoScreen, which would crash the app.
    """
    with contextlib.suppress(NoScreen):
        widget.loading = False


class Navigate(Message):
    """Arrow key left the list bounds: the screen decides where to move focus."""

    def __init__(self, source: Widget, direction: str) -> None:
        super().__init__()
        self.source = source
        self.direction = direction

    @property
    def control(self) -> Widget:
        return self.source


class _EdgeNavigation:
    """Lists do not wrap around: up on the first item and left/right post `Navigate` instead."""

    NAV_BINDINGS: ClassVar[list[Binding]] = [
        Binding("up,k", "cursor_up", show=False),
        Binding("down,j", "cursor_down", show=False),
        Binding("left", "navigate('left')", show=False),
        Binding("right", "navigate('right')", show=False),
    ]

    def action_cursor_up(self) -> None:
        if self.highlighted in (None, 0):  # type: ignore[attr-defined]
            self.post_message(Navigate(self, "up"))  # type: ignore[attr-defined, arg-type]
        else:
            self.highlighted -= 1  # type: ignore[attr-defined]

    def action_cursor_down(self) -> None:
        if self.highlighted is None:  # type: ignore[attr-defined]
            if self.option_count and not self.get_option_at_index(0).disabled:  # type: ignore[attr-defined]
                self.highlighted = 0
        elif self.highlighted < self.option_count - 1:  # type: ignore[attr-defined]
            self.highlighted += 1

    def action_navigate(self, direction: str) -> None:
        self.post_message(Navigate(self, direction))  # type: ignore[attr-defined, arg-type]


class ItemList(_EdgeNavigation, OptionList, Generic[T]):
    """OptionList that keeps python objects behind options."""

    BINDINGS = _EdgeNavigation.NAV_BINDINGS

    def __init__(self, *args: Any, empty_text: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.items: list[T] = []
        self.empty_text = empty_text if empty_text is not None else t("Empty")

    def set_items(self, items: Iterable[T], labels: Iterable[Any] | None = None) -> None:
        self.items = list(items)
        labels = list(labels) if labels is not None else [str(i) for i in self.items]
        self.clear_options()
        if self.items:
            self.add_options(labels)
            self.highlighted = 0
        else:
            self.add_option(Option(Text(self.empty_text, style="dim italic"), disabled=True))

    @property
    def current(self) -> T | None:
        if self.highlighted is None or not self.items:
            return None
        return self.items[self.highlighted]


@localized_bindings
class EpisodeList(_EdgeNavigation, SelectionList[int]):
    """Space toggles an episode, Enter chooses marked episodes (or the highlighted one).

    Watched episodes (MyAnimeList) have a lit checkbox, but stay unmarked: marks are for playback.
    """

    BINDINGS = [Binding("enter", "choose", "Choose"), *_EdgeNavigation.NAV_BINDINGS]
    COMPONENT_CLASSES: ClassVar[set[str]] = {"episode-list--button-watched"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.watched: set[int] = set()

    def set_watched(self, values: Iterable[int]) -> None:
        self.watched = set(values)
        self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        try:
            option = self.get_option_at_index(self.scroll_offset.y + y)
        except OptionDoesNotExist:
            return strip
        if option.value not in self.watched or option.value in self._selected:
            return strip
        segments = list(strip)
        if len(segments) < 2:  # noqa: PLR2004
            return strip
        # segments: button left side, button inner (the cross), button right side, prompt...
        inner = segments[1]
        color = self.get_component_rich_style("episode-list--button-watched").color
        segments[1] = Segment(inner.text, (inner.style or Style()) + Style(color=color))
        return Strip(segments, strip.cell_length)

    class Chosen(Message):
        def __init__(self, indices: list[int]) -> None:
            super().__init__()
            self.indices = indices

    async def _on_click(self, event: events.Click) -> None:
        """A click opens the clicked episode: marks are toggled by Space only."""
        # OptionList would toggle the mark (same action as Space)
        event.prevent_default()
        clicked = event.style.meta.get("option")
        if clicked is None or self.get_option_at_index(clicked).disabled:
            return
        self.highlighted = clicked
        self.post_message(self.Chosen([self.get_option_at_index(clicked).value]))

    def action_choose(self) -> None:
        indices = sorted(self.selected)
        if not indices and self.highlighted is not None:
            indices = [self.get_option_at_index(self.highlighted).value]
        if indices:
            self.post_message(self.Chosen(indices))


class PlayerBar(Horizontal):
    """Global playback status, driven by app reactives."""

    def compose(self) -> ComposeResult:
        yield Static(id="player-label", markup=False)
        yield ProgressBar(id="player-progress", show_eta=False)

    def on_mount(self) -> None:
        self.watch(self.app, "player_status", self._on_status)
        self.watch(self.app, "player_progress", self._on_progress)

    def _on_status(self, status: str) -> None:
        self.display = bool(status)
        self.query_one("#player-label", Static).update(status)

    def _on_progress(self, progress: tuple[int, int] | None) -> None:
        bar = self.query_one(ProgressBar)
        bar.display = progress is not None
        if progress:
            bar.update(progress=progress[0], total=progress[1])
