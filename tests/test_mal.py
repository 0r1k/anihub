"""MyAnimeList: token refresh errors, the OAuth redirect, matching titles to MAL entries. No network."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import time
from types import SimpleNamespace
from typing import Any

import attr
import httpx
import pytest

from anihub.common import mal as mal_api
from anihub.common import mal_links, storage
from anihub.common.mal import AuthRequest, MalAnime, MalAuthError, MalClient, MalError, MalRateLimited, Token
from anihub.tui import mal


def run(coro, timeout: float = 30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


ANIME = {"id": 1, "title": "Frieren", "num_episodes": 28}


def _client(handler, *, expires_in: float = -1) -> MalClient:
    """A logged in client whose access token expires in `expires_in` seconds (expired by default)."""
    Token("old-access", "old-refresh", time.time() + expires_in, "me").save()
    client = MalClient("client-id")
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _saved() -> dict[str, Any]:
    return json.loads(Token.path().read_text(encoding="utf-8"))


def _token_endpoint(response) -> Any:
    """MAL answering `response` (a Response or an exception to raise) at the token endpoint, the anime otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == mal_api.TOKEN_URL:
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json=ANIME)

    return handler


# --- token refresh -----------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(401, json={"error": "invalid_grant"}),
        # league/oauth2-server 7.x: an expired or revoked refresh token
        httpx.Response(401, json={"error": "invalid_request", "hint": "Token has expired"}),
        httpx.Response(400, json={"error": "invalid_request", "hint": "Token has been revoked"}),
        httpx.Response(401, text="Unauthorized"),
    ],
)
def test_refused_refresh_token_logs_out(response):
    client = _client(_token_endpoint(response))
    with pytest.raises(MalAuthError):
        run(client.get_anime(1))
    assert not client.logged_in
    assert not Token.path().exists()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, text="Too Many Requests"), MalRateLimited),
        (httpx.Response(503, text="<html>maintenance</html>"), MalError),
        (httpx.Response(500, json={"error": "server_error"}), MalError),
        (httpx.Response(200, text="<html>not json</html>"), MalError),
        (httpx.ConnectError("no route"), MalError),
        (httpx.ReadTimeout("timed out"), MalError),
    ],
)
def test_failed_refresh_keeps_the_login(response, error):
    """MAL or the network failing for a while: the request fails, the user stays logged in to retry later."""
    client = _client(_token_endpoint(response))
    before = _saved()
    with pytest.raises(error) as raised:
        run(client.get_anime(1))
    assert not isinstance(raised.value, MalAuthError)
    assert client.logged_in and client.username == "me"
    assert _saved() == before


def test_failed_refresh_uses_the_token_still_valid():
    """Refreshed a bit before the expiry: if MAL fails then, the current access token still works."""
    client = _client(_token_endpoint(httpx.Response(503)), expires_in=60)
    assert run(client.get_anime(1)).title == "Frieren"
    assert client.token is not None and client.token.access_token == "old-access"


def test_failed_refresh_is_not_repeated_every_request():
    """MAL failing: the token still works, so the next requests do not post to the token endpoint again."""
    posts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == mal_api.TOKEN_URL:
            posts.append(1)
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, json=ANIME)

    client = _client(handler, expires_in=60)

    async def go() -> None:
        for _ in range(3):
            await client.get_anime(1)

    run(go())
    assert len(posts) == 1
    # after the delay it tries again
    client._refresh_failed_at -= mal_api._REFRESH_RETRY_DELAY + 1
    run(client.get_anime(1))
    assert len(posts) == 2


def test_refused_refresh_keeps_the_token_another_instance_saved():
    """Refused, and the file holds a different refresh token that is not newer: it is not ours to delete."""
    client = _client(_token_endpoint(httpx.Response(400, json={"error": "invalid_grant"})))
    # another instance logged in anew, but its token expires earlier than the stale one (so it is not "taken")
    Token("other-access", "other-refresh", time.time() - 100, "me").save()
    with pytest.raises(MalAuthError):
        run(client.get_anime(1))
    assert not client.logged_in
    assert _saved()["refresh_token"] == "other-refresh"


@pytest.mark.parametrize("answer", [httpx.Response(200, json={}), httpx.Response(204)])
def test_update_list_status_without_a_body_is_a_mal_error(answer):
    client = _client(lambda request: answer, expires_in=3600)
    with pytest.raises(MalError):
        run(client.update_list_status(1, status="watching"))


def test_refresh_saves_the_new_token_privately():
    answer = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    client = _client(_token_endpoint(httpx.Response(200, json=answer)))
    assert run(client.get_anime(1)).id == 1
    saved = _saved()
    assert saved["access_token"] == "new-access" and saved["refresh_token"] == "new-refresh"
    assert saved["username"] == "me"
    if os.name == "posix":
        assert stat.S_IMODE(Token.path().stat().st_mode) == 0o600


