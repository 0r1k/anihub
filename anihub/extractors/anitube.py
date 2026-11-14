"""AniTube (https://anitube.in.ua): anime with ukrainian dubs and subtitles.

The site runs DLE. Players of a title are loaded by ajax as a tree:
type (СУБТИТРИ / ОЗВУЧЕННЯ) -> team (Рідний Голос) -> player (ПЛЕЄР FENIX / MOON / ASHDI) -> episodes
with an iframe url per episode. A source is "<type> <team>" on a player host.

Players:
- fenixplay.xyz: the page has the HLS master url and a media access token (variants and segments are public)
- ashdi.vip, moonanime.art, tortuga: Playerjs-like pages, the first m3u8/mp4 link is taken
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from anicli_api.base import BaseAnime, BaseEpisode, BaseExtractor, BaseOngoing, BaseSearch, BaseSource
from anicli_api.player.base import Video
from attrs import define, field

BASE_URL = "https://anitube.in.ua"
PLAYLISTS_URL = f"{BASE_URL}/engine/ajax/playlists.php"
# the catalog sorted by the last update: recently released episodes first
ONGOING_PAGES = 2
# the default client of anicli-api is a mobile browser: the site serves another (mobile) markup to it
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
_HEADERS = {"Referer": f"{BASE_URL}/", "User-Agent": _USER_AGENT}

_STORY = re.compile(r'<article class="story">(.*?)</article>', re.S)
_STORY_TITLE = re.compile(r'<h2[^>]*>\s*<a href="([^"]+)">(.*?)</a>', re.S)
_STORY_POSTER = re.compile(r'<img (?:data-)?src="([^"]+)"')
_STORY_ADDED = re.compile(r'class="story_link"><a[^>]*>(.*?)</a>', re.S)
_STORY_EPISODES = re.compile(r"<dt>Серій:</dt>\s*([^<]+)")
_STORY_YEAR = re.compile(r"/xfsearch/year/(\d{4})/")

# <h2 id-mal="" > Крижані стіни (2 сезон) <ul class="reset moder">
_PAGE_TITLE = re.compile(r'<h2 id-mal="(\d*)"[^>]*>(.*?)<', re.S)
_PAGE_OG_TITLE = re.compile(r'property="og:title" content="([^"]+)"')
_PAGE_DESCRIPTION = re.compile(r'<div class="my-text">(.*?)</div>', re.S)
_PAGE_POSTER = re.compile(r'property="og:image" content="([^"]+)"')
_PAGE_NEWS_ID = re.compile(r'data-news_id="(\d+)"')
_PAGE_XFNAME = re.compile(r'data-xfname="([^"]+)"')
_PAGE_HASH = re.compile(r"dle_login_hash\s*=\s*'([^']+)'")
_PAGE_STATUS = re.compile(r'id="anime-status"\s*data-status="([^"]*)"\s*data-episode="([^"]*)"')

_LIST_ITEM = re.compile(r'<li data-id="([\d_]+)"[^>]*>(.*?)</li>', re.S)
_VIDEO_ITEM = re.compile(r'<li data-file="([^"]+)" data-id="([\d_]+)"[^>]*>(.*?)</li>', re.S)
_NUMBER = re.compile(r"\d+")
# "ПЛЕЄР FENIX", "Плєер mp4", "PLAYER ..."
_PLAYER_LABEL = re.compile(r"^(пл[еєі]+[еєі]?р|player)\b", re.I)
# working players first: ashdi is often down (the site shows a warning about it)
_PLAYER_ORDER = ("fenixplay", "moonanime", "tortuga")
# the http client retries connection errors: a dead player would hold the playback for minutes
PLAYER_TIMEOUT = 20

_FENIX_ACCESS = re.compile(r"window\.FENIX_MEDIA_ACCESS\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
_FENIX_MASTER = re.compile(r'"hls_master_url"\s*:\s*"([^"]+)"')
_RESOLUTION = re.compile(r"RESOLUTION=\d+x(\d+)")
_PLAYERJS_FILE = re.compile(r"""file\s*:\s*['"](https?://[^'"]+)['"]""")
_DIRECT_VIDEO = re.compile(r"""https?://[^'"\s<>]+?\.(m3u8|mp4)(?:\?[^'"\s<>]*)?""")


def _text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _absolute(url: str) -> str:
    return urljoin(BASE_URL + "/", url) if url else ""


# --- parsing -----------------------------------------------------------------


