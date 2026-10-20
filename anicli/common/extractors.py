import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

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


# extractors of anihub itself go together with the anicli-api ones (a local name wins)
LOCAL_PACKAGE = "anicli.extractors"
API_PACKAGE = "anicli_api.source"


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