def test_refresh_takes_the_token_of_another_instance():
    """Another anihub refreshed meanwhile: MAL refuses the old refresh token, the saved new one is used."""
    client = _client(_token_endpoint(httpx.Response(400, json={"error": "invalid_grant"})))
    Token("other-access", "other-refresh", time.time() + 3600, "me").save()
    assert run(client.get_anime(1)).id == 1
    assert client.logged_in and client.token is not None and client.token.access_token == "other-access"


def test_broken_token_file_is_set_aside(monkeypatch):
    """Not silently: the file is kept next to it and reported (the TUI tells the user)."""
    monkeypatch.setattr(storage, "broken_files", [])
    Token.path().parent.mkdir(parents=True, exist_ok=True)
    Token.path().write_text('{"access_token": "trunc', encoding="utf-8")
    assert Token.load() is None
    assert [path for path, _ in storage.broken_files] == [Token.path()]
    assert storage.broken_files[0][1].exists()


def test_token_file_without_refresh_token_is_set_aside(monkeypatch):
    """JSON, but not a login: reported like a broken file instead of a silent logout."""
    monkeypatch.setattr(storage, "broken_files", [])
    Token.path().parent.mkdir(parents=True, exist_ok=True)
    Token.path().write_text('{"access_token": "a", "expires_at": 1}', encoding="utf-8")
    assert Token.load() is None
    assert [path for path, _ in storage.broken_files] == [Token.path()]
    assert not Token.path().exists()


def test_token_without_access_token_refreshes():
    """The refresh token is the login: the rest of a damaged file is got anew."""
    Token.path().parent.mkdir(parents=True, exist_ok=True)
    Token.path().write_text(json.dumps({"refresh_token": "r", "expires_at": "soon"}), encoding="utf-8")
    token = Token.load()
    assert token is not None and token.refresh_token == "r" and token.expires_at == 0


# --- OAuth redirect ----------------------------------------------------------


def test_callback_needs_the_state_of_this_attempt():
    auth = AuthRequest("client-id")
    assert auth.parse_callback(f"code=abc&state={auth.state}") == "abc"
    for query in ("code=abc", "code=abc&state=other", "", "error=access_denied"):
        with pytest.raises(MalError):
            auth.parse_callback(query)
    with pytest.raises(MalError, match="denied"):
        auth.parse_callback(f"error=access_denied&state={auth.state}")
    with pytest.raises(MalError, match="no authorization code"):
        auth.parse_callback(f"state={auth.state}")
    # the server path: a denial without any state ends the wait, a different state is still foreign
    with pytest.raises(MalError, match="denied"):
        auth.parse_callback("error=access_denied", accept_stateless_error=True)
    with pytest.raises(MalError, match="state mismatch"):
        auth.parse_callback("error=access_denied&state=other", accept_stateless_error=True)
    with pytest.raises(MalError, match="state mismatch"):
        auth.parse_callback("code=abc", accept_stateless_error=True)


def test_pasted_redirect():
    auth = AuthRequest("client-id")
    assert auth.parse_redirect("  abc123  ") == "abc123"
    assert auth.parse_redirect(f"http://localhost:8765/callback?code=abc&state={auth.state}") == "abc"
    assert auth.parse_redirect(f"code=abc&state={auth.state}") == "abc"
    for value in ("", "/callback", "http://localhost:8765/callback", "http://localhost:8765/callback?code=abc"):
        with pytest.raises(MalError):
            auth.parse_redirect(value)


