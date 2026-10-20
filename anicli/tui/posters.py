"""Title posters via textual-image: kitty graphics protocol, sixel or colored half blocks as a fallback.

textual-image queries the terminal on import, before Textual takes over stdin: this module must be
imported before the app runs (it is, through the screens imports).
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx

if TYPE_CHECKING:
    from textual.widget import Widget

logger = logging.getLogger(__name__)

try:
    from PIL import Image as PILImage
    from textual_image.renderable import Image as DetectedRenderable
    from textual_image.renderable import SixelImage as SixelRenderable
    from textual_image.widget import HalfcellImage
    from textual_image.widget import Image as PosterImage
except Exception:
    # not installed or the terminal query failed: titles are shown without posters
    logger.debug("textual-image is not available, posters are disabled", exc_info=True)
    PILImage = None  # type: ignore[assignment]
    PosterImage = HalfcellImage = None  # type: ignore[assignment,misc]

AVAILABLE = PosterImage is not None
# settings values: how to draw posters
RENDER_MODES = ("graphics", "auto", "blocks")
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


def uses_sixel() -> bool:
    return AVAILABLE and DetectedRenderable is SixelRenderable


def make_widget(render: str = "graphics", **kwargs: Any) -> Widget | None:
    """Poster widget for the render mode of the settings.

    - graphics (default): the best the terminal supports (kitty graphics protocol, sixel, half blocks)
    - blocks: colored half blocks, drawn by Textual itself
    - auto: graphics, but half blocks instead of sixel. Sixel data is written to the terminal around Textual:
      when the poster changes under a dialog (Konsole) the terminal scrolls and the layout breaks
    """
    if PosterImage is None:
        return None
    if render == "blocks" or (render != "graphics" and uses_sixel()):
        return HalfcellImage(**kwargs)
    return PosterImage(**kwargs)
