"""MyAnimeList profiles: own and other users' (friends), and other users' anime lists."""

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING, Any, ClassVar

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Select, Static

from anicli.common import mal_profile
from anicli.common.mal import STATUSES, MalAnime, MalError

from .. import mal, posters
from ..i18n import localized_bindings, t
from ..widgets import FilterSelect, ItemList, Navigate, PlayerBar, stop_loading
from .mal_dialogs import UserNameScreen

if TYPE_CHECKING:
    from anicli.common.mal_profile import Favorite, Friend, Profile, Stats, Update

    from ..app import AnicliApp

# MAL site colors of the list statuses (manga: reading, plan to read)
STATUS_COLORS = {
    "watching": "#2db039",
    "reading": "#2db039",
    "completed": "#26448f",
    "on_hold": "#f9d457",
    "dropped": "#a12f31",
    "plan_to_watch": "#c3c3c3",
    "plan_to_read": "#c3c3c3",
}
BAR_WIDTH = 36
USER_LIST_ALL = "all"
FAVORITE_KINDS = {"anime": "Anime", "manga": "Manga", "character": "Character", "person": "Person"}


def _bar(stats: Stats, width: int = BAR_WIDTH) -> Text:
    """The colored status bar of the site: a part per status, as wide as its share."""
    total = sum(s.count for s in stats.statuses)
    bar = Text()
    if not total:
        return bar.append("░" * width, style="dim")
    used = 0
    for i, status in enumerate(stats.statuses):
        cells = width - used if i == len(stats.statuses) - 1 else round(width * status.count / total)
        cells = max(0, min(cells, width - used))
        bar.append("█" * cells, style=STATUS_COLORS.get(status.status, ""))
        used += cells
    return bar


def stats_view(title: str, stats: Stats) -> Group:
    score = Table.grid(expand=True)
    score.add_column()
    score.add_column(justify="right")
    score.add_row(
        Text.assemble((f"{t('Days')}: ", "dim"), (stats.days, "bold")),
        Text.assemble((f"{t('Mean Score')}: ", "dim"), (stats.mean_score, "bold")),
    )
    counts = Table.grid(expand=True, padding=(0, 1))
    counts.add_column()
    counts.add_column(justify="right")
    counts.add_column(width=2)
    counts.add_column(style="dim")
    counts.add_column(justify="right")
    rows = max(len(stats.statuses), len(stats.data))
    for i in range(rows):
        status = stats.statuses[i] if i < len(stats.statuses) else None
        data = stats.data[i] if i < len(stats.data) else None
        counts.add_row(
            Text.assemble(("● ", STATUS_COLORS.get(status.status, "")), t(status.label)) if status else "",
            str(status.count) if status else "",
            "",
            t(data[0]) if data else "",
            data[1] if data else "",
        )
    return Group(Text(title, style="bold underline"), score, _bar(stats), counts)


def _update_label(update: Update) -> Text:
    label = Text(update.title, style="bold")
    label.append("\n● ", style=STATUS_COLORS.get(update.status, ""))
    label.append(update.details, style="dim")
    if update.ago:
        label.append(f" · {update.ago}", style="dim")
    return label


def _favorite_label(favorite: Favorite) -> Text:
    label = Text(f"{t(FAVORITE_KINDS[favorite.kind])}: ", style="dim")
    label.append(favorite.title, style="bold" if favorite.anime_id else "")
    if favorite.details:
        label.append(f" · {favorite.details}", style="dim")
    return label


def _friend_label(friend: Friend) -> Text:
    label = Text(friend.name, style="bold")
    if friend.last_online:
        label.append(f"\n{friend.last_online}", style="dim")
    return label


class _AnimeActions:
    """Open a MAL title by id and add it to Plan to Watch: shared by the profile and the user list."""

    app: AnicliApp

    @work(group="mal-open", exclusive=True)
    async def open_anime(self, anime_id: int) -> None:
        try:
            anime = await self.app.session.mal.get_anime(anime_id)
        except MalError as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        self.app.open_mal_anime(anime)

    @work(group="mal-update")
    async def plan_to_watch(self, anime_id: int) -> None:
        try:
            anime, added = await mal.plan_to_watch(self.app.session, anime_id)
        except MalError as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        self.planned(anime)
        if added:
            self.app.notify(t("«{title}» added to Plan to Watch", title=anime.title), markup=False)
        else:
            status = anime.list_status.status if anime.list_status else None
            self.app.notify(
                t("«{title}» is in your list already: {status}", title=anime.title, status=mal.status_label(status)),
                markup=False,
            )

    def planned(self, anime: MalAnime) -> None:
        """The own list entry of the title is known now (added or there already)."""