def _parse_stories(page: str) -> list[dict[str, Any]]:
    """Search results and catalog pages share the same story blocks."""
    stories = []
    for block in _STORY.findall(page):
        title = _STORY_TITLE.search(block)
        if not title:
            continue
        poster = _STORY_POSTER.search(block)
        added = _STORY_ADDED.search(block)
        episodes = _STORY_EPISODES.search(block)
        year = _STORY_YEAR.search(block)
        stories.append(
            {
                "url": title.group(1),
                "title": _text(title.group(2)),
                "thumbnail": _absolute(poster.group(1)) if poster else "",
                "data": {
                    # "Додано 4 СУБ (Morys) / 4 СУБ 4 ОЗВ (Рідний Голос)": changes with every release
                    "added": _text(added.group(1)) if added else "",
                    # "3 з 12 (24 хв.)"
                    "episodes_text": _text(episodes.group(1)) if episodes else "",
                    "year": int(year.group(1)) if year else None,
                },
            }
        )
    return stories


def _parse_anime(page: str, url: str) -> dict[str, Any]:
    title = _PAGE_TITLE.search(page)
    og_title = _PAGE_OG_TITLE.search(page)
    name = _text(title.group(2)) if title else ""
    if not name and og_title:
        name = _text(og_title.group(1)).removesuffix(" українською онлайн")
    description = _PAGE_DESCRIPTION.search(page)
    poster = _PAGE_POSTER.search(page)
    news_id = _PAGE_NEWS_ID.search(page)
    xfname = _PAGE_XFNAME.search(page)
    user_hash = _PAGE_HASH.search(page)
    status = _PAGE_STATUS.search(page)
    if not news_id:
        msg = f"no player on the page {url}"
        raise ValueError(msg)
    return {
        "title": name,
        "thumbnail": poster.group(1) if poster else "",
        "description": _text(description.group(1)) if description else "",
        "url": url,
        "news_id": news_id.group(1),
        "xfname": xfname.group(1) if xfname else "playlist",
        "user_hash": user_hash.group(1) if user_hash else "",
        "data": {
            # the site knows the MAL entry of some titles: used to link it automatically
            "mal_id": int(title.group(1)) if title and title.group(1) else None,
            "status": status.group(1) if status else "",
            "episodes_text": status.group(2) if status else "",
        },
    }


def _label(text: str) -> str:
    text = _text(text)
    return text.capitalize() if text.isupper() else text


# "1 серія", "12 серія": the number is shown by the TUI, the title is left empty
_PLAIN_EPISODE = re.compile(r"^\d+\s*(серія|серия|episode|еп\.?)?$", re.I)
# without the episodes count a jump bigger than this is continuous numbering ("8 серія" -> "49 серія")
_MAX_GAP = 3


def _episodes_total(episodes_text: str) -> int | None:
    """The largest episode number of the title: "9 з 10" -> 10, "4 з ХХ" -> 4, "" -> None."""
    numbers = [int(n) for n in _NUMBER.findall(episodes_text.split("(", 1)[0])]
    return max(numbers) if numbers else None


def _ordinals(names: list[str], total: int | None) -> list[int | None]:
    """Episode numbers of one playlist (a team on a player), in the playlist order.

    Some teams number by the whole franchise: "1 серія" ... "8 серія", "49 серія" is the 9th one.
    A number above the title episodes count (or a big jump when it is unknown) is replaced by the position.
    None: not a numbered episode (ОВА, Фільм, Спешл).
    """
    result: list[int | None] = []
    previous = 0
    for name in names:
        found = _NUMBER.search(name)
        if not found:
            result.append(None)
            continue
        number = int(found.group())
        continuous = number > total if total else number > previous + _MAX_GAP
        if continuous or number <= previous:
            number = previous + 1
        result.append(number)
        previous = number
    return result