def _free_port() -> int:
    """A port free on both IPv4 and IPv6: the server binds every address of "localhost"."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not socket.has_ipv6:
            return port
        try:
            with socket.socket(socket.AF_INET6) as s6:
                s6.bind(("::1", port))
        except OSError as e:
            # no IPv6 at all is fine, a busy port is not
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                continue
        return port
    msg = "no free port"
    raise RuntimeError(msg)


def test_redirect_server_waits_for_this_attempt(monkeypatch):
    """Other requests (the favicon, a bare /callback, a tab of an earlier attempt) do not end the login."""
    port = _free_port()
    monkeypatch.setattr(mal_api, "REDIRECT_PORT", port)
    auth = AuthRequest("client-id")

    async def check() -> None:
        waiting = asyncio.ensure_future(mal_api.wait_for_redirect(auth))
        base = f"http://localhost:{port}"
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(50):
                try:
                    await http.get(f"{base}/probe")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            assert (await http.get(f"{base}/favicon.ico")).status_code == 404
            assert (await http.get(f"{base}/callbackx?code=a&state={auth.state}")).status_code == 404
            assert (await http.get(f"{base}/callback")).status_code == 400
            assert (await http.get(f"{base}/callback?code=old&state=earlier")).status_code == 400
            assert (await http.get(f"{base}/callback?code=old")).status_code == 400
            assert (await http.get(f"{base}/callback?error=access_denied&state=earlier")).status_code == 400
            assert not waiting.done()
            assert (await http.get(f"{base}/callback?code=right&state={auth.state}")).status_code == 200
        assert await waiting == "right"

    run(check())


def test_redirect_server_reports_denial_without_state(monkeypatch):
    port = _free_port()
    monkeypatch.setattr(mal_api, "REDIRECT_PORT", port)
    auth = AuthRequest("client-id")

    async def check() -> None:
        waiting = asyncio.ensure_future(mal_api.wait_for_redirect(auth))
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(50):
                try:
                    await http.get(f"http://localhost:{port}/callback?error=access_denied")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        with pytest.raises(MalError, match="denied"):
            await asyncio.wait_for(waiting, 5)

    run(check())


def test_redirect_server_reports_denied_access(monkeypatch):
    port = _free_port()
    monkeypatch.setattr(mal_api, "REDIRECT_PORT", port)
    auth = AuthRequest("client-id")

    async def check() -> None:
        waiting = asyncio.ensure_future(mal_api.wait_for_redirect(auth))
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(50):
                try:
                    await http.get(f"http://localhost:{port}/callback?error=access_denied&state={auth.state}")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        with pytest.raises(MalError, match="denied"):
            await waiting

    run(check())


# --- matching titles ---------------------------------------------------------


@attr.s(auto_attribs=True)
class Item:
    title: str
    url: str
    thumbnail: str = ""
    myanimelist_id: int | None = None


class FakeMal:
    """MalClient answering from a dict of entries, recording the requests."""

    def __init__(self, *entries: MalAnime) -> None:
        self.entries = {a.id: a for a in entries}
        self.requested: list[Any] = []

    async def get_anime(self, anime_id: int) -> MalAnime:
        self.requested.append(anime_id)
        return self.entries[anime_id]

    async def search(self, query: str) -> list[MalAnime]:
        self.requested.append(query)
        return list(self.entries.values())


def _anime(anime_id: int, title: str) -> MalAnime:
    return MalAnime.from_json({"id": anime_id, "title": title})


def _resolve(fake: FakeMal, item: Item, hint: MalAnime | None = None):
    session: Any = SimpleNamespace(mal=fake)
    return run(mal.resolve(session, item, "site", hint=hint))


def test_resolve_prefers_the_saved_link():
    fake = FakeMal(_anime(1, "Frieren"), _anime(2, "Other"))
    item = Item("Frieren", "https://site/frieren")
    mal_links.save(item, "site", _anime(2, "Other"))
    found, _ = _resolve(fake, item, hint=_anime(1, "Frieren"))
    assert found is not None and found.id == 2


def test_resolve_by_hint_saves_the_link():
    fake = FakeMal(_anime(1, "Frieren"))
    item = Item("Sousou no Frieren", "https://site/frieren")
    found, _ = _resolve(fake, item, hint=_anime(1, "Frieren"))
    assert found is not None and found.id == 1
    link = mal_links.get(item, "site")
    assert link is not None and link["mal_id"] == 1


def test_resolve_skips_a_broken_link():
    """mal_links.json edited by hand: a link without a valid id is matched anew."""
    fake = FakeMal(_anime(1, "Frieren"))
    item = Item("Frieren", "https://site/frieren")
    path = mal_links._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    link = {**mal_links.serialize_item(item, "site"), "mal_id": "one"}
    path.write_text(json.dumps([link]), encoding="utf-8")
    found, _ = _resolve(fake, item, hint=_anime(1, "Frieren"))
    assert found is not None and found.id == 1


def test_resolve_by_the_site_mal_id():
    fake = FakeMal(_anime(5, "Bocchi"))
    found, _ = _resolve(fake, Item("Bocchi", "https://site/bocchi", myanimelist_id=5))
    assert found is not None and found.id == 5 and fake.requested == [5]


def test_resolve_by_exact_title():
    fake = FakeMal(_anime(7, "Dandadan"), _anime(8, "Dandadan 2nd Season"))
    found, titles = _resolve(fake, Item("Dandadan", "https://site/dandadan"))
    assert found is not None and found.id == 7 and "Dandadan" in titles
    assert mal_links.get(Item("Dandadan", "https://site/dandadan"), "site") is not None


def test_resolve_finds_nothing():
    fake = FakeMal(_anime(9, "Something else"))
    found, titles = _resolve(fake, Item("Unknown title", "https://site/unknown"))
    assert found is None and titles == ["Unknown title"]


@pytest.mark.parametrize(
    ("value", "number"),
    [(1, 1), (12, 12), ("12", 12), (" 3 ", 3), (3.0, 3), ("12.5", None), (12.5, None), ("OVA", None),
     ("²", None), ("", None), (0, None), (-1, None), (True, None), (None, None)],
)  # fmt: skip
def test_episode_number(value, number):
    assert mal.episode_number(value) == number