@localized_bindings
class ProfileScreen(_AnimeActions, Screen[None]):
    """A MAL profile like on the site: avatar, statistics, last updates, favorites, friends."""

    app: AnicliApp
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("l", "anime_list", "Anime list"),
        Binding("a", "plan_to_watch", "To Plan to Watch"),
        Binding("u", "open_user", "Other user"),
        Binding("b", "browser", "In browser"),
        Binding("r,f5", "refresh", "Refresh"),
    ]
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (110, "-wide")]
    # left/right between the lists
    LISTS = ("profile-friends", "profile-updates", "profile-favorites")

    def __init__(self, name: str) -> None:
        super().__init__()
        self.user_name = name
        self.profile: Profile | None = None

    @property
    def own(self) -> bool:
        return self.user_name.casefold() == self.app.session.mal.username.casefold()

    def compose(self) -> ComposeResult:
        yield Header()
        settings = self.app.session.settings
        with Horizontal(id="profile-page"):
            with Vertical(id="profile-side"):
                if settings.posters and (avatar := posters.make_widget(id="avatar")):
                    yield avatar
                yield Static(id="profile-status")
                yield ItemList(id="profile-friends", empty_text=t("No friends"))
            with Vertical(id="profile-main"):
                with VerticalScroll(id="profile-info"):
                    yield Static(id="profile-about")
                    with Horizontal(id="profile-stats"):
                        yield Static(id="stats-anime")
                        yield Static(id="stats-manga")
                with Horizontal(id="profile-lists"):
                    yield ItemList(id="profile-updates", empty_text=t("No updates yet"))
                    yield ItemList(id="profile-favorites", empty_text=t("No favorites yet"))
        yield PlayerBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = t("{user}'s profile", user=self.user_name)
        self.query_one("#profile-friends").border_title = t("Friends")
        self.query_one("#profile-updates").border_title = t("Last Anime Updates")
        self.query_one("#profile-favorites").border_title = t("Favorites")
        self.load()

    @work(exclusive=True, group="profile")
    async def load(self) -> None:
        lists = [self.query_one(f"#{i}", ItemList) for i in self.LISTS]
        for widget in lists:
            widget.loading = True
        try:
            self.profile = profile = await mal_profile.load(self.app.session.mal, self.user_name)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        finally:
            for widget in lists:
                stop_loading(widget)
        self.user_name = profile.name
        status = Text(profile.name, style="bold")
        for title, value in profile.status:
            status.append(f"\n{t(title)}: ", style="dim")
            status.append(value)
        self.query_one("#profile-status", Static).update(status)
        self.query_one("#profile-about", Static).update(
            Text(profile.about, style="dim") if profile.about else Text(t("No biography yet."), style="dim italic")
        )
        self.query_one("#stats-anime", Static).update(stats_view(t("Anime Stats"), profile.anime))
        self.query_one("#stats-manga", Static).update(stats_view(t("Manga Stats"), profile.manga))
        friends, updates, favorites = lists
        friends.set_items(profile.friends, [_friend_label(f) for f in profile.friends])
        friends.border_title = t("Friends: {count}", count=len(profile.friends))
        updates.set_items(profile.updates, [_update_label(u) for u in profile.updates])
        favorites.set_items(profile.favorites, [_favorite_label(f) for f in profile.favorites])
        if self.focused is None:
            (updates if profile.updates else friends).focus()
        if profile.avatar and self.query("#avatar"):
            self.load_avatar(profile.avatar)

    @work(exclusive=True, group="avatar")
    async def load_avatar(self, url: str) -> None:
        image = await posters.fetch(self.app.session.mal.http, url)
        if image is not None:
            self.query_one("#avatar").image = image  # type: ignore[attr-defined]

    # graphics drawn around Textual (sixel) under a dialog can scroll the terminal
    def on_screen_suspend(self) -> None:
        for avatar in self.query("#avatar"):
            avatar.display = False

    def on_screen_resume(self) -> None:
        for avatar in self.query("#avatar"):
            avatar.display = True

    @on(Navigate)
    def _navigate(self, event: Navigate) -> None:
        if event.direction not in ("left", "right") or event.control.id not in self.LISTS:
            return
        index = self.LISTS.index(event.control.id) + (1 if event.direction == "right" else -1)
        if 0 <= index < len(self.LISTS):
            self.query_one(f"#{self.LISTS[index]}").focus()

    @on(OptionList.OptionSelected, "#profile-friends")
    def _friend_chosen(self, event: OptionList.OptionSelected) -> None:
        friends: ItemList[Friend] = self.query_one("#profile-friends", ItemList)
        if friends.items:
            self.app.push_screen(ProfileScreen(friends.items[event.option_index].name))

    @on(OptionList.OptionSelected, "#profile-updates, #profile-favorites")
    def _anime_chosen(self, event: OptionList.OptionSelected) -> None:
        if (anime_id := self._anime_id(event.option_list)) is not None:
            self.open_anime(anime_id)

    def _anime_id(self, widget: Any) -> int | None:
        current = widget.current if isinstance(widget, ItemList) else None
        return getattr(current, "anime_id", None)

    def action_plan_to_watch(self) -> None:
        if not self.own and (anime_id := self._anime_id(self.focused)) is not None:
            self.plan_to_watch(anime_id)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # own updates and favorites are in the own list already; on others' profiles only titles can be added
        if action == "plan_to_watch":
            return not self.own and self._anime_id(self.focused) is not None
        return True

    @on(OptionList.OptionHighlighted)
    def _highlighted(self) -> None:
        self.refresh_bindings()

    def on_descendant_focus(self) -> None:
        self.refresh_bindings()

    def action_anime_list(self) -> None:
        if self.own:
            self.app.show_home_tab("mal")
        else:
            self.app.push_screen(UserListScreen(self.user_name))

    def action_open_user(self) -> None:
        def on_name(name: str | None) -> None:
            if name:
                self.app.push_screen(ProfileScreen(name))

        self.app.push_screen(UserNameScreen(), on_name)

    def action_browser(self) -> None:
        webbrowser.open(mal_profile.Profile(self.user_name).url)

    def action_refresh(self) -> None:
        self.load()

    def action_back(self) -> None:
        self.dismiss()