def _parse_playlists(response: str, episodes_text: str = "") -> list[dict[str, Any]]:
    """Episodes with their sources: [{"ordinal", "title", "sources": [{"title", "url"}]}] sorted by number."""
    labels = {item_id: _label(text) for item_id, text in _LIST_ITEM.findall(response)}
    playlists: dict[str, list[tuple[str, str]]] = {}
    for file_url, item_id, name in _VIDEO_ITEM.findall(response):
        playlists.setdefault(item_id, []).append((html.unescape(file_url), _text(name)))

    numbered: dict[int, dict[str, Any]] = {}
    special: dict[str, dict[str, Any]] = {}
    total = _episodes_total(episodes_text)
    for item_id, videos in playlists.items():
        parts = item_id.split("_")
        # "0_1_0_1" -> labels of "0_1" (type), "0_1_0" (team), "0_1_0_1" (player)
        path = [labels.get("_".join(parts[: i + 1]), "") for i in range(1, len(parts))]
        source_title = " ".join(p for p in path if p and not _PLAYER_LABEL.match(p))
        for (file_url, name), ordinal in zip(videos, _ordinals([n for _, n in videos], total), strict=True):
            source = {"title": source_title or urlsplit(file_url).netloc, "url": file_url}
            if ordinal is None:
                special.setdefault(name, {"title": name, "sources": []})["sources"].append(source)
                continue
            title = "" if _PLAIN_EPISODE.match(name) else name
            numbered.setdefault(ordinal, {"title": title, "sources": []})["sources"].append(source)

    result = [{**numbered[n], "ordinal": n} for n in sorted(numbered)]
    # ОВА, Фільм, Спешл after the episodes
    last = result[-1]["ordinal"] if result else 0
    result += [{**episode, "ordinal": last + i} for i, episode in enumerate(special.values(), start=1)]
    for episode in result:
        episode["sources"].sort(key=lambda s: _player_rank(s["url"]))
    return result


def _player_rank(url: str) -> int:
    host = urlsplit(url).netloc
    return next((i for i, name in enumerate(_PLAYER_ORDER) if name in host), len(_PLAYER_ORDER))


def _playlists_params(anime: Anime) -> dict[str, str]:
    return {"news_id": anime.news_id, "xfield": anime.xfname, "user_hash": anime.user_hash}


def _fenix_videos(page: str) -> list[Video]:
    access = _FENIX_ACCESS.search(page)
    master = _FENIX_MASTER.search(page)
    if not access or not master:
        msg = "fenixplay: no video on the page"
        raise ValueError(msg)
    config = json.loads(access.group(1))
    url = urlsplit(master.group(1).replace("\\/", "/"))
    # media-delivery.js: the gateway origin + url prefix + media path, the token as a query param
    gateway = config["origin"].rstrip("/") + (config.get("url_prefix") or "").rstrip("/") + url.path
    query = f"{url.query}&" if url.query else ""
    return [Video(type="m3u8", quality=1080, url=f"{gateway}?{query}token={config['token']}")]


def _generic_videos(page: str, player_url: str) -> list[Video]:
    page = page.replace("\\/", "/")
    found = _PLAYERJS_FILE.search(page) or _DIRECT_VIDEO.search(page)
    if not found:
        msg = f"{urlsplit(player_url).netloc}: no video link on the player page (blocked or not supported)"
        raise ValueError(msg)
    url = found.group(1) if found.re is _PLAYERJS_FILE else found.group(0)
    kind = "mp4" if ".mp4" in urlsplit(url).path else "m3u8"
    origin = "{0.scheme}://{0.netloc}/".format(urlsplit(player_url))
    return [Video(type=kind, quality=1080, url=url, headers={"Referer": origin})]


async def _fenix_quality(source: Source, video: Video) -> Video:
    """The highest variant of the master playlist (mpv picks the stream itself, this is for the quality label)."""
    try:
        resp = await source.http_async.get(video.url, headers={"User-Agent": _USER_AGENT})
        heights = [int(h) for h in _RESOLUTION.findall(resp.text)]
    except Exception:
        return video
    return Video(type=video.type, quality=max(heights), url=video.url) if heights else video


# --- models ------------------------------------------------------------------


class Extractor(BaseExtractor):
    BASE_URL = BASE_URL

    def _search_form(self, query: str) -> dict[str, str]:
        return {"do": "search", "subaction": "search", "story": query}

    def search(self, query: str) -> list[Search]:
        resp = self.http.post(f"{BASE_URL}/index.php?do=search", data=self._search_form(query), headers=_HEADERS)
        return [Search(**s, **self._kwargs_http) for s in _parse_stories(resp.text)]

    async def a_search(self, query: str) -> list[Search]:
        resp = await self.http_async.post(
            f"{BASE_URL}/index.php?do=search", data=self._search_form(query), headers=_HEADERS
        )
        return [Search(**s, **self._kwargs_http) for s in _parse_stories(resp.text)]

    @staticmethod
    def _ongoing_url(page: int) -> str:
        return f"{BASE_URL}/anime/" if page == 1 else f"{BASE_URL}/anime/page/{page}/"

    def ongoing(self) -> list[Ongoing]:
        stories = []
        for page in range(1, ONGOING_PAGES + 1):
            stories += _parse_stories(self.http.get(self._ongoing_url(page), headers=_HEADERS).text)
        return [Ongoing(**s, **self._kwargs_http) for s in stories]

    async def a_ongoing(self) -> list[Ongoing]:
        stories = []
        for page in range(1, ONGOING_PAGES + 1):
            stories += _parse_stories((await self.http_async.get(self._ongoing_url(page), headers=_HEADERS)).text)
        return [Ongoing(**s, **self._kwargs_http) for s in stories]


