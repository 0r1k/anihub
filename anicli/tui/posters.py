"""Title posters via textual-image: kitty graphics protocol or sixel, colored half blocks as a fallback.

The old Windows console (conhost) and other terminals without graphics can draw half blocks only.

textual-image queries the terminal on import, before Textual takes over stdin: this module must be
imported before the app runs (it is, through the screens imports).
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import logs

if TYPE_CHECKING:
    from textual.widget import Widget

logger = logging.getLogger(__name__)


@contextmanager
def _console_escape_sequences() -> Iterator[None]:
    """Let the Windows console understand the query textual-image sends on import.

    Textual turns escape sequences on only when the app starts. Until then cmd.exe prints them as text
    and does not reply: garbage on the screen and a 2 seconds timeout on every start.
    """
    out = sys.__stdout__
    if sys.platform != "win32" or out is None or not out.isatty():
        yield
        return

    from textual.drivers import win32

    mode = win32.get_console_mode(out)
    win32.set_console_mode(out, mode | win32.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    try:
        yield
    finally:
        win32.set_console_mode(out, mode)


# a failed terminal query is logged with a traceback: without handlers yet it would go to stderr
logs.setup()

try:
    with _console_escape_sequences():
        from PIL import Image as PILImage
        from textual_image.renderable import Image as DetectedRenderable
        from textual_image.renderable import SixelImage as SixelRenderable
        from textual_image.renderable import TGPImage as TGPRenderable
        from textual_image.widget import Image as PosterImage
except Exception:
    # not installed or the terminal query failed: titles are shown without posters
    logger.debug("textual-image is not available, posters are disabled", exc_info=True)
    PILImage = None  # type: ignore[assignment]
    PosterImage = None  # type: ignore[assignment,misc]
    DetectedRenderable = SixelRenderable = TGPRenderable = None  # type: ignore[assignment,misc]

AVAILABLE = PosterImage is not None
# the terminal draws real images: kitty graphics protocol or sixel (otherwise colored half blocks)
GRAPHICS = AVAILABLE and DetectedRenderable in (SixelRenderable, TGPRenderable)
# enough for a sidebar poster, less data to send to the terminal on every redraw
_MAX_SIZE = (400, 600)
_CACHE_SIZE = 32
_cache: OrderedDict[str, Any] = OrderedDict()


def resolve_url(url: str, page_url: str, base_url: str = "") -> str:
    """Extractor thumbnails can be protocol-relative (//host/x.jpg) or site-relative (/x.jpg),
    item urls can be relative to the extractor `BASE_URL` too. Empty string if it cannot be resolved.
    """
    if not url:
        return ""
    resolved = urljoin(urljoin(urljoin("https://", base_url), page_url), url)
    return resolved if urlsplit(resolved).netloc else ""


def _decode(data: bytes) -> Any:
    image = PILImage.open(io.BytesIO(data))
    image.load()
    image.thumbnail(_MAX_SIZE)
    return image.convert("RGB")


async def fetch(client: httpx.AsyncClient, url: str) -> Any:
    """Downloaded PIL image (cached), None if posters are unavailable or the image is broken."""
    if not AVAILABLE or not url:
        return None
    if url in _cache:
        _cache.move_to_end(url)
        return _cache[url]
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        image = await asyncio.to_thread(_decode, resp.content)
    except (httpx.HTTPError, OSError, ValueError, PILImage.DecompressionBombError) as e:
        logger.warning("failed to load poster %s: %s", url, e)
        return None
    _cache[url] = image
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return image


def make_widget(**kwargs: Any) -> Widget | None:
    """Poster widget in the best the terminal can draw, None without textual-image (no poster in the layout)."""
    return PosterImage(**kwargs) if AVAILABLE else None
