from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .. import subtitles
from ..i18n import localized_bindings, t
from ..widgets import ItemList

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@localized_bindings
class SitesScreen(ModalScreen[subtitles.ForeignTitle | None]):
    """The title on the other sites: rows fill in as the sites answer. Dismisses with the chosen site or None.

    The search runs on the title page (`update` is called as each site answers), the players of the episode
    are loaded here for the found sites.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, searches: list[subtitles.SiteSearch], ordinal: int) -> None:
        super().__init__()
        self.title_text = title
        self.searches = searches
        self.ordinal = ordinal
        # video hostings of the episode by site name: missing - loading
        self.players: dict[str, list[str]] = {}
        self._frame = 0

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="sites-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(id="sites-progress", classes="dialog-hint")
            sites: ItemList[subtitles.SiteSearch] = ItemList(
                *(Option(self._label(s), id=s.name, disabled=True) for s in self.searches), id="sites"
            )
            sites.items = self.searches
            yield sites
            yield Static(t("Enter — open the title on this site"), classes="dialog-hint", markup=False)

    def on_mount(self) -> None:
        for search in self.searches:
            self.update(search)
        self.set_interval(0.1, self._spin)
        self.query_one(ItemList).focus()

    def update(self, search: subtitles.SiteSearch) -> None:
        """The site answered: show the result, load the players of the episode."""
        if not self.is_mounted:
            return
        self._refresh_row(search)
        self._update_progress()
        if search.site is None:
            return
        sites = self.query_one(ItemList)
        index = sites.get_option_index(search.name)
        sites.enable_option_at_index(index)
        if sites.highlighted is None or sites.get_option_at_index(sites.highlighted).disabled:
            sites.highlighted = index
        if search.name not in self.players:
            self.load_players(search)

    def load_players(self, search: subtitles.SiteSearch) -> None:
        async def load() -> None:
            assert search.site is not None  # noqa: S101
            self.players[search.name] = await subtitles.site_players(search.site, self.ordinal)
            self._refresh_row(search)

        self.run_worker(load(), group=f"players-{search.name}")

    def _refresh_row(self, search: subtitles.SiteSearch) -> None:
        sites = self.query_one(ItemList)
        sites.replace_option_prompt_at_index(sites.get_option_index(search.name), self._label(search))

    def _spin(self) -> None:
        loading = [s for s in self.searches if s.state == "searching" or (s.site and s.name not in self.players)]
        if not loading:
            return
        self._frame = (self._frame + 1) % len(SPINNER)
        for search in loading:
            self._refresh_row(search)
        self._update_progress()

    def _update_progress(self) -> None:
        answered = sum(s.state != "searching" for s in self.searches)
        found = sum(s.site is not None for s in self.searches)
        if answered < len(self.searches):
            text = Text(f"{SPINNER[self._frame]} ", style="bold")
            text.append(
                t("Searching: {answered} of {total} sites answered", answered=answered, total=len(self.searches))
            )
        elif found:
            text = Text(t("Found on {found} of {total} sites", found=found, total=len(self.searches)))
        else:
            text = Text(t("The title is not found on other sites"))
        self.query_one("#sites-progress", Static).update(text)

    def _label(self, search: subtitles.SiteSearch) -> Text:
        spinner = SPINNER[self._frame]
        label = Text(search.name, style="bold" if search.site else "dim")
        if search.state == "searching":
            label.append(f" · {spinner} {t('searching…')}", style="dim")
            return label
        if search.state == "failed":
            reason = t("the site did not answer") if search.error == "TimeoutError" else search.error
            label.append(f" · {reason}", style="dim")
            return label
        if search.site is None:
            label.append(f" · {t('not found')}", style="dim")
            return label
        site = search.site
        label.append(f" «{site.item.title}»")
        details = [t("episodes: {count}", count=len(site.episodes))]
        if not site.exact:
            details.append(t("similar name, check the season"))
        if search.name not in self.players:
            details.append(t("episode {num}: {players}", num=self.ordinal, players=spinner))
        elif players := self.players[search.name]:
            details.append(t("episode {num}: {players}", num=self.ordinal, players=", ".join(players)))
        label.append("\n" + " · ".join(details), style="dim")
        return label

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if (site := self.searches[event.option_index].site) is not None:
            self.dismiss(site)

    def action_cancel(self) -> None:
        self.dismiss(None)
