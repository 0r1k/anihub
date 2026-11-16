"""Titles and links from the sites cannot add playlist entries or reach the player as files or options."""

from __future__ import annotations

import pytest
from anicli_api.player.base import Video

from anihub.common.m3u import UnsafeVideoUrlError, checked_url, generate_m3u_str_playlist, one_line


def video(url: str) -> Video:
    return Video(type="mp4", quality=720, url=url)


def test_line_breaks_in_titles_do_not_add_entries():
    playlist = generate_m3u_str_playlist(
        [video("https://a.example/1.mp4"), video("https://a.example/2.mp4")],
        ["One\n/etc/passwd", "Two\r\n#EXTINF:0,x\r\nhttp://evil.example/x"],
    )
    lines = [line for line in playlist.splitlines() if line]
    assert lines == [
        "#EXTM3U",
        "#EXTINF:0,One /etc/passwd",
        "https://a.example/1.mp4",
        "#EXTINF:0,Two #EXTINF:0,x http://evil.example/x",
        "https://a.example/2.mp4",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "/home/user/video.mkv",
        "--script=evil.lua",
        "javascript:alert(1)",
        "https://a.example/1.mp4\nfile:///etc/passwd",
        "https://a.example/1 2.mp4",
        "https://a.example/1\x00.mp4",
        # urlsplit() raises a plain ValueError for a broken host
        "http://[x/1.mp4",
        "https://a.example]/1.mp4",
    ],
)
def test_only_plain_http_links(url):
    with pytest.raises(UnsafeVideoUrlError):
        checked_url(url)
    with pytest.raises(UnsafeVideoUrlError):
        generate_m3u_str_playlist([video(url)], ["title"])


def test_http_links_pass():
    assert checked_url("http://127.0.0.1:8000/v/index.m3u8?a=1") == "http://127.0.0.1:8000/v/index.m3u8?a=1"
    assert checked_url("HTTPS://a.example/1.mp4") == "HTTPS://a.example/1.mp4"


def test_one_line():
    assert one_line("  a\tb\n\nc d  ") == "a b c d"


def test_length_mismatch():
    with pytest.raises(ValueError, match="mismatch"):
        generate_m3u_str_playlist([video("https://a.example/1.mp4")], [])
