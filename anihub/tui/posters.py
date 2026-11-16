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
import time
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

    from textual.drivers import win32  # noqa: PLC0415 (Windows only)

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
    PosterImage = None  # type: ignore[assignment]
    DetectedRenderable = SixelRenderable = TGPRenderable = None  # type: ignore[assignment,misc]

AVAILABLE = PosterImage is not None
# the terminal draws real images: kitty graphics protocol or sixel (otherwise colored half blocks)
GRAPHICS = AVAILABLE and DetectedRenderable in (SixelRenderable, TGPRenderable)
# enough for a sidebar poster, less data to send to the terminal on every redraw
_MAX_SIZE = (400, 600)
_CACHE_SIZE = 32
_cache: OrderedDict[str, Any] = OrderedDict()
# posters and avatars are a few hundred kilobytes: a bigger answer is not a poster
_MAX_BYTES = 5 * 1024 * 1024
# a few megabytes of PNG can hold a gigapixel image, Pillow only warns up to ~179 megapixels: anything larger
# than this (~64 MB decoded) is refused. JPEG is checked at its reduced decoding scale, see `_decode`
_MAX_PIXELS = 4096 * 4096
# the whole download: the site client retries a dead host for minutes, the poster is not worth that
_FETCH_TIMEOUT = 20
# a failed poster is not downloaded again for a while, or a dead url waits for the timeouts on every open.
# A network error is likely to pass: retried after a minute, the delay doubles with every failure in a row.
# A missing or broken image (404, not an image, too large) waits the longest delay at once
_RETRY_AFTER = 60
_RETRY_AFTER_MAX = 3600
_FAILED_SIZE = 256
# url -> (monotonic time of the next attempt, failures in a row)
_failed: OrderedDict[str, tuple[float, int]] = OrderedDict()
# statuses of a server that is busy, not of a missing image
# 401/403/451 too: a block page or a geo restriction is what a proxy or another network fixes
_TRANSIENT_STATUSES = (401, 403, 408, 425, 429, 451)


class _BrokenPosterError(Exception):
    """The answer is not a usable image: too large or undecodable."""


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
    # JPEG is decoded right away at a reduced scale (1/2…1/8): a huge photo costs no more than a poster.
    # Twice the poster size, as thumbnail() itself does, keeps the quality of the final resize
    image.draft("RGB", (_MAX_SIZE[0] * 2, _MAX_SIZE[1] * 2))
    if image.width * image.height > _MAX_PIXELS:
        msg = f"{image.width}x{image.height} pixels"
        raise _BrokenPosterError(msg)
    image.thumbnail(_MAX_SIZE)
    return image.convert("RGB")


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    # identity and raw bytes: a compressed answer (httpx decodes gzip/br on its own) could expand a few
    # kilobytes into gigabytes inside one chunk, before the size check below. Images are compressed already
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}, follow_redirects=True) as resp:
        resp.raise_for_status()
        encoding = resp.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            msg = f"content encoding {encoding}"
            raise _BrokenPosterError(msg)
        length = resp.headers.get("content-length", "")
        if length.isdecimal() and int(length) > _MAX_BYTES:
            msg = f"{length} bytes"
            raise _BrokenPosterError(msg)
        data = bytearray()
        async for chunk in resp.aiter_raw():
            data += chunk
            if len(data) > _MAX_BYTES:
                msg = f"more than {_MAX_BYTES} bytes"
                raise _BrokenPosterError(msg)
    return bytes(data)


async def _load(client: httpx.AsyncClient, url: str) -> Any:
    data = await asyncio.wait_for(_download(client, url), _FETCH_TIMEOUT)
    try:
        return await asyncio.to_thread(_decode, data)
    except _BrokenPosterError:
        raise
    except Exception as e:  # Pillow raises errors of many kinds on broken files
        raise _BrokenPosterError(repr(e)) from e


def forget_failures() -> None:
    """Retry the failed posters right away: the network changed (a proxy was set), the old failures may be gone."""
    _failed.clear()


def _is_lasting(error: Exception) -> bool:
    """A missing or broken image stays so; network errors, timeouts and busy servers pass."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status < httpx.codes.INTERNAL_SERVER_ERROR and status not in _TRANSIENT_STATUSES
    return isinstance(error, _BrokenPosterError)


def _remember_failure(url: str, error: Exception) -> None:
    _, failures = _failed.pop(url, (0.0, 0))
    delay = _RETRY_AFTER_MAX if _is_lasting(error) else min(_RETRY_AFTER_MAX, _RETRY_AFTER * 2**failures)
    _failed[url] = (time.monotonic() + delay, failures + 1)
    while len(_failed) > _FAILED_SIZE:
        _failed.popitem(last=False)


async def fetch(client: httpx.AsyncClient, url: str) -> Any:
    """Downloaded PIL image (cached), None if posters are unavailable, the image is broken or failed recently.

    Never raises (but CancelledError): a poster is not worth an error on the screen.
    """
    if not AVAILABLE or not url:
        return None
    if url in _cache:
        _cache.move_to_end(url)
        return _cache[url]
    if url in _failed and time.monotonic() < _failed[url][0]:
        return None
    try:
        image = await _load(client, url)
    except Exception as e:
        _remember_failure(url, e)
        logger.warning("failed to load poster %s: %r", url, e)
        return None
    _failed.pop(url, None)
    _cache[url] = image
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return image


def make_widget(**kwargs: Any) -> Widget | None:
    """Poster widget in the best the terminal can draw, None without textual-image (no poster in the layout)."""
    return PosterImage(**kwargs) if AVAILABLE else None
