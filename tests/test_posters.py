"""Posters: the download and the image are limited, a failed url waits before the next attempt."""

from __future__ import annotations

import asyncio
import gzip
import io

import httpx
import pytest

from anihub.tui import posters

ImageFile = pytest.importorskip("PIL.ImageFile")
pytestmark = pytest.mark.skipif(not posters.AVAILABLE, reason="posters need Pillow and textual-image")
URL = "https://posters.example/poster.jpg"


@pytest.fixture(autouse=True)
def clean_caches():
    posters._cache.clear()
    posters._failed.clear()
    yield
    posters._cache.clear()
    posters._failed.clear()


def image_bytes(size: tuple[int, int], kind: str = "PNG") -> bytes:
    out = io.BytesIO()
    posters.PILImage.new("RGB", size, (200, 30, 30)).save(out, kind)
    return out.getvalue()


class Hosting:
    """Answers every request with `answer` (a response, an exception to raise or a coroutine function)."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        if callable(self.answer):
            return await self.answer(request)
        # a fresh unread stream, like a real transport gives: the raw bytes are read from it, once
        return httpx.Response(
            self.answer.status_code, headers=self.answer.headers, stream=httpx.ByteStream(self.answer.content)
        )

    def fetch(self, url: str = URL):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
                return await posters.fetch(client, url)

        return asyncio.run(run())


def _delay(retry_at: float) -> float:
    """Seconds until the retry, to the millisecond: Windows' monotonic clock ticks every ~16 ms, so a delay
    of exactly 3600 s can read as 3600.0000000000005."""
    return round(retry_at - posters.time.monotonic(), 3)


def test_a_poster_is_downloaded_resized_and_cached():
    hosting = Hosting(httpx.Response(200, content=image_bytes((800, 1200))))
    image = hosting.fetch()
    assert image is not None and image.size == (400, 600) and image.mode == "RGB"
    assert hosting.fetch() is image and hosting.requests == 1


def test_a_big_jpeg_is_decoded_at_a_reduced_scale():
    hosting = Hosting(httpx.Response(200, content=image_bytes((3200, 4800), "JPEG")))
    image = hosting.fetch()
    assert image is not None and image.size == (400, 600)


def test_a_declared_size_over_the_limit_is_not_downloaded():
    hosting = Hosting(httpx.Response(200, headers={"Content-Length": str(posters._MAX_BYTES + 1)}, content=b""))
    assert hosting.fetch() is None
    assert URL in posters._failed


def test_a_body_over_the_limit_is_cut_off():
    """No Content-Length: the download stops right after the limit."""
    sent = 0
    chunk = b"\0" * 64 * 1024

    async def body():
        nonlocal sent
        for _ in range(10 * posters._MAX_BYTES // len(chunk)):
            sent += len(chunk)
            yield chunk

    async def answer(_request):
        return httpx.Response(200, content=body())

    assert Hosting(answer).fetch() is None
    assert sent <= posters._MAX_BYTES + len(chunk)
    assert URL in posters._failed


def test_a_decompression_bomb_is_refused(monkeypatch):
    """The pixels are counted from the header, before the image is decoded."""
    loaded = []
    bomb = image_bytes((1001, 1000))
    monkeypatch.setattr(posters, "_MAX_PIXELS", 1000 * 1000)

    def load(image):
        loaded.append(image)

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    assert Hosting(httpx.Response(200, content=bomb)).fetch() is None
    assert loaded == []


def test_a_compressed_answer_is_refused_unread():
    """A few bytes of brotli can decode to hundreds of megabytes: such an answer is not decoded at all."""
    seen = []

    async def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["accept-encoding"])
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"\0" * 10_000_000))

    hosting = Hosting(answer)
    assert hosting.fetch() is None
    assert seen == ["identity"] and URL in posters._failed


def test_a_compressed_answer_is_not_inflated_while_reading(monkeypatch):
    monkeypatch.setattr(posters, "_MAX_BYTES", 1000)
    bomb = gzip.compress(b"\0" * 5_000_000)
    # even if a server ignores the refusal and the header: the raw size is counted, nothing is decoded
    assert len(bomb) < 5000
    assert Hosting(httpx.Response(200, content=bomb)).fetch() is None


@pytest.mark.parametrize("status", [401, 403, 451])
def test_a_blocked_poster_is_retried_soon(status):
    hosting = Hosting(httpx.Response(status))
    assert hosting.fetch() is None
    retry_at, _ = posters._failed[URL]
    assert retry_at - posters.time.monotonic() <= posters._RETRY_AFTER


def test_forgetting_failures_allows_an_immediate_retry():
    hosting = Hosting(httpx.Response(404))
    assert hosting.fetch() is None and hosting.fetch() is None and hosting.requests == 1
    posters.forget_failures()
    assert hosting.fetch() is None and hosting.requests == 2


def test_not_an_image():
    assert Hosting(httpx.Response(200, content=b"<html>not found</html>")).fetch() is None


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(503), httpx.Response(429), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
def test_a_transient_failure_waits_a_minute_then_longer(answer):
    hosting = Hosting(answer)
    assert hosting.fetch() is None
    assert hosting.fetch() is None and hosting.requests == 1  # not asked again at once
    retry_at, failures = posters._failed[URL]
    assert failures == 1 and _delay(retry_at) <= posters._RETRY_AFTER
    posters._failed[URL] = (0.0, failures)  # the delay is over
    assert hosting.fetch() is None and hosting.requests == 2
    retry_at, failures = posters._failed[URL]
    assert failures == 2 and posters._RETRY_AFTER < _delay(retry_at) <= 2 * posters._RETRY_AFTER


@pytest.mark.parametrize("answer", [httpx.Response(404), httpx.Response(200, content=b"garbage")])
def test_a_missing_image_waits_the_longest(answer):
    assert Hosting(answer).fetch() is None
    retry_at, _ = posters._failed[URL]
    assert _delay(retry_at) > posters._RETRY_AFTER_MAX - 60


def test_the_delay_does_not_grow_forever():
    hosting = Hosting(httpx.Response(503))
    for _ in range(20):
        if URL in posters._failed:
            posters._failed[URL] = (0.0, posters._failed[URL][1])
        hosting.fetch()
    retry_at, failures = posters._failed[URL]
    assert failures == 20 and _delay(retry_at) <= posters._RETRY_AFTER_MAX


def test_the_poster_comes_back_after_the_delay():
    """A transient failure does not hide the poster for good."""
    hosting = Hosting(httpx.Response(503))
    assert hosting.fetch() is None
    hosting.answer = httpx.Response(200, content=image_bytes((40, 60)))
    assert hosting.fetch() is None  # still waiting
    posters._failed[URL] = (0.0, posters._failed[URL][1])
    assert hosting.fetch() is not None
    assert URL not in posters._failed


def test_a_slow_download_is_given_up(monkeypatch):
    monkeypatch.setattr(posters, "_FETCH_TIMEOUT", 0.2)

    async def answer(_request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    hosting = Hosting(answer)
    started = posters.time.monotonic()
    assert hosting.fetch() is None
    assert posters.time.monotonic() - started < 2
    assert URL in posters._failed


def test_the_failed_list_is_bounded():
    hosting = Hosting(httpx.Response(404))
    for number in range(posters._FAILED_SIZE + 10):
        hosting.fetch(f"{URL}?{number}")
    assert len(posters._failed) == posters._FAILED_SIZE
    assert f"{URL}?0" not in posters._failed
