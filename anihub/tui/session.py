"""UI-agnostic application state: settings, extractor and shared http clients."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, get_args, get_type_hints

from anicli_api._http import HTTPAsync, HTTPSync

from anihub.common import storage
from anihub.common.config import get_config_dir
from anihub.common.extractors import dynamic_load_extractor_module, get_extractor_modules
from anihub.common.mal import MalClient
from anihub.common.players import PLAYERS, player_command
from anihub.common.stream_proxy import StreamProxy
from anihub.common.utils import validate_proxy_url

from .i18n import DEFAULT_LANGUAGE, LANGUAGES, t

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
    "subtitles_first",
    "ongoing_filter",
    "stream_proxy",
    "show_leaked",
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
    # as the user typed it: a saved one httpx refuses is not used, see Session.proxy
    proxy: str | None = None
    # MyAnimeList API client id, see anihub.common.mal
    mal_client_id: str = ""
    # count watched episodes and change the status on MyAnimeList
    mal_auto_update: bool = True
    # textual theme name chosen in the command palette (ctrl+p), empty for the default one
    theme: str = ""
    # show posters on the title page (textual-image)
    posters: bool = True
    # players with subtitles (original audio) on top of the dub list
    subtitles_first: bool = True
    # ongoing tab filter: all, airing, announced
    ongoing_filter: str = "airing"
    # players load videos through anihub: see anihub.common.stream_proxy
    stream_proxy: bool = True
    # episodes dubbed before their release (1-6 and 12): see anihub.common.episodes
    show_leaked: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    cookies: CookieJar | None = None

    @staticmethod
    def path() -> Path:
        return get_config_dir() / "settings.json"

    @classmethod
    def load(cls, *, read_only: bool = False, **overrides: Any) -> Settings:
        """Saved settings (defaults for missing or invalid values), `overrides` with None values are ignored.

        A broken file is moved aside by `storage`, which keeps the list for the app to tell the user.
        `read_only` (the CLI): a broken file stays in place, so that the TUI finds and reports it.
        """
        raw: dict[str, Any] = storage.read_json(cls.path(), {}, set_aside=not read_only)
        # renamed in 7.0: player is not mpv only anymore
        if "mpv_opts" in raw:
            raw.setdefault("player_args", raw["mpv_opts"])
        data = {k: v for k, v in raw.items() if k in PERSISTENT_FIELDS and _valid_value(k, v)}
        if data.get("extractor_name") not in get_extractor_modules():
            data.pop("extractor_name", None)
        if data.get("player") not in PLAYERS:
            data.pop("player", None)
        if data.get("language") not in LANGUAGES:
            data.pop("language", None)
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    @property
    def player_executable(self) -> str:
        return player_command(self.player, self.player_command)

    def persistent(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in PERSISTENT_FIELDS}

    def save(self) -> None:
        """Replace the file atomically. Raises OSError: the settings stay applied for this run."""
        storage.write_json(self.path(), self.persistent())


def _accepted_types(hint: Any) -> tuple[type, ...]:
    types = get_args(hint) or (hint,)
    # JSON has one kind of numbers: a saved 60 is a fine float
    return (*types, int) if float in types else types


_FIELD_TYPES = {k: _accepted_types(v) for k, v in get_type_hints(Settings).items() if k in PERSISTENT_FIELDS}


def _valid_value(name: str, value: Any) -> bool:
    """`value` has the type of the field: a file edited by hand must not break the start (or the screens later)."""
    types = _FIELD_TYPES[name]
    # bool is an int for isinstance: `"quality": true` is not a quality
    valid = bool in types if isinstance(value, bool) else isinstance(value, types)
    if not valid:
        logger.warning("settings: %s is not %s, the default is used", name, " or ".join(tp.__name__ for tp in types))
    return valid


class NetworkSettingsError(Exception):
    """The http clients cannot be made with this proxy (or timeout): the reason for the user."""


class _Network(NamedTuple):
    # the normalized proxy URL the clients use
    proxy: str | None
    http: HTTPSync
    http_async: HTTPAsync
    mal: MalClient


class Session:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # clients replaced by `set_network`, closed on exit: titles, episodes and workers made before keep them
        self._retired: list[_Network] = []
        # why the saved proxy is not used, see `startup_warnings`
        self._proxy_error: str | None = None
        try:
            network = self._make_network(settings.proxy, settings.timeout, settings.mal_client_id)
        except NetworkSettingsError as e:
            if not settings.proxy:
                raise
            # anihub 8.2 and older saved any scheme: start without the proxy instead of not at all. The settings
            # keep it as typed: the settings screen shows it as an error, the user fixes or clears it there
            logger.warning("the saved proxy is not used: %s", e)
            self._proxy_error = str(e)
            network = self._make_network(None, settings.timeout, settings.mal_client_id)
        # the proxy really used: None if the saved one is refused
        self.proxy, self.http, self.http_async, self.mal = network
        self.extractor = self._make_extractor(settings.extractor_name, self.http, self.http_async)
        self.stream_proxy = StreamProxy()

    @property
    def http_kwargs(self) -> T_KW_HTTPS:
        return {"http": self.http, "http_async": self.http_async}

    def _make_network(self, proxy: str | None, timeout: float, mal_client_id: str) -> _Network:
        """New http clients for the sites and MyAnimeList. Raises NetworkSettingsError."""
        s = self.settings
        try:
            url = validate_proxy_url(proxy) if proxy else None
            kwargs = {"headers": s.headers, "timeout": timeout, "cookies": s.cookies, "proxy": url}
            http, http_async = HTTPSync(**kwargs), HTTPAsync(**kwargs)
            return _Network(url, http, http_async, MalClient(mal_client_id, proxy=url, timeout=timeout))
        except Exception as e:
            # besides the proxy httpx reads the environment when a client is made (proxy variables, SSL_CERT_FILE):
            # whatever it refuses, the caller goes on with the clients it has. The ones made so far have no
            # connections yet, there is nothing to close
            raise NetworkSettingsError(str(e) or type(e).__name__) from e

    @property
    def mal_tracking(self) -> bool:
        return self.mal.logged_in and self.settings.mal_auto_update

    @staticmethod
    def _make_extractor(name: str, http: HTTPSync, http_async: HTTPAsync) -> BaseExtractor:
        extractor_cls = dynamic_load_extractor_module(name).Extractor
        return extractor_cls(http_client=http, http_async_client=http_async)

    def set_extractor(self, name: str) -> None:
        self.extractor = self._make_extractor(name, self.http, self.http_async)
        self.settings.extractor_name = name

    def set_network(self, *, proxy: str | None, timeout: float) -> None:
        """Switch to new http clients: httpx cannot change the proxy or timeout of a client.

        Everything is made before anything is replaced: on NetworkSettingsError the clients and settings stay
        as they were. The MyAnimeList client keeps its Client ID: the account is not a network setting, the
        settings screen compares the Client ID after this.
        """
        network = self._make_network(proxy, timeout, self.mal.client_id)
        extractor = self._make_extractor(self.settings.extractor_name, network.http, network.http_async)
        # objects of open screens (search results, a title page, its episodes) and running workers hold the old
        # clients: closing them now would break those with "client has been closed". A few idle clients until
        # exit cost nothing, and the user changes the network a couple of times per run at most
        self._retired.append(_Network(self.proxy, self.http, self.http_async, self.mal))
        self.proxy, self.http, self.http_async, self.mal = network
        self.extractor = extractor
        self.settings.proxy, self.settings.timeout = proxy, timeout

    def startup_warnings(self) -> list[str]:
        """Problems of the saved settings that the start worked around, for the user.

        Translated on the call: the session is made before the app sets the language.
        """
        if self._proxy_error is None:
            return []
        return [
            t(
                "The saved proxy is not used, anihub connects directly: {error}. Fix it in settings (F2)",
                error=self._proxy_error,
            )
        ]

    async def aclose(self) -> None:
        for network in (*self._retired, _Network(self.proxy, self.http, self.http_async, self.mal)):
            network.http.close()
            await network.http_async.aclose()
            await network.mal.aclose()
        await self.stream_proxy.aclose()
