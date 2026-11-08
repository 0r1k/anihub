from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, OptionList, Select, TabbedContent, TabPane, Tabs

from anicli.common import history, mal_links, ongoing_state
from anicli.common.mal import STATUSES, MalAnime, MalError
from anicli.common.mal_feed import RELATIONS, RETRY_AFTER, Feed, FeedItem

from .. import mal, ongoing, playback, watching
from ..i18n import localized_bindings, t
from ..widgets import FilterSelect, ItemList, Navigate, PlayerBar, stop_loading
from .anime import AnimeScreen
from .dialogs import ConfirmScreen
from .profile import ProfileScreen

if TYPE_CHECKING:
    from ..app import AnicliApp


HistoryItem = tuple[dict[str, Any], Any]
TAB_ORDER = ("search", "ongoing", "watching", "history", "mal", "feed")
TAB_CONTENT = {
    "search": "#query",
    "ongoing": "#ongoing-results",
    "watching": "#watching-results",
    "history": "#history-results",
    "mal": "#mal-results",
    "feed": "#feed-results",
}
# MAL tab filter: all statuses
MAL_ALL = "all"


def _format_time(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _history_label(entry: dict[str, Any], _item: Any) -> Text:
    label = Text(entry["data"]["title"], style="bold")
    details = [entry.get("extractor_name", "?")]
    if entry.get("episode") and entry["episode"] != "Unknown":
        details.append(entry["episode"])
    if entry.get("time"):
        details.append(_format_time(entry["time"]))
    label.append("\n" + " · ".join(details), style="dim")
    return label


@localized_bindings
class HistoryList(ItemList[HistoryItem]):
    BINDINGS = [Binding("d,delete", "delete", "Delete from history")]

    def action_delete(self) -> None:
        if (current := self.current) is None:
            return
        history.delete(current[0])
        self.screen.reload_history()  # type: ignore[attr-defined]


@localized_bindings
class ListSearch(Input):
    """Search in the loaded list: Esc clears the query, the second Esc goes back to the list."""

    BINDINGS = [Binding("escape", "clear_or_leave", "Clear", show=False)]

    def action_clear_or_leave(self) -> None:
        if self.value:
            self.value = ""
        else:
            self.post_message(Navigate(self, "down"))


@localized_bindings
class FeedList(ItemList[FeedItem]):
    BINDINGS = [
        Binding("a", "plan_to_watch", "To Plan to Watch"),
        Binding("d,delete", "hide", "Hide"),
        Binding("c", "mark_read", "Mark all as read"),
    ]

    def action_plan_to_watch(self) -> None:
        if (current := self.current) is not None:
            self.screen.feed_plan_to_watch(current)  # type: ignore[attr-defined]

    def action_hide(self) -> None:
        if (current := self.current) is not None:
            self.screen.feed_hide(current)  # type: ignore[attr-defined]

    def action_mark_read(self) -> None:
        self.screen.feed_mark_read()  # type: ignore[attr-defined]


def _added(seconds: float, added: float) -> str:
    """When the title was added to MAL: "5 h ago" for the last month, then the date."""
    if seconds < 30 * 24 * 3600:
        return ongoing.ago(seconds)
    return time.strftime("%Y-%m-%d", time.localtime(added))


def _feed_label(item: FeedItem, unread: bool, now: float) -> Text:
    text = Text()
    if unread:
        text.append(f" {t('NEW')} ", style="bold reverse")
        text.append(" ")
    text.append(item.anime.title, style="bold")
    if kind := " ".join(str(i) for i in (item.anime.media_type.upper(), item.anime.year) if i):
        text.append(f" · {kind}", style="dim")
    relation = t(RELATIONS.get(item.relation, item.relation.replace("_", " ").title()))
    text.append(f"\n{relation}: {item.source.title}", style="dim")
    if item.added:
        text.append(" · " + t("added to MyAnimeList {when}", when=_added(now - item.added, item.added)), style="dim")
    return text


def _links_by_mal_id() -> dict[int, dict[str, Any]]:
    """The latest link of every MAL entry, read once per list render."""
    links: dict[int, dict[str, Any]] = {}
    for link in mal_links.read():
        links.setdefault(link.get("mal_id"), link)
    return links


def _mal_label(anime: MalAnime, with_status: bool, link: dict[str, Any] | None) -> Text:
    label = mal.describe(anime, with_status=with_status)
    if link:
        label.append(f"\n↩ {link['extractor_name']}: {link['data']['title']}", style="dim")
    return label


@localized_bindings
class HomeScreen(Screen[None]):
    app: AnicliApp
    BINDINGS = [
        Binding("slash", "show_tab('search')", "Search"),
        Binding("o", "show_tab('ongoing')", "Ongoing"),
        Binding("w", "show_tab('watching')", "Watching"),
        Binding("h", "show_tab('history')", "History"),
        Binding("m", "show_tab('mal')", "MyAnimeList"),
        Binding("n", "show_tab('feed')", "Notifications"),
        Binding("p", "profile", "Profile"),
        Binding("r,f5", "refresh", "Refresh"),
        Binding("f", "find", "Find"),
        Binding("up", "arrow('up')", show=False),
        Binding("down", "arrow('down')", show=False),
    ]

    def __init__(self, *, query: str | None = None, ongoing: bool = False) -> None:
        super().__init__()
        self.initial_query = query
        self.initial_ongoing = ongoing
        self._ongoing_loaded = False
        # all ongoing titles of the site (recently updated first), the filter is applied on display
        self._ongoing: list[Any] = []
        # when a new episode of the title was noticed, by item url
        self._ongoing_updated: dict[str, float] = {}
        self._mal_loaded = False
        # the loaded MAL list (the status filter is applied by the API, the search on display)
        self._mal_items: list[MalAnime] = []
        self._watching_loaded = False
        # MAL entry being searched on the site from the MAL tab: linked when a result is opened
        self._mal_hint: MalAnime | None = None
        # related titles not in the list (the API has no MAL notifications), loaded at start for the unread count
        self._feed: Feed | None = None
        self._feed_items: list[FeedItem] = []
        self._feed_loaded = False

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane(t("Search"), id="search"):
                yield Input(placeholder=t("Anime title, Enter to search"), id="query")
                yield ItemList(id="search-results", empty_text=t("Type a query and press Enter"))
            with TabPane(t("Ongoing"), id="ongoing"):
                yield FilterSelect(
                    [(t("All"), ongoing.ALL), (t("Airing"), ongoing.AIRING), (t("Announced"), ongoing.ANNOUNCED)],
                    value=self._ongoing_filter_setting(),
                    allow_blank=False,
                    id="ongoing-filter",
                )
                yield ItemList(id="ongoing-results", empty_text=t("No ongoing (is the site down?)"))
            with TabPane(t("Watching"), id="watching"):
                yield ItemList(id="watching-results", empty_text=t("Log in to MyAnimeList in settings (F2)"))
            with TabPane(t("History"), id="history"):
                yield HistoryList(id="history-results", empty_text=t("History is empty"))
            with TabPane("MyAnimeList", id="mal"):
                with Horizontal(id="mal-controls"):
                    yield FilterSelect(
                        [(t("All"), MAL_ALL), *((mal.status_label(s), s) for s in STATUSES)],
                        value="watching",
                        allow_blank=False,
                        id="mal-filter",
                    )
                    yield ListSearch(
                        placeholder=t("Find in the list (f): any title, english or russian"), id="mal-query"
                    )
                    yield Button(t("Random"), id="mal-random")
                yield ItemList(id="mal-results", empty_text=t("Log in to MyAnimeList in settings (F2)"))
            with TabPane(t("Notifications"), id="feed"):
                yield FeedList(id="feed-results", empty_text=t("Log in to MyAnimeList in settings (F2)"))
        yield PlayerBar()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#search-results", ItemList).set_items([])
        if self.initial_query:
            self.query_one("#query", Input).value = self.initial_query
            self.search(self.initial_query)
        elif self.initial_ongoing:
            self.action_show_tab("ongoing")
            # textual focuses the tab bar on start: move into the list once the tab is shown
            self.call_after_refresh(self.focus_tab_content)
        else:
            self.query_one("#query", Input).focus()
        if self.app.session.mal.logged_in:
            self.load_feed()

    def on_screen_resume(self) -> None:
        active = self.query_one(TabbedContent).active
        if active == "history":
            self.reload_history()
        # the list could be changed on the title page
        self._mal_loaded = self._watching_loaded = self._feed_loaded = False
        if active == "mal":
            self.load_mal()
        elif active == "watching":
            self.load_watching()
        elif active == "feed":
            self.load_feed()

    def reset(self) -> None:
        """Drop cached results after extractor or network settings change."""
        self.query_one("#search-results", ItemList).set_items([])
        self._ongoing_loaded = False
        if self.query_one(TabbedContent).active == "ongoing":
            self.load_ongoing()
        self.reset_mal()

    def reset_mal(self) -> None:
        """MAL account changed: reload the lists when the tab is shown."""
        self._mal_loaded = self._watching_loaded = self._feed_loaded = False
        active = self.query_one(TabbedContent).active
        if active == "mal":
            self.load_mal()
        elif active == "watching":
            self.load_watching()
        # the unread count is on the tab: reload it on any tab
        self.load_feed()

    # --- tabs ----------------------------------------------------------------

    def action_show_tab(self, tab: str) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active == tab:
            self._on_tab_shown(tab)
        else:
            # TabActivated handler moves focus into the tab when the tab bar is not focused
            self.set_focus(None)
            tabs.active = tab

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id:
            # arrows on the tab bar keep focus there, so the next arrow switches the tab again
            self._on_tab_shown(event.pane.id, focus=not isinstance(self.focused, Tabs))

    def _on_tab_shown(self, tab: str, *, focus: bool = True) -> None:
        # "f" (find) is shown on the MyAnimeList tab only
        self.refresh_bindings()
        if tab == "ongoing" and not self._ongoing_loaded:
            self.load_ongoing()
        elif tab == "history":
            self.reload_history()
        elif tab == "mal" and not self._mal_loaded:
            self.load_mal()
        elif tab == "watching" and not self._watching_loaded:
            self.load_watching()
        elif tab == "feed" and not self._feed_loaded:
            self.load_feed()
        if focus:
            self.focus_tab_content()

    def focus_loaded(self, tab: str) -> None:
        """A list cannot take focus while it is loading: focus it when the data is there."""
        if self.focused is None and self.query_one(TabbedContent).active == tab:
            self.query_one(TAB_CONTENT[tab]).focus()

    def focus_tab_content(self) -> None:
        tab = self.query_one(TabbedContent).active
        widget = self.query_one(TAB_CONTENT[tab])
        # from the tab bar go straight to the search results if there are any
        if tab == "search" and self.query_one("#search-results", ItemList).items and isinstance(self.focused, Tabs):
            widget = self.query_one("#search-results")
        widget.focus()

    # --- arrow navigation ----------------------------------------------------

    @on(Navigate)
    def _navigate(self, event: Navigate) -> None:
        filters = {"mal-filter": "#mal-results", "mal-query": "#mal-results", "ongoing-filter": "#ongoing-results"}
        # up from the top of a list: to the search field / filter above it
        lists = {"mal-results": "#mal-query", "ongoing-results": "#ongoing-filter"}
        if event.control.id in filters and event.direction in ("up", "down"):
            if event.direction == "up":
                self.query_one(Tabs).focus()
            else:
                self.query_one(filters[event.control.id]).focus()
        elif event.direction == "up":
            if event.control.id in lists:
                self.query_one(lists[event.control.id]).focus()
            elif event.control.id == "search-results":
                self.query_one("#query", Input).focus()
            else:
                self.query_one(Tabs).focus()
        else:
            tabs = self.query_one(TabbedContent)
            step = 1 if event.direction == "right" else -1
            self.action_show_tab(TAB_ORDER[(TAB_ORDER.index(tabs.active) + step) % len(TAB_ORDER)])

    def action_arrow(self, direction: str) -> None:
        """Up/down for widgets without own vertical navigation: search input and tab bar."""
        focused = self.focused
        if isinstance(focused, Tabs):
            if direction == "down":
                self.focus_tab_content()
        elif focused is not None and focused.id == "query":
            if direction == "up":
                self.query_one(Tabs).focus()
            elif self.query_one("#search-results", ItemList).items:
                self.query_one("#search-results").focus()
        elif focused is not None and focused.id == "mal-query":
            self._navigate(Navigate(focused, direction))

    def action_find(self) -> None:
        self.query_one("#mal-query", Input).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # the list search is only on the MyAnimeList tab
        if action == "find":
            return self.query_one(TabbedContent).active == "mal"
        return True

    def action_refresh(self) -> None:
        active = self.query_one(TabbedContent).active
        if active == "ongoing":
            self.load_ongoing()
        elif active == "history":
            self.reload_history()
        elif active == "mal":
            self.load_mal()
        elif active == "watching":
            self.load_watching(force=True)
        elif active == "feed":
            self.load_feed(force=True)
        elif query := self.query_one("#query", Input).value.strip():
            self.search(query)

    # --- data ----------------------------------------------------------------

    @on(Input.Submitted, "#query")
    def _query_submitted(self, event: Input.Submitted) -> None:
        if query := event.value.strip():
            self._mal_hint = None
            self.search(query)

    @work(exclusive=True, group="search")
    async def search(self, query: str) -> None:
        results: ItemList = self.query_one("#search-results", ItemList)
        results.loading = True
        try:
            items = await self.app.session.extractor.a_search(query)
        except Exception as e:
            self.notify(t("Search error: {error}", error=e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        results.empty_text = t("Nothing found for «{query}»", query=query)
        results.set_items(items)
        results.border_title = t("Results: {count}", count=len(items)) if items else ""
        if items:
            results.focus()

    @work(exclusive=True, group="ongoing")
    async def load_ongoing(self) -> None:
        results: ItemList = self.query_one("#ongoing-results", ItemList)
        results.loading = True
        try:
            items = await self.app.session.extractor.a_ongoing()
        except Exception as e:
            self.notify(t("Failed to load ongoing: {error}", error=e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        self._ongoing_loaded = True
        self._ongoing_updated = ongoing_state.track(self.app.session.settings.extractor_name, items, ongoing.mark)
        # titles with new episodes on top, the rest in the site order (sorted() is stable)
        self._ongoing = sorted(items, key=lambda i: -self._ongoing_updated.get(i.url, 0))
        self._show_ongoing()
        self.focus_loaded("ongoing")

    def _ongoing_filter_setting(self) -> str:
        value = self.app.session.settings.ongoing_filter
        return value if value in ongoing.FILTERS else ongoing.AIRING

    def _show_ongoing(self) -> None:
        results: ItemList = self.query_one("#ongoing-results", ItemList)
        wanted = str(self.query_one("#ongoing-filter", Select).value)
        items = [i for i in self._ongoing if ongoing.matches(i, wanted)]
        highlighted = results.current
        if self._ongoing:
            results.empty_text = t("No titles with this status")
        now = time.time()
        results.set_items(items, [ongoing.label(i, self._ongoing_updated.get(i.url, 0), now) for i in items])
        if highlighted in items:
            results.highlighted = items.index(highlighted)
        counts = {status: sum(ongoing.status(i) == status for i in self._ongoing) for status in ongoing.FILTERS[1:]}
        results.border_title = (
            t(
                "Airing: {airing} · Announced: {announced}",
                airing=counts[ongoing.AIRING],
                announced=counts[ongoing.ANNOUNCED],
            )
            if self._ongoing
            else ""
        )

    @on(Select.Changed, "#ongoing-filter")
    def _ongoing_filter_changed(self, event: Select.Changed) -> None:
        settings = self.app.session.settings
        if settings.ongoing_filter != event.value:
            settings.ongoing_filter = str(event.value)
            with contextlib.suppress(OSError):
                settings.save()
        if self._ongoing_loaded:
            self._show_ongoing()

    @on(Select.Changed, "#mal-filter")
    def _mal_filter_changed(self) -> None:
        if self.query_one(TabbedContent).active == "mal":
            self.load_mal()

    @work(exclusive=True, group="mal")
    async def load_mal(self) -> None:
        results: ItemList[MalAnime] = self.query_one("#mal-results", ItemList)
        client = self.app.session.mal
        if not client.logged_in:
            results.empty_text = t("Log in to MyAnimeList in settings (F2)")
            results.set_items([])
            results.border_title = ""
            return
        status = str(self.query_one("#mal-filter", Select).value)
        results.loading = True
        try:
            items = await client.user_list(None if status == MAL_ALL else status)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        self._mal_loaded = True
        self._mal_items = items
        self._show_mal()
        self.focus_loaded("mal")

    def _show_mal(self) -> None:
        results: ItemList[MalAnime] = self.query_one("#mal-results", ItemList)
        query = self.query_one("#mal-query", Input).value.strip()
        with_status = str(self.query_one("#mal-filter", Select).value) == MAL_ALL
        items = self._mal_items
        links = _links_by_mal_id()
        if query:
            items = [
                a
                for a in items
                if mal.matches(mal.search_text(a, links[a.id]["data"]["title"] if a.id in links else ""), query)
            ]
        current = results.current
        results.empty_text = (
            t("Nothing matches «{query}»", query=query)
            if query and self._mal_items
            else t("No titles with this status")
        )
        results.set_items(items, [_mal_label(i, with_status, links.get(i.id)) for i in items])
        if current in items:
            results.highlighted = items.index(current)
        username = self.app.session.mal.username
        total = len(self._mal_items)
        if query:
            results.border_title = t("{user} · {shown} of {total}", user=username, shown=len(items), total=total)
        else:
            results.border_title = f"{username} · {total}" if total else username

    def _random_mal(self) -> None:
        results: ItemList[MalAnime] = self.query_one("#mal-results", ItemList)
        items = results.items
        if not len(items):
            self.notify(t("No titles with this status"), markup=False)
            return
        self.open_mal_anime(random.choice(items))

    @on(Input.Changed, "#mal-query")
    def _mal_query_changed(self) -> None:
        if self._mal_loaded:
            self._show_mal()

    @on(Button.Pressed, "#mal-random")
    def _mal_random_pressed(self) -> None:
        if self._mal_loaded:
            self._random_mal()

    @work(exclusive=True, group="watching")
    async def load_watching(self, *, force: bool = False) -> None:
        results: ItemList[watching.Entry] = self.query_one("#watching-results", ItemList)
        client = self.app.session.mal
        if not client.logged_in:
            results.empty_text = t("Log in to MyAnimeList in settings (F2)")
            results.set_items([])
            results.border_title = ""
            return
        results.loading = True
        try:
            animes = await client.user_list("watching")
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        self._watching_loaded = True
        results.empty_text = t("Nothing in Watching on MyAnimeList")
        items = watching.entries(self.app.session, animes)
        self._show_watching(items)
        self.focus_loaded("watching")

        # the sites answer at different speed: update the list as they do
        semaphore = asyncio.Semaphore(watching.CONCURRENCY)

        async def check(entry: watching.Entry) -> None:
            await watching.check(entry, semaphore, force=force, show_leaked=self.app.session.settings.show_leaked)
            self._show_watching(items)

        await asyncio.gather(*(check(e) for e in items if e.item is not None))
        watching.remember_releases(items)
        self._show_watching(items)

    def _show_watching(self, items: list[watching.Entry]) -> None:
        results: ItemList[watching.Entry] = self.query_one("#watching-results", ItemList)
        # follow the entry only if the user moved the cursor: otherwise stay on top, on the new episodes
        current = results.current if results.highlighted else None
        ordered = watching.ordered(items)
        now = time.time()
        results.set_items(ordered, [watching.label(e, now) for e in ordered])
        if current in ordered:
            results.highlighted = ordered.index(current)
        new = sum(1 for e in items if e.unwatched)
        results.border_title = t("With new episodes: {new} of {total}", new=new, total=len(items)) if items else ""

    # --- notifications -------------------------------------------------------

    @work(exclusive=True, group="feed")
    async def load_feed(self, *, force: bool = False) -> None:
        results: FeedList = self.query_one(FeedList)
        client = self.app.session.mal
        if not client.logged_in:
            self._feed_items = []
            results.empty_text = t("Log in to MyAnimeList in settings (F2)")
            self._show_feed()
            return
        if self._feed is None:
            self._feed = Feed()
        # the first load asks MAL about every title of the list: show the progress, keep the old items
        results.loading = not self._feed_items

        def progress(done: int, total: int) -> None:
            results.border_title = t("Checking related titles: {done} of {total}…", done=done, total=total)

        try:
            self._feed_items = await self._feed.load(client, force=force, on_progress=progress)
        except MalError as e:
            results.border_title = ""
            if self.query_one(TabbedContent).active == "feed":
                self.notify(str(e), severity="error", markup=False)
            return
        finally:
            stop_loading(results)
        self._feed_loaded = True
        incomplete = self._feed.incomplete
        results.empty_text = (
            t("MyAnimeList limits requests: the rest will be checked in a few minutes")
            if incomplete
            else t("No new related titles")
        )
        if self._feed.limited:
            # MAL lifts the limit in a few minutes: go on from where the check stopped
            self.set_timer(RETRY_AFTER, self.load_feed)
        self._show_feed()
        self.focus_loaded("feed")

    def _show_feed(self) -> None:
        results: FeedList = self.query_one(FeedList)
        feed, items = self._feed, self._feed_items
        current = results.current
        now = time.time()
        results.set_items(items, [_feed_label(i, feed is not None and feed.is_unread(i), now) for i in items])
        if current in items:
            results.highlighted = items.index(current)
        unread = sum(1 for i in items if feed is not None and feed.is_unread(i))
        parts = []
        if items:
            parts.append(t("Related titles not in your list: {total} · new: {unread}", total=len(items), unread=unread))
        if feed is not None and feed.incomplete:
            checked, total = feed.incomplete
            parts.append(t("checked {checked} of {total}, the rest in a few minutes", checked=checked, total=total))
        results.border_title = " · ".join(parts)
        tab = self.query_one(TabbedContent).get_tab("feed")
        tab.label = f"{t('Notifications')} ({unread})" if unread else t("Notifications")

    @work(group="mal-update")
    async def feed_plan_to_watch(self, item: FeedItem) -> None:
        try:
            anime, added = await mal.plan_to_watch(self.app.session, item.anime.id)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        # in the list now: not a notification anymore
        self._feed_items = [i for i in self._feed_items if i.anime.id != anime.id]
        self._show_feed()
        if added:
            self.notify(t("«{title}» added to Plan to Watch", title=anime.title), markup=False)
        else:
            status = anime.list_status.status if anime.list_status else None
            self.notify(
                t("«{title}» is in your list already: {status}", title=anime.title, status=mal.status_label(status)),
                markup=False,
            )

    def feed_hide(self, item: FeedItem) -> None:
        if self._feed is not None:
            self._feed.hide(item.anime.id)
        self._feed_items = [i for i in self._feed_items if i is not item]
        self._show_feed()

    def feed_mark_read(self) -> None:
        if self._feed is not None:
            self._feed.mark_read()
            self._show_feed()

    def action_profile(self) -> None:
        client = self.app.session.mal
        if not client.logged_in:
            self.notify(t("Log in to MyAnimeList in settings (F2)"), severity="warning", markup=False)
            return
        self.app.push_screen(ProfileScreen(client.username))

    def reload_history(self) -> None:
        results: HistoryList = self.query_one(HistoryList)
        entries = history.load(self.app.session.http_kwargs)
        highlighted = results.highlighted
        results.set_items(entries, [_history_label(*e) for e in entries])
        if entries and highlighted is not None:
            results.highlighted = min(highlighted, len(entries) - 1)

    # --- open title ----------------------------------------------------------

    @on(OptionList.OptionSelected, "#search-results, #ongoing-results")
    def _open_result(self, event: OptionList.OptionSelected) -> None:
        items = event.option_list.items  # type: ignore[attr-defined]
        if items:
            item = items[event.option_index]
            hint = self._mal_hint if event.option_list.id == "search-results" else None
            self.open_title(item, self.app.session.settings.extractor_name, mal_hint=hint)

    @on(OptionList.OptionSelected, "#mal-results")
    def _open_mal(self, event: OptionList.OptionSelected) -> None:
        results: ItemList[MalAnime] = self.query_one("#mal-results", ItemList)
        if results.items:
            self.open_mal_anime(results.items[event.option_index])

    @on(OptionList.OptionSelected, "#feed-results")
    def _open_feed(self, event: OptionList.OptionSelected) -> None:
        results: FeedList = self.query_one(FeedList)
        if results.items:
            self.open_mal_anime(results.items[event.option_index].anime)

    @on(OptionList.OptionSelected, "#watching-results")
    def _open_watching(self, event: OptionList.OptionSelected) -> None:
        results: ItemList[watching.Entry] = self.query_one("#watching-results", ItemList)
        if not results.items:
            return
        entry = results.items[event.option_index]
        if entry.item is not None and entry.link is not None:
            self.open_title(entry.item, entry.link["extractor_name"])
        else:
            self.open_mal_anime(entry.anime)

    def open_mal_anime(self, anime: MalAnime) -> None:
        """The linked title page, or search it on the current site (the opened result gets linked)."""
        session = self.app.session
        link = mal_links.find_by_mal_id(anime.id, session.settings.extractor_name)
        if link:
            try:
                item = mal_links.restore(link, session.http_kwargs)
            except Exception:
                # the extractor of the link was removed or changed its models: search again
                self.log.warning(f"broken MAL link: {link}")
            else:
                self.open_title(item, link["extractor_name"])
                return
        # never watched in anihub: search on the current site, the opened result gets linked
        query = mal.search_queries(anime.all_titles, limit=1)
        self._mal_hint = anime
        self.action_show_tab("search")
        self.query_one("#query", Input).value = query[0] if query else anime.title
        self.search(self.query_one("#query", Input).value)
        self.notify(
            t("Pick «{title}» in the search results: it will be linked to MyAnimeList", title=anime.title),
            markup=False,
        )

    def open_title(self, item: Any, extractor_name: str, mal_hint: MalAnime | None = None) -> None:
        # a fast double click selects twice before the first title page is shown: open it once
        if self.app.screen is self:
            self.app.push_screen(AnimeScreen(item, extractor_name, mal_hint=mal_hint))

    @on(OptionList.OptionSelected, "#history-results")
    def _open_history(self, event: OptionList.OptionSelected) -> None:
        results = self.query_one(HistoryList)
        if not results.items or self.app.screen is not self:
            return
        entry, item = results.items[event.option_index]

        def open_title(resume: bool | None) -> None:
            if resume:
                url = entry["data"]["url"]
                self.app.play(
                    playback.resume(
                        self.app.session,
                        entry,
                        on_watched=self.app.mal_tracker(entry["extractor_name"], url),
                        confirm_watched=self.app.mal_confirm_watched(entry["extractor_name"], url),
                    ),
                    f"▶ {entry['episode']}",
                )
            else:
                self.open_title(item, entry["extractor_name"])

        if entry.get("time") and entry.get("source"):
            question = t(
                "Continue «{episode}» from {time}?", episode=entry["episode"], time=_format_time(entry["time"])
            )
            self.app.push_screen(ConfirmScreen(question, yes=t("Continue"), no=t("To episodes")), open_title)
        else:
            open_title(False)