class _Item:
    """Search/Ongoing: open the title page."""

    __slots__ = ()

    def get_anime(self) -> Anime:
        resp = self.http.get(self.url, headers=_HEADERS)  # type: ignore[attr-defined]
        return Anime(**_parse_anime(resp.text, self.url), **self._kwargs_http)  # type: ignore[attr-defined]

    async def a_get_anime(self) -> Anime:
        resp = await self.http_async.get(self.url, headers=_HEADERS)  # type: ignore[attr-defined]
        return Anime(**_parse_anime(resp.text, self.url), **self._kwargs_http)  # type: ignore[attr-defined]


@define(kw_only=True)
class Search(_Item, BaseSearch):
    # added: the last released episodes, episodes_text: "3 з 12 (24 хв.)", year
    data: dict[str, Any] = field(factory=dict)

    def __str__(self) -> str:
        return self.title


@define(kw_only=True)
class Ongoing(_Item, BaseOngoing):
    # added: the last released episodes, episodes_text: "3 з 12 (24 хв.)", year
    data: dict[str, Any] = field(factory=dict)

    def __str__(self) -> str:
        added = self.data.get("added", "")
        return f"{self.title} ({added})" if added and added != "Перегляд" else self.title


@define(kw_only=True)
class Anime(BaseAnime):
    url: str
    news_id: str
    xfname: str = "playlist"
    user_hash: str = ""
    data: dict[str, Any] = field(factory=dict)

    def _episodes(self, payload: dict[str, Any]) -> list[Episode]:
        if not payload.get("success"):
            return []
        return [
            Episode(title=e["title"], ordinal=e["ordinal"], sources=e["sources"], **self._kwargs_http)
            for e in _parse_playlists(payload.get("response", ""), self.data.get("episodes_text", ""))
        ]

    def get_episodes(self) -> list[Episode]:
        resp = self.http.get(PLAYLISTS_URL, params=_playlists_params(self), headers={**_HEADERS, "Referer": self.url})
        return self._episodes(resp.json())

    async def a_get_episodes(self) -> list[Episode]:
        resp = await self.http_async.get(
            PLAYLISTS_URL, params=_playlists_params(self), headers={**_HEADERS, "Referer": self.url}
        )
        return self._episodes(resp.json())


@define(kw_only=True)
class Episode(BaseEpisode):
    sources: list[dict[str, str]] = field(factory=list)

    def get_sources(self) -> list[Source]:
        return [Source(title=s["title"], url=s["url"], **self._kwargs_http) for s in self.sources]

    async def a_get_sources(self) -> list[Source]:
        return self.get_sources()


@define(kw_only=True)
class Source(BaseSource):
    def _videos(self, page: str) -> list[Video]:
        if "fenixplay" in urlsplit(self.url).netloc:
            return _fenix_videos(page)
        return _generic_videos(page, self.url)

    def get_videos(self, **_kwargs: Any) -> list[Video]:
        return self._videos(self.http.get(self.url, headers=_HEADERS, timeout=PLAYER_TIMEOUT / 2).text)

    async def a_get_videos(self, **_kwargs: Any) -> list[Video]:
        try:
            resp = await asyncio.wait_for(self.http_async.get(self.url, headers=_HEADERS), PLAYER_TIMEOUT)
        except asyncio.TimeoutError:
            msg = f"{urlsplit(self.url).netloc} did not answer in {PLAYER_TIMEOUT}s (down or blocked in your network)"
            raise TimeoutError(msg) from None
        videos = self._videos(resp.text)
        if "fenixplay" in urlsplit(self.url).netloc:
            videos = [await _fenix_quality(self, v) for v in videos]
        return videos
