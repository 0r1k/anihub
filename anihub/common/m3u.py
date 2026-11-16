import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from anicli_api.player.base import Video

_M3U_HEADER = "#EXTM3U"
_M3U_ITEM = "#EXTINF:0,{name}\n{url}"
_M3U_SEP = "\n\n"
# spaces and control characters: a line break in a link adds a playlist entry
_NOT_IN_URL = re.compile(r"[\s\x00-\x1f\x7f]")


class UnsafeVideoUrlError(ValueError):
    """The video link from the site is not a plain http(s) url: the player could take it for a file or an option."""


def one_line(text: str) -> str:
    """Text from a site as a single line: a line break would start a new playlist entry or HTTP header."""
    return " ".join(str(text).split())


def checked_url(url: str) -> str:
    """The video url if it is http(s) without spaces or control characters, else UnsafeVideoUrlError."""
    try:
        scheme = urlsplit(url).scheme
    except ValueError:  # a broken host ("http://[x/"): a plain ValueError would end as a playback error
        scheme = ""
    if scheme.lower() not in ("http", "https") or _NOT_IN_URL.search(url):
        msg = f"unsafe video url: {url!r}"
        raise UnsafeVideoUrlError(msg)
    return url


def generate_m3u_str_playlist(videos: Sequence["Video"], titles: Sequence[str]) -> str:
    if len(videos) != len(titles):
        msg = f"Length mismatch: {len(videos)} videos and {len(titles)} titles provided."
        raise ValueError(msg)
    playlist_items = [_M3U_HEADER]
    playlist_items.extend(
        _M3U_ITEM.format(name=one_line(title), url=checked_url(video.url))
        for video, title in zip(videos, titles, strict=True)
    )
    return _M3U_SEP.join(playlist_items)
