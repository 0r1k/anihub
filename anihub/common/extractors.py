import importlib.util
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from anicli_api.base import (
        BaseAnime,
        BaseEpisode,
        BaseExtractor,
        BaseOngoing,
        BaseSearch,
        BaseSource,
    )


class ExtractorLikeModule(Protocol):
    Extractor: type["BaseExtractor"]
    Search: type["BaseSearch"]
    Ongoing: type["BaseOngoing"]
    Anime: type["BaseAnime"]
    Episode: type["BaseEpisode"]
    Source: type["BaseSource"]


logger = logging.getLogger(__name__)

# extractors of anihub itself go together with the anicli-api ones (a local name wins)
LOCAL_PACKAGE = "anihub.extractors"
API_PACKAGE = "anicli_api.source"
# rookiepy puts a domain into its SQL query as is
_PLAIN_HOST = re.compile(r"[a-z0-9.-]+")


def _package_modules(package_name: str) -> list[str]:
    spec = importlib.util.find_spec(package_name)
    if not spec or not spec.submodule_search_locations:
        return []
    package_path = Path(spec.submodule_search_locations[0])
    # Filter: must be .py, not dunder, not private
    return [p.stem.replace("_", "-") for p in package_path.glob("*.py") if not p.name.startswith("_")]


# guaranteed not will be updated in runtime, cache it for slightly increase speed
@lru_cache(maxsize=1)
def get_extractor_modules() -> list[str]:
    """Available source extractors, sorted by name."""
    return sorted({*_package_modules(API_PACKAGE), *_package_modules(LOCAL_PACKAGE)})


def dynamic_load_extractor_module(source_name: str) -> ExtractorLikeModule:
    if source_name not in get_extractor_modules():
        msg = f"Extractor module {source_name} not exists in anicli-api"
        raise NameError(msg)
    module_name = source_name.replace("-", "_")
    package = LOCAL_PACKAGE if source_name in _package_modules(LOCAL_PACKAGE) else API_PACKAGE
    module = importlib.import_module(f"{package}.{module_name}")
    return cast(ExtractorLikeModule, module)


# second levels under a two-letter country domain: 'example.co.uk', 'anitube.in.ua'. Any other short second
# level ('abc.ru' with 'www.' before it) is a registered name: there is no public suffix list in stdlib.
_SECOND_LEVELS = frozenset({"ac", "co", "com", "edu", "gov", "in", "net", "org"})

# Hosts that the sources and their players request besides BASE_URL (the same http client sends the cookies
# there): API hosts, players, mirrors. Taken from anicli-api (source/, player/) and anihub/extractors;
# tests/test_cookies.py checks that every host found in the anicli-api sources is covered.
EXTRA_HOSTS = (
    "api.yani.tv",  # yummy-anime API
    "yummy-anime.ru",
    "ru.yummyani.me",
    "api.animevost.org",
    "aniliberty.top",  # anilibria API
    "anilibria.top",
    "api.cdnlibs.org",  # anilib.me API and video hosts
    "video1.cdnlibs.org",
    "v3.animelib.org",
    "animego.org",  # a mirror of animego.me
    "kodik.info",  # players
    "kodikplayer.com",
    "aniqit.com",
    "aniboom.one",
    "video.sibnet.ru",
    "player.aksor.tv",
    "plapi.cdnvideohub.com",
    "play.dreamerscast.com",
)


def _site_domain(host: str) -> str:
    """'api.animevost.org' -> 'animevost.org': the site with every subdomain (old.yummyani.me after a redirect).

    rookiepy matches the cookie domain with `LIKE '%domain%'`, so a cookie set on '.abc.ru' is found by 'abc.ru'
    for 'www.abc.ru' too.
    """
    labels = host.split(".")
    if all(label.isdigit() for label in labels):
        return host  # an IP address: a part of it would match other hosts
    size = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVELS else 2  # noqa: PLR2004
    return ".".join(labels[-size:])


def source_domains() -> list[str]:
    """Domains of the sites of all sources: browser cookies are read for them only, not for every site.

    All sources, not only the chosen one: it can be changed in the settings (F2), and titles from the history
    and MyAnimeList links open with their own source. Players and API hosts are included, see EXTRA_HOSTS.
    """
    hosts: set[str | None] = set(EXTRA_HOSTS)
    for name in get_extractor_modules():
        try:
            base_url = dynamic_load_extractor_module(name).Extractor.BASE_URL
        except Exception:
            logger.warning("extractor %s is not loaded", name, exc_info=True)
            continue
        hosts.add(urlsplit(base_url).hostname if isinstance(base_url, str) else None)
    return sorted({_site_domain(h.rstrip(".")) for h in hosts if h and _PLAIN_HOST.fullmatch(h)})