def _user_entry_label(anime: MalAnime) -> Text:
    label = mal.describe(anime, with_status=True)
    mine = anime.my_list_status
    if mine is not None:
        label.append("\n" + t("in your list: {status}", status=mal.status_label(mine.status)), style="dim")
    else:
        label.append("\n" + t("not in your list: a — to Plan to Watch"), style="dim italic")
    return label


@localized_bindings
class UserListScreen(_AnimeActions, Screen[None]):
    """Another user's anime list: their status, episodes and score, and whether the title is in yours."""

    app: AnicliApp
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("a", "plan_to_watch", "To Plan to Watch"),
        Binding("r,f5", "refresh", "Refresh"),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.user_name = name
        # the whole list, loaded once: the status filter is applied here
        self.items: list[MalAnime] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="user-list-page"):
            yield FilterSelect(
                [(t("All"), USER_LIST_ALL), *((mal.status_label(s), s) for s in STATUSES)],
                value=USER_LIST_ALL,
                allow_blank=False,
                id="user-list-filter",
            )
            yield ItemList(id="user-list", empty_text=t("No titles with this status"))
        yield PlayerBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = t("{user}'s anime list", user=self.user_name)
        self.load()

    @on(Select.Changed, "#user-list-filter")
    def _filter_changed(self) -> None:
        if self.items is not None:
            self._show()

    @work(exclusive=True, group="user-list")
    async def load(self) -> None:
        results: ItemList[MalAnime] = self.query_one("#user-list", ItemList)
        results.loading = True
        try:
            self.items = await self.app.session.mal.user_list(user=self.user_name)
        except MalError as e:
            # private lists answer 403
            results.empty_text = t("The list is not available: {error}", error=e)
            results.set_items([])
            return
        finally:
            stop_loading(results)
        self._show()
        results.focus()

    def _show(self) -> None:
        results: ItemList[MalAnime] = self.query_one("#user-list", ItemList)
        status = str(self.query_one("#user-list-filter", Select).value)
        everything = self.items or []
        items = [a for a in everything if status == USER_LIST_ALL or (a.list_status and a.list_status.status == status)]
        results.empty_text = t("No titles with this status")
        results.set_items(items, [_user_entry_label(a) for a in items])
        shown = len(items)
        results.border_title = (
            f"{self.user_name} · {shown}"
            if shown == len(everything)
            else f"{self.user_name} · {shown} / {len(everything)}"
        )

    @on(Navigate)
    def _navigate(self, event: Navigate) -> None:
        if event.control.id == "user-list" and event.direction == "up":
            self.query_one("#user-list-filter").focus()
        elif event.control.id == "user-list-filter" and event.direction == "down":
            self.query_one("#user-list").focus()

    @on(OptionList.OptionSelected, "#user-list")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        results: ItemList[MalAnime] = self.query_one("#user-list", ItemList)
        if results.items:
            self.app.open_mal_anime(results.items[event.option_index])

    def action_plan_to_watch(self) -> None:
        current = self.query_one("#user-list", ItemList).current
        if current is not None:
            self.plan_to_watch(current.id)

    def planned(self, anime: MalAnime) -> None:
        results: ItemList[MalAnime] = self.query_one("#user-list", ItemList)
        for index, entry in enumerate(results.items):
            if entry.id == anime.id:
                entry.my_list_status = anime.my_list_status or anime.list_status
                results.replace_option_prompt_at_index(index, _user_entry_label(entry))

    def action_refresh(self) -> None:
        self.load()

    def action_back(self) -> None:
        self.dismiss()
