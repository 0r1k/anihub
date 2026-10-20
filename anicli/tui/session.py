"""UI-agnostic application state: settings, extractor and shared http clients."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anicli_api._http import HTTPAsync, HTTPSync

from anicli.common.config import get_config_dir
from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules
from anicli.common.mal import MalClient
from anicli.common.players import PLAYERS, player_command

from .i18n import DEFAULT_LANGUAGE, LANGUAGES

if TYPE_CHECKING:
    from anicli_api.base import T_KW_HTTPS, BaseExtractor

logger = logging.getLogger(__name__)
QUALITIES = (2160, 1080, 720, 480, 360)
# fields editable in the settings screen and stored between runs
PERSISTENT_FIELDS = (
    "language",
    "extractor_name",
    "quality",
    "player",
    "player_command",
    "player_args",
    "m3u_size",
    "timeout",
    "proxy",
    "mal_client_id",
    "mal_auto_update",
    "theme",
    "posters",
    "poster_render",
    "subtitles_first",
    "ongoing_filter",
)


@dataclass
class Settings:
    language: str = DEFAULT_LANGUAGE
    extractor_name: str = "animego"
    # preferred quality, fallback to the closest available
    quality: int = 1080
    # mpv, vlc or custom
    player: str = "mpv"
    # command for the custom player, eg: "celluloid" or "/usr/bin/haruna"
    player_command: str = ""
    player_args: str = ""
    # max episodes per player playlist in batch mode
    m3u_size: int = 6
    timeout: float = 60
    proxy: str | None = None
    # MyAnimeList API client id, see anicli.common.mal
    mal_client_id: str = ""
    # count watched episodes and change the status on MyAnimeList
    mal_auto_update: bool = True
    # textual theme name chosen in the command palette (ctrl+p), empty for the default one
    theme: str = ""
    # show posters on the title page (textual-image)
    posters: bool = True
    # graphics (kitty protocol / sixel), auto (no sixel), blocks: see anicli.tui.posters.make_widget
    poster_render: str = "graphics"
    # players with subtitles (original audio) on top of the dub list
    subtitles_first: bool = True
    # ongoing tab filter: all, airing, announced
    ongoing_filter: str = "airing"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: CookieJar | None = None

    @staticmethod
    def path() -> Path:
        return get_config_dir() / "settings.json"

    @classmethod
    def load(cls, **overrides: Any) -> Settings:
        """Saved settings (defaults if missing or broken), `overrides` with None values are ignored."""
        data: dict[str, Any] = {}
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
            # renamed in 7.0: player is not mpv only anymore
            if "mpv_opts" in raw:
                raw.setdefault("player_args", raw["mpv_opts"])
            data = {k: v for k, v in raw.items() if k in PERSISTENT_FIELDS}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            logger.warning("broken settings file %s, use defaults", cls.path(), exc_info=True)
        if data.get("extractor_name") not in get_extractor_modules():
            data.pop("extractor_name", None)
        if data.get("player") not in PLAYERS:
            data.pop("player", None)
        if data.get("language") not in LANGUAGES:
            data.pop("language", None)
        data.update({k: v for k, v in overrides.items() if v is not None})
        try:
            return cls(**data)
        except TypeError:
            return cls(**{k: v for k, v in overrides.items() if v is not None})

    @property
    def player_executable(self) -> str:
        return player_command(self.player, self.player_command)

    def save(self) -> None:
        data = {k: getattr(self, k) for k in PERSISTENT_FIELDS}
        self.path().write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


class Session:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http, self.http_async = self._make_clients()
        self.extractor = self._make_extractor(settings.extractor_name)
        self.mal = self._make_mal()

    @property
    def http_kwargs(self) -> T_KW_HTTPS:
        return {"http": self.http, "http_async": self.http_async}

    def _make_clients(self) -> tuple[HTTPSync, HTTPAsync]:
        s = self.settings
        kwargs = {"headers": s.headers, "timeout": s.timeout, "cookies": s.cookies, "proxy": s.proxy or None}
        return HTTPSync(**kwargs), HTTPAsync(**kwargs)

    def _make_mal(self) -> MalClient:
        s = self.settings
        return MalClient(s.mal_client_id, proxy=s.proxy, timeout=s.timeout)

    @property
    def mal_tracking(self) -> bool:
        return self.mal.logged_in and self.settings.mal_auto_update

    def _make_extractor(self, name: str) -> BaseExtractor:
        extractor_cls = dynamic_load_extractor_module(name).Extractor
        return extractor_cls(http_client=self.http, http_async_client=self.http_async)

    def set_extractor(self, name: str) -> None:
        self.extractor = self._make_extractor(name)
        self.settings.extractor_name = name

    async def set_network(self, *, proxy: str | None, timeout: float) -> None:
        """Rebuild http clients (httpx does not allow change proxy of the existing client)."""
        self.settings.proxy = proxy
        self.settings.timeout = timeout
        old = self.http, self.http_async, self.mal
        self.http, self.http_async = self._make_clients()
        self.extractor = self._make_extractor(self.settings.extractor_name)
        self.mal = self._make_mal()
        old[0].close()
        await old[1].aclose()
        await old[2].aclose()

    async def aclose(self) -> None:
        self.http.close()
        await self.http_async.aclose()
        await self.mal.aclose()
