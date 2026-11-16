"""Subtitles: which players are subtitles, title words and seasons, the same title on other sites."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import attr
import pytest

from anihub.tui import subtitles
from anihub.tui.subtitles import ForeignSource, ForeignTitle, _Wanted, _words, is_subtitles, subtitles_first


def source(title: str, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(title=title, url=f"https://player.example/{title}", **kwargs)


# names of real players: yummy-anime, animego (kodik names), hdrezka, anitube, the sites' own labels
@pytest.mark.parametrize(
    "name",
    [
        "Субтитры SubVost",
        "Субтитры Crunchyroll",
        "Kazoku Project.Subtitles",
        "Crunchyroll.Subtitles",
        "Оригинал (+субтитры)",
        "Субтитри Morys",
        "Субтитры",
        "Sub",
        "[SUB]",
        "СУБ",
        "Eng Subs",
        "RAW+SUB",
        "Hardsub",
        "SubVost",
        "YakuSub Studio",
    ],
)
def test_subtitles(name):
    assert is_subtitles(source(name))


@pytest.mark.parametrize(
    "name",
    [
        "Subaru Dub",
        "SUBARU DUB",
        "Submarine",
        "Оригинальная озвучка",
        "Оригинал",
        "Озвучка AniLibria",
        "Озвучення Clan Kaizoku",
        "AniLibria.TV",
        "AniDUB",
        "Dream Cast",
        "Aniliberty",
        "Субару",
        "hdrezka",
        "",
    ],
)
def test_not_subtitles(name):
    assert not is_subtitles(source(name))


def test_translation_type_of_the_player():
    """anilibme names players by the team only: the kind of the translation is in the data."""
    assert is_subtitles(source("Crunchyroll", data={"translation_type": {"id": 1, "label": "Субтитры"}}))
    assert not is_subtitles(source("AniLibria", data={"translation_type": {"id": 2, "label": "Озвучка"}}))
    for data in (None, "Субтитры", {"translation_type": "Субтитры"}, {"translation_type": {"label": None}}):
        assert not is_subtitles(source("AniLibria", data=data))


def test_subtitles_first_keeps_the_order():
    site = ForeignTitle("anitube", SimpleNamespace(title="Блакитна скринька 2"), None, {})
    dub, subs, other_dub = source("Озвучка AniLibria"), source("Субтитры Crunchyroll"), source("Dream Cast")
    foreign = ForeignSource(site, source("Субтитри Morys"), 1)
    assert subtitles_first([dub, subs, other_dub, foreign]) == [subs, foreign, dub, other_dub]


@pytest.mark.parametrize(
    ("title", "words"),
    [
        # hdrezka: the season and the episode in the title
        ("Синяя шкатулка (2 сезон, 3 серия)", {"синяя", "шкатулка", "2"}),
        ("Монолог фармацевта 2 сезон 12 серия", {"монолог", "фармацевта", "2"}),
        # animevost: both names and the episodes
        (
            "Синяя шкатулка (второй сезон) / Ao no Hako Season 2 [1-3 из 12+]",
            {"синяя", "шкатулка", "ao", "no", "hako", "2"},
        ),
        ("Ван-Пис [ТВ] [1-1155 из 1155+]", {"ван", "пис", "тв"}),
        ("Наруто: Ураганные хроники [ТВ-2]", {"наруто", "ураганные", "хроники", "2"}),
        ("Sousou no Frieren 2nd Season", {"sousou", "no", "frieren", "2"}),
        (
            "Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season",
            {"re", "zero", "kara", "hajimeru", "isekai", "seikatsu", "3"},
        ),
        ("Durarara!!x2 Shou", {"durarara", "2", "shou"}),
        # the first season is not numbered on most sites
        ("Атака титанов [ТВ-1]", {"атака", "титанов"}),
        ("Ёсуга но сора", {"есуга", "но", "сора"}),
        # anitube, ukrainian: the ordinal with any of the apostrophes, the episodes count
        ("Блакитна скринька (другий сезон)", {"блакитна", "скринька", "2"}),
        ("Магія та м'язи (п'ятий сезон)", {"магія", "та", "м", "язи", "5"}),
        ("Магія та м’язи (п’ятий сезон)", {"магія", "та", "м", "язи", "5"}),
        ("Магія та мʼязи (пʼятий сезон)", {"магія", "та", "м", "язи", "5"}),
        ("Крижані стіни [12 серій]", {"крижані", "стіни"}),
    ],
)
def test_words_and_seasons(title, words):
    assert _words(title) == words


def test_the_same_season_on_other_sites():
    wanted = _Wanted(["Синяя шкатулка 2", "Ao no Hako 2nd Season"], None, {"2"})
    for title in (
        "Синяя шкатулка (второй сезон) / Ao no Hako Season 2 [1-3 из 12+]",
        "Синяя шкатулка [ТВ-2]",
        "Синяя шкатулка (2 сезон, 3 серия)",
        "Ao no Hako 2",
    ):
        assert wanted.is_exact(title), title
    for title in ("Синяя шкатулка", "Синяя шкатулка [ТВ-1]", "Синяя шкатулка 3", "Ao no Hako"):
        assert not wanted.is_exact(title) and not wanted.is_similar(title), title
    # a longer name with the same words and season is only similar: the user checks it
    assert not wanted.is_exact("Синяя шкатулка 2: Весна юности")
    assert wanted.is_similar("Синяя шкатулка 2: Весна юности")


# --- the search on other sites ---------------------------------------------------


@attr.define
class FakeEpisode:
    ordinal: int
    sources: list = attr.Factory(list)

    async def a_get_sources(self) -> list:
        return self.sources


@attr.define
class FakeAnime:
    title: str
    data: dict = attr.Factory(dict)
    episodes: list = attr.Factory(lambda: [FakeEpisode(1), FakeEpisode(2)])

    async def a_get_episodes(self) -> list:
        return self.episodes


@attr.define
class FakeResult:
    title: str
    url: str
    anime: FakeAnime

    async def a_get_anime(self) -> FakeAnime:
        return self.anime


def result(title: str, mal_id: int | None = None, original: str = "") -> FakeResult:
    data = {key: value for key, value in (("mal_id", mal_id), ("original", original)) if value}
    return FakeResult(title, f"/anime/{title}", FakeAnime(title, data))


class FakeSite:
    """Search results by query, any query if `None` is a key. A site raising the error is down."""

    def __init__(self, results: dict[str | None, list[FakeResult]] | Exception) -> None:
        self.results = results
        self.queries: list[str] = []

    async def a_search(self, query: str) -> list[FakeResult]:
        self.queries.append(query)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results.get(query, self.results.get(None, []))


def find(monkeypatch, sites: dict[str, FakeSite], queries: list[str], **kwargs: object):
    monkeypatch.setattr(subtitles, "get_extractor_modules", lambda: ["here", *sites])
    monkeypatch.setattr(
        subtitles,
        "dynamic_load_extractor_module",
        lambda name: SimpleNamespace(Extractor=lambda **_http: sites[name]),
    )
    answers: list[subtitles.SiteSearch] = []
    session = SimpleNamespace(http=None, http_async=None)
    found = asyncio.run(
        subtitles.find_titles(session, "here", queries, queries, on_site=answers.append, **kwargs)  # type: ignore[arg-type]
    )
    return {site.extractor_name: site for site in found}, {answer.name: answer for answer in answers}


def test_found_by_the_name_in_the_search_results(monkeypatch):
    sites = {"other": FakeSite({None: [result("Синяя шкатулка"), result("Синяя шкатулка [ТВ-2]")]})}
    found, answers = find(monkeypatch, sites, ["Синяя шкатулка 2"])
    assert found["other"].item.title == "Синяя шкатулка [ТВ-2]" and found["other"].exact
    assert sorted(found["other"].episodes) == [1, 2]
    assert answers["other"].state == "found"


def test_found_by_the_mal_id_of_the_title_page(monkeypatch):
    sites = {"other": FakeSite({None: [result("Голубая коробка", mal_id=7), result("Голубая коробка 2", mal_id=8)]})}
    found, _ = find(monkeypatch, sites, ["Синяя шкатулка 2"], mal_id=8)
    assert found["other"].item.title == "Голубая коробка 2" and found["other"].exact


def test_another_season_is_not_taken(monkeypatch):
    """The page tells another MAL entry: not even a similar name is taken then."""
    sites = {"other": FakeSite({None: [result("Синяя шкатулка 2: Весна юности", mal_id=7)]})}
    found, answers = find(monkeypatch, sites, ["Синяя шкатулка 2"], mal_id=8, similar=True)
    assert found == {} and answers["other"].state == "missing"


def test_a_similar_name_only_when_asked(monkeypatch):
    sites = {"other": FakeSite({None: [result("Синяя шкатулка 2: Весна юности")]})}
    assert find(monkeypatch, sites, ["Синяя шкатулка 2"])[0] == {}
    found, _ = find(monkeypatch, sites, ["Синяя шкатулка 2"], similar=True)
    assert found["other"].item.title == "Синяя шкатулка 2: Весна юности" and not found["other"].exact


def test_the_original_name_from_one_site_finds_it_on_another(monkeypatch):
    russian = FakeSite({"Синяя шкатулка 2": [result("Синяя шкатулка 2", original="Ao no Hako 2nd Season")]})
    original_only = FakeSite({"Ao no Hako 2nd Season": [result("Ao no Hako 2nd Season")]})
    found, _ = find(monkeypatch, {"a": russian, "b": original_only}, ["Синяя шкатулка 2"])
    assert set(found) == {"a", "b"} and found["b"].exact
    assert original_only.queries == ["Синяя шкатулка 2", "Ao no Hako 2nd Season"]


def test_a_site_that_fails_does_not_stop_the_others(monkeypatch):
    sites = {"down": FakeSite(ConnectionError("refused")), "up": FakeSite({None: [result("Синяя шкатулка 2")]})}
    found, answers = find(monkeypatch, sites, ["Синяя шкатулка 2"])
    assert set(found) == {"up"}
    assert (answers["down"].state, answers["down"].error) == ("failed", "ConnectionError")


def test_only_subtitles_are_taken_from_other_sites():
    ordinal = 3
    episode = FakeEpisode(
        ordinal, [source("Озвучка AniLibria"), source("Субтитры Crunchyroll"), source("Субтитри Morys")]
    )
    site = ForeignTitle("other", result("Синяя шкатулка 2"), FakeAnime("Синяя шкатулка 2"), {ordinal: episode})
    found = asyncio.run(subtitles.foreign_sources([site], ordinal))
    assert [s.source.title for s in found] == ["Субтитры Crunchyroll", "Субтитри Morys"]
    assert all(s.site is site and s.ordinal == ordinal for s in found)
    assert asyncio.run(subtitles.foreign_sources([site], ordinal + 1)) == []
