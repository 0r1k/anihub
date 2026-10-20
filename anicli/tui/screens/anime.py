from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, OptionList, Select, Static
from textual.widgets.selection_list import Selection

from anicli.common.extractors import dynamic_load_extractor_module
from anicli.common.mal import STATUSES, MalAnime, MalError

from .. import mal, playback, posters, subtitles
from ..i18n import localized_bindings, t
from ..selection import parse_selection_mask
from ..widgets import EpisodeList, ItemList, Navigate, PlayerBar, stop_loading
from .dialogs import ConfirmScreen
from .mal_dialogs import EpisodesScreen, MalSearchScreen, ScoreScreen

if TYPE_CHECKING:
    from anicli_api.base import BaseAnime, BaseEpisode, BaseSource

    from ..app import AnicliApp

# the quality column: parallel requests to the players and the wait for one of them
QUALITY_CONCURRENCY = 4
QUALITY_TIMEOUT = 25
# actions available only when the title is linked to MyAnimeList
MAL_ACTIONS = {"mal_status", "mal_score", "mal_episodes", "mal_remove"}


@localized_bindings
class AnimeScreen(Screen[None]):
    """Title page: pick episode(s), then dubber/player, then play."""

    app: AnicliApp
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("a", "select_all", "All episodes"),
        Binding("n", "select_none", "Clear marks"),
        Binding("r", "range", "Range"),
        Binding("f5,u", "refresh", "Refresh"),
        Binding("t", "find_subtitles", "Subtitles from other sites"),
        Binding("g", "mal_score", "Rate"),
        Binding("s", "mal_status", "MAL status", show=False),
        Binding("e", "mal_episodes", "Watched episodes", show=False),
        Binding("l", "mal_link", "MAL link", show=False),
        Binding("D", "mal_remove", "Remove from MAL", show=False),
    ]
    # the poster sidebar is hidden on narrow terminals
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (110, "-wide")]

    def __init__(self, item: Any, extractor_name: str, mal_hint: MalAnime | None = None) -> None:
        super().__init__()
        self.item = item
        self.extractor_name = extractor_name
        self.anime: BaseAnime | None = None
        self.episodes: Sequence[BaseEpisode] = []
        self.chosen: list[int] = []
        # chosen in the MyAnimeList tab before searching the title on the site
        self.mal_hint = mal_hint
        self.mal: MalAnime | None = None
        self.mal_titles: list[str] = []
        self._poster_url = ""
        # best video quality by source url: missing - loading, None - the player gave no video
        self._qualities: dict[str, int | None] = {}
        # the same title on other sites, searched for subtitles (None: not searched yet)
        self.foreign: list[subtitles.ForeignTitle] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="anime-page"):
            settings = self.app.session.settings
            poster = posters.make_widget(settings.poster_render, id="poster") if settings.posters else None
            if poster is not None:
                with Vertical(id="anime-side"):
                    yield poster
            with Vertical(id="anime-main"):
                yield from self._compose_main()
        yield PlayerBar()
        yield Footer()

    def _compose_main(self) -> ComposeResult:
        with VerticalScroll(id="anime-info"):
            yield Static(Text(self.item.title, style="bold"), id="anime-title")
            yield Static(id="anime-description")
        with Horizontal(id="mal-bar"):
            yield Static(id="mal-label", markup=False)
            yield Select(
                [(mal.status_label(s), s) for s in STATUSES],
                prompt=t("Not in list"),
                id="mal-status",
                classes="mal-linked",
            )
            yield Button(f"★ {t('Rate')} (g)", id="mal-score", classes="mal-linked")
            yield Button(f"{t('Episodes')} (e)", id="mal-episodes", classes="mal-linked")
            yield Button(f"{t('Link')} (l)", id="mal-link")
            yield Button(f"{t('Remove')} (D)", id="mal-remove", variant="error", classes="mal-linked")
        yield Input(placeholder=t("Episodes: 1-12 or 1 3 5-7, Enter to mark"), id="episode-range", classes="hidden")
        with Horizontal(id="anime-body"):
            yield EpisodeList(id="episodes")
            yield ItemList(id="sources", empty_text=t("Select an episode on the left"))

    def on_mount(self) -> None:
        self.sub_title = self.item.title
        episodes = self.query_one(EpisodeList)
        episodes.border_title = t("Episodes")
        episodes.border_subtitle = t("Space to mark, Enter to choose")
        self.query_one("#sources").border_title = t("Dub / player")
        self.query_one("#mal-bar").display = self.app.session.mal.logged_in
        self.set_mal(None)
        self.load_anime()

    # --- loading -------------------------------------------------------------

    @work(exclusive=True, group="anime")
    async def load_anime(self, *, refresh: bool = False) -> None:
        episodes_list = self.query_one(EpisodeList)
        old_count = len(self.episodes)
        marked = set(episodes_list.selected)
        highlighted = episodes_list.highlighted
        episodes_list.loading = True
        try:
            self.anime = await self.item.a_get_anime()
            self.episodes = await self.anime.a_get_episodes()
        except Exception as e:
            self.notify(t("Failed to load the title: {error}", error=e), severity="error", markup=False)
            return
        finally:
            stop_loading(episodes_list)

        self.query_one("#anime-title", Static).update(Text(self.anime.title, style="bold"))
        self.query_one("#anime-description", Static).update(Text(self.anime.description or "", style="dim"))
        if not refresh and self.app.session.mal.logged_in:
            # the poster is chosen after the MAL entry is resolved
            self.load_mal()
        elif not refresh:
            self._update_poster()
        episodes_list.clear_options()
        if not self.episodes:
            self.notify(t("No episodes available"), severity="warning", markup=False)
            return
        episodes_list.add_options(
            Selection(self._episode_label(ep), i, i in marked) for i, ep in enumerate(self.episodes)
        )
        if not refresh:
            episodes_list.highlighted = 0
            episodes_list.focus()
            return

        episodes_list.highlighted = min(highlighted or 0, len(self.episodes) - 1)
        new_count = len(self.episodes) - old_count
        if new_count > 0:
            self.notify(t("New episodes: {count}", count=new_count))
        else:
            self.notify(t("No new episodes"))

    @work(exclusive=True, group="anime")
    async def load_sources(self, indices: list[int]) -> None:
        sources_list: ItemList[BaseSource] = self.query_one("#sources", ItemList)
        sources_list.loading = True
        first = self.episodes[indices[0]]
        try:
            sources = await first.a_get_sources()
        except Exception as e:
            self.notify(t("Failed to load sources: {error}", error=e), severity="error", markup=False)
            return
        finally:
            stop_loading(sources_list)

        self.chosen = indices
        self._show_sources(list(sources), indices)
        if sources:
            sources_list.focus()
        elif not self.foreign:
            self.notify(t("No sources available"), severity="warning", markup=False)
        if self.foreign:
            # own sources are shown at once, subtitles from other sites are appended when loaded
            sources_list.border_subtitle = t("loading subtitles from other sites…")
            found = await subtitles.foreign_sources(self.foreign, int(first.ordinal))
            self._show_sources([*sources, *found], indices, keep_highlight=True)
            if found and not sources:
                sources_list.focus()

    def _show_sources(self, sources: list[Any], indices: list[int], *, keep_highlight: bool = False) -> None:
        sources_list: ItemList[Any] = self.query_one("#sources", ItemList)
        first = self.episodes[indices[0]]
        if self.app.session.settings.subtitles_first:
            sources = subtitles.subtitles_first(sources)
        # keep the cursor only if the user moved it: otherwise it stays on top, on the subtitles
        highlighted = sources_list.current if keep_highlight and sources_list.highlighted else None
        subtitle = (
            t("episode {num}", num=first.ordinal)
            if len(indices) == 1
            else t("{count} episodes from {num}", count=len(indices), num=first.ordinal)
        )
        if not any(subtitles.is_subtitles(self._plain_source(s)) for s in sources):
            subtitle = f"{t('no subtitles: t — search other sites')} · {subtitle}"
        sources_list.border_subtitle = subtitle
        sources_list.set_items(sources, [self._source_label(s) for s in sources])
        if highlighted is not None and highlighted in sources:
            sources_list.highlighted = sources.index(highlighted)
        self.load_qualities()

    @staticmethod
    def _plain_source(choice: Any) -> BaseSource:
        return choice.source if isinstance(choice, subtitles.ForeignSource) else choice

    def _source_label(self, choice: Any) -> Table:
        """The source name on the left, its best video quality on the right."""
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(self._source_name(choice), self._quality_label(self._plain_source(choice).url))
        return grid

    def _quality_label(self, url: str) -> Text:
        if url not in self._qualities:
            return Text("…", style="dim")
        quality = self._qualities[url]
        if quality is None:
            return Text("—", style="dim")
        if not quality:
            return Text("?", style="dim")
        wanted = self.app.session.settings.quality
        # below the quality of the settings: the closest one will be played
        return Text(f"{quality}p", style="bold" if quality >= wanted else "")

    @work(exclusive=True, group="qualities")
    async def load_qualities(self) -> None:
        """Ask the players of the shown sources for their videos, a few at a time; the labels update as they answer."""
        sources_list: ItemList[Any] = self.query_one("#sources", ItemList)
        choices = list(sources_list.items)
        semaphore = asyncio.Semaphore(QUALITY_CONCURRENCY)

        async def one(choice: Any) -> None:
            source = self._plain_source(choice)
            if source.url in self._qualities:
                return
            async with semaphore:
                try:
                    videos = await asyncio.wait_for(playback.source_videos(source), QUALITY_TIMEOUT)
                except Exception:  # the player is down or blocked: shown as "—"
                    videos = []
            self._qualities[source.url] = playback.max_quality(videos) if videos else None
            # the list could be rebuilt meanwhile (more subtitles from other sites)
            if choice in sources_list.items:
                index = sources_list.items.index(choice)
                sources_list.replace_option_prompt_at_index(index, self._source_label(choice))

        await asyncio.gather(*(one(c) for c in choices))

    def _source_name(self, choice: Any) -> Text:
        source = self._plain_source(choice)
        label = Text()
        if subtitles.is_subtitles(source):
            label.append(f" {t('SUB')} ", style="bold reverse")
            label.append(" ")
        label.append(source.title)
        if isinstance(choice, subtitles.ForeignSource):
            label.append(f" · {choice.site.extractor_name} «{choice.site.item.title}»", style="italic")
        label.append(f" · {urlsplit(source.url).netloc}", style="dim")
        return label

    # --- events --------------------------------------------------------------

    @on(EpisodeList.SelectedChanged)
    def _update_counter(self) -> None:
        episodes = self.query_one(EpisodeList)
        parts = [t("Episodes")]
        if episodes.watched:
            parts.append(t("watched {count}", count=len(episodes.watched)))
        if count := len(episodes.selected):
            parts.append(t("marked {count}", count=count))
        episodes.border_title = " · ".join(parts)

    @on(EpisodeList.Chosen)
    def _episodes_chosen(self, event: EpisodeList.Chosen) -> None:
        self.load_sources(event.indices)

    @on(OptionList.OptionSelected, "#sources")
    def _source_chosen(self, event: OptionList.OptionSelected) -> None:
        sources_list: ItemList[Any] = self.query_one("#sources", ItemList)
        if not self.anime or not self.chosen or not sources_list.items:
            return
        choice = sources_list.items[event.option_index]
        episodes = [self.episodes[i] for i in self.chosen]
        item, extractor_name, anime = self.item, self.extractor_name, self.anime
        if isinstance(choice, subtitles.ForeignSource):
            # play from the other site: its episodes with the same numbers
            site = choice.site
            item, extractor_name, anime = site.item, site.extractor_name, site.anime
            episodes = [site.episodes[o] for o in (int(e.ordinal) for e in episodes) if o in site.episodes]
        source = self._plain_source(choice)
        app, session = self.app, self.app.session
        # MAL progress belongs to this page's title whatever site plays it
        on_watched = app.mal_tracker(self.extractor_name, self.item.url)
        confirm_watched = app.mal_confirm_watched(self.extractor_name, self.item.url)

        if len(episodes) == 1:
            label = playback.episode_title(anime, episodes[0])
            app.play(
                playback.play_episode(
                    session,
                    item,
                    extractor_name,
                    anime,
                    episodes[0],
                    source,
                    on_watched=on_watched,
                    confirm_watched=confirm_watched,
                ),
                label,
            )
            return

        total = len(episodes)
        app.player_progress = (0, total)

        def on_fetched(count: int, _title: str) -> None:
            app.player_progress = (count, total)

        def on_play(titles: Sequence[str]) -> None:
            app.player_status = f"▶ {titles[0]} … {titles[-1]}" if len(titles) > 1 else f"▶ {titles[0]}"

        async def confirm_next(title: str) -> bool:
            question = t("Play the next episodes starting from «{title}»?", title=title)
            return bool(await app.push_screen_wait(ConfirmScreen(question, yes=t("Play"), no=t("Stop"))))

        async def run() -> None:
            result = await playback.play_batch(
                session,
                item,
                extractor_name,
                anime,
                episodes,
                source,
                on_fetched=on_fetched,
                on_play=on_play,
                confirm_next=confirm_next,
                on_watched=on_watched,
                confirm_watched=confirm_watched,
            )
            if result.stopped:
                app.notify(t("Playback stopped"))
            elif result.played < total:
                app.notify(
                    t(
                        "Played {played} of {total}: «{source}» has no next episodes",
                        played=result.played,
                        total=total,
                        source=source.title,
                    ),
                    severity="warning",
                    markup=False,
                )

        app.play(run(), t("{title}: loading {count} episodes…", title=anime.title, count=total))

    @on(Navigate)
    def _navigate(self, event: Navigate) -> None:
        if event.direction == "right" and event.control.id == "episodes":
            if self.query_one("#sources", ItemList).items:
                self.query_one("#sources").focus()
            else:
                # nothing to show on the right yet: load sources of the highlighted episode
                self.query_one(EpisodeList).action_choose()
        elif event.direction == "left" and event.control.id == "sources":
            self.query_one(EpisodeList).focus()

    @on(Input.Submitted, "#episode-range")
    def _range_submitted(self, event: Input.Submitted) -> None:
        episodes_list = self.query_one(EpisodeList)
        try:
            mask = parse_selection_mask(event.value, len(self.episodes))
        except ValueError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        episodes_list.deselect_all()
        for i, marked in enumerate(mask):
            if marked:
                episodes_list.select(i)
        event.input.add_class("hidden")
        episodes_list.focus()

    # --- actions -------------------------------------------------------------

    def action_back(self) -> None:
        range_input = self.query_one("#episode-range", Input)
        if not range_input.has_class("hidden"):
            range_input.add_class("hidden")
            self.query_one(EpisodeList).focus()
        elif self.focused and self.focused.id == "sources":
            self.query_one(EpisodeList).focus()
        else:
            self.dismiss()

    def action_select_all(self) -> None:
        self.query_one(EpisodeList).select_all()

    def action_select_none(self) -> None:
        self.query_one(EpisodeList).deselect_all()

    def action_refresh(self) -> None:
        self.load_anime(refresh=True)

    def action_range(self) -> None:
        if not self.episodes:
            return
        range_input = self.query_one("#episode-range", Input)
        range_input.remove_class("hidden")
        range_input.value = ""
        range_input.focus()

    # --- MyAnimeList ---------------------------------------------------------

    @work(exclusive=True, group="mal")
    async def load_mal(self) -> None:
        self.query_one("#mal-label", Static).update(t("MyAnimeList: searching…"))
        try:
            found, self.mal_titles = await mal.resolve(
                self.app.session, self.item, self.extractor_name, self.anime, self.mal_hint
            )
        except MalError as e:
            self.query_one("#mal-label", Static).update(t("MyAnimeList: error"))
            self.notify(str(e), severity="error", markup=False)
            self._update_poster()
            return
        self.set_mal(found)

    def set_mal(self, anime: MalAnime | None) -> None:
        """Show the MAL entry state (None: not linked)."""
        self.mal = anime
        label = self.query_one("#mal-label", Static)
        status_select = self.query_one("#mal-status", Select)
        # not linked: only the link button, the hint needs the space
        for widget in self.query(".mal-linked"):
            widget.display = anime is not None
        status_select.display = anime is not None
        if anime is None:
            label.update(Text(t("Not linked to MyAnimeList: press l to choose"), style="dim"))
            status_select.clear()
        else:
            label.update(mal.describe(anime))
            status = anime.list_status.status if anime.list_status else None
            # programmatic change: the Changed handler skips it as the value equals the current status
            if status in STATUSES:
                status_select.value = status
            else:
                status_select.clear()
        self.query_one("#mal-remove", Button).disabled = anime is None or anime.list_status is None
        self._refresh_watched()
        self._update_poster()
        self.refresh_bindings()

    # --- poster --------------------------------------------------------------

    def on_screen_suspend(self) -> None:
        # a dialog on top: graphics drawn around Textual (sixel) under it can scroll the terminal
        for poster in self.query("#poster"):
            poster.display = False

    def on_screen_resume(self) -> None:
        for poster in self.query("#poster"):
            poster.display = True

    def _update_poster(self) -> None:
        """MAL poster if the title is linked, otherwise the site thumbnail."""
        if self.anime is None or not self.query("#poster"):
            return
        session = self.app.session
        if self.mal is not None and self.mal.picture:
            url, client = self.mal.picture, session.mal.http
        else:
            thumbnail = self.anime.thumbnail or self.item.thumbnail
            base_url = getattr(dynamic_load_extractor_module(self.extractor_name).Extractor, "BASE_URL", "")
            url, client = posters.resolve_url(thumbnail, self.item.url, base_url), session.http_async
        if url and url != self._poster_url:
            self._poster_url = url
            self.load_poster(url, client)

    @work(exclusive=True, group="poster")
    async def load_poster(self, url: str, client: Any) -> None:
        image = await posters.fetch(client, url)
        if image is not None and url == self._poster_url:
            self.query_one("#poster").image = image  # type: ignore[attr-defined]

    def _is_watched(self, episode: BaseEpisode) -> bool:
        """MAL keeps only the watched count: episodes up to it are watched."""
        status = self.mal.list_status if self.mal else None
        if status is None:
            return False
        if status.status == "completed" and not status.is_rewatching:
            return True
        try:
            return 0 < int(episode.ordinal) <= status.num_episodes_watched
        except (TypeError, ValueError):
            return False

    def _episode_label(self, episode: BaseEpisode) -> Text:
        return Text(
            f"{episode.ordinal:>4}. {episode.title.strip() or t('Episode')}",
            style="dim" if self._is_watched(episode) else "",
        )

    def _refresh_watched(self) -> None:
        episodes_list = self.query_one(EpisodeList)
        episodes = self.episodes[: episodes_list.option_count]
        episodes_list.set_watched(i for i, episode in enumerate(episodes) if self._is_watched(episode))
        for i, episode in enumerate(episodes):
            episodes_list.replace_option_prompt_at_index(i, self._episode_label(episode))
        self._update_counter()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in MAL_ACTIONS or action == "mal_link":
            if not self.app.session.mal.logged_in:
                return False
            if action in MAL_ACTIONS and self.mal is None:
                return None
        return True

    @work(group="mal-update")
    async def mal_update(self, **fields: Any) -> None:
        anime = self.mal
        if anime is None:
            return
        try:
            anime.list_status = await self.app.session.mal.update_list_status(anime.id, **fields)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            self.set_mal(anime)  # restore the select value
            return
        self.set_mal(anime)
        self.notify(
            t("MyAnimeList updated: {summary}", summary=mal.describe(anime, with_status=True).plain), markup=False
        )

    @on(Select.Changed, "#mal-status")
    def _mal_status_changed(self, event: Select.Changed) -> None:
        anime = self.mal
        if anime is None or event.value is Select.NULL:
            return
        current = anime.list_status.status if anime.list_status else None
        if event.value == current:
            return
        fields: dict[str, Any] = {"status": event.value}
        # like the site does: completed means all episodes are watched
        if event.value == "completed" and anime.num_episodes:
            fields["num_watched_episodes"] = anime.num_episodes
        self.mal_update(**fields)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "mal-score": self.action_mal_score,
            "mal-episodes": self.action_mal_episodes,
            "mal-link": self.action_mal_link,
            "mal-remove": self.action_mal_remove,
        }
        if action := actions.get(event.button.id or ""):
            action()

    def action_mal_status(self) -> None:
        status_select = self.query_one("#mal-status", Select)
        if status_select.display:
            status_select.focus()
            status_select.action_show_overlay()

    def action_mal_score(self) -> None:
        anime = self.mal
        if anime is None:
            return
        current = anime.list_status.score if anime.list_status else 0

        def on_score(score: int | None) -> None:
            if score is not None and score != current:
                fields: dict[str, Any] = {"score": score}
                # a score adds the title to the list: give it a status like the site does
                if anime.list_status is None:
                    fields["status"] = "watching"
                self.mal_update(**fields)

        self.app.push_screen(ScoreScreen(anime.title, current), on_score)

    def action_mal_episodes(self) -> None:
        anime = self.mal
        if anime is None:
            return
        current = anime.list_status.num_episodes_watched if anime.list_status else 0

        def on_count(count: int | None) -> None:
            if count is None or count == current:
                return
            fields: dict[str, Any] = {"num_watched_episodes": count}
            status = anime.list_status.status if anime.list_status else None
            if anime.num_episodes and count >= anime.num_episodes:
                fields["status"] = "completed"
            elif count and status in (None, "plan_to_watch"):
                fields["status"] = "watching"
            elif status is None:
                fields["status"] = "plan_to_watch"
            self.mal_update(**fields)

        self.app.push_screen(EpisodesScreen(anime.title, current, anime.num_episodes), on_count)

    def action_mal_link(self) -> None:
        queries = mal.search_queries(self.mal_titles) or [self.item.title]
        if self.mal is not None:
            queries = [self.mal.title, *queries]

        self.app.push_screen(MalSearchScreen(queries), self._mal_relink)

    @work(group="mal")
    async def _mal_relink(self, chosen: MalAnime | None) -> None:
        if chosen is None:
            return
        try:
            self.set_mal(await mal.link(self.app.session, self.item, self.extractor_name, chosen))
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)

    def action_mal_remove(self) -> None:
        anime = self.mal
        if anime is None or anime.list_status is None:
            return

        question = t("Remove «{title}» from your MyAnimeList? The score and progress will be lost.", title=anime.title)
        self.app.push_screen(ConfirmScreen(question, yes=t("Remove"), no=t("Cancel")), self._mal_remove)

    @work(group="mal-update")
    async def _mal_remove(self, confirmed: bool | None) -> None:
        anime = self.mal
        if not confirmed or anime is None:
            return
        try:
            await self.app.session.mal.delete_list_status(anime.id)
        except MalError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        anime.list_status = None
        self.set_mal(anime)
        self.notify(t("«{title}» removed from your MyAnimeList", title=anime.title), markup=False)

    def matches(self, extractor_name: str, url: str) -> bool:
        return self.extractor_name == extractor_name and self.item.url == url

    # --- subtitles from other sites ----------------------------------------

    def action_find_subtitles(self) -> None:
        if self.anime is not None:
            self.find_subtitles()

    @work(exclusive=True, group="subtitles")
    async def find_subtitles(self) -> None:
        assert self.anime is not None  # noqa: S101
        titles = {self.item.title, self.anime.title, *self.mal_titles}
        if self.mal is not None:
            titles.update(self.mal.all_titles)
        self.notify(t("Searching «{title}» on other sites…", title=self.item.title), markup=False)
        queries = [self.item.title, *mal.search_queries([self.mal.title] if self.mal else self.mal_titles, limit=1)]
        queries = list(dict.fromkeys(queries))
        sites = await subtitles.find_titles(self.app.session, self.extractor_name, queries, titles)
        self.foreign = sites
        if sites:
            names = ", ".join(f"{s.extractor_name} «{s.item.title}»" for s in sites)
            self.notify(t("Found on: {sites}", sites=names), markup=False)
        else:
            self.notify(t("The title is not found on other sites"), severity="warning", markup=False)
        if self.chosen:
            self.load_sources(self.chosen)
        elif sites:
            self.notify(t("Choose an episode: subtitles from these sites are added to the list"), markup=False)
