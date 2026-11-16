"""MyAnimeList API v2 client: OAuth2 (PKCE) login, user anime list management.

Docs: https://myanimelist.net/apiconfig/references/api/v2
The user registers an own API client (App Type "other") at https://myanimelist.net/apiconfig
with the redirect URL `REDIRECT_URI` and puts its Client ID into the settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from anihub.common import storage
from anihub.common.config import get_config_dir

logger = logging.getLogger(__name__)

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_URL = "https://api.myanimelist.net/v2"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
_CALLBACK_PATH = urlsplit(REDIRECT_URI).path
APICONFIG_URL = "https://myanimelist.net/apiconfig"

# user list statuses in the MAL site order
STATUSES = ("watching", "completed", "on_hold", "dropped", "plan_to_watch")
STATUS_LABELS = {
    "watching": "Watching",
    "completed": "Completed",
    "on_hold": "On-Hold",
    "dropped": "Dropped",
    "plan_to_watch": "Plan to Watch",
}
SCORE_LABELS = {
    10: "Masterpiece",
    9: "Great",
    8: "Very Good",
    7: "Good",
    6: "Fine",
    5: "Average",
    4: "Bad",
    3: "Very Bad",
    2: "Horrible",
    1: "Appalling",
}
ANIME_FIELDS = "alternative_titles,main_picture,num_episodes,media_type,start_season,status,my_list_status"
# public pages of the site: profiles and friends (the API has no other users but their lists)
SITE_URL = "https://myanimelist.net"
# MAL rejects search queries shorter than that
MIN_QUERY_LENGTH = 3
# refresh the access token a bit before it expires
_EXPIRY_MARGIN = 300
# OAuth errors (RFC 6749 5.2) of the token endpoint after which the token will never work again: log in anew.
# Anything else (429, 5xx, a proxy page, a timeout) is MAL or the network failing for a while.
_GRANT_REFUSED = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})
# league/oauth2-server 7.x answers a dead refresh token with 401 (or 400) invalid_request: refused too
_REFRESH_DEAD = _GRANT_REFUSED | {"invalid_request"}
# after a failed refresh the next try waits: MAL is struggling, or rate limiting
_REFRESH_RETRY_DELAY = 60
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404
_HTTP_SERVER_ERROR = 500
# MAL answers too frequent requests with 429, or with 307 to an HTML page
_RATE_LIMITED = (307, 429)


class MalError(Exception):
    pass


class MalAuthError(MalError):
    """Not logged in or the refresh token is revoked/expired: the user should log in again."""


class MalRateLimited(MalError):  # noqa: N818 (public name: the TUI and the tests catch it)
    """MAL refuses too frequent requests: 429, or 307 to an HTML page instead of the API answer."""


class _ForeignRedirectError(MalError):
    """The redirect is not of this login attempt (no or another `state`): a tab of an earlier attempt."""


@dataclass
class ListStatus:
    status: str | None = None
    score: int = 0
    num_episodes_watched: int = 0
    is_rewatching: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ListStatus | None:
        if not data:
            return None
        return cls(
            status=data.get("status"),
            score=int(data.get("score") or 0),
            num_episodes_watched=int(data.get("num_episodes_watched") or 0),
            is_rewatching=bool(data.get("is_rewatching")),
        )


@dataclass
class MalAnime:
    id: int
    title: str
    # english, japanese and synonyms
    alt_titles: list[str] = field(default_factory=list)
    # 0 if unknown (eg: ongoing without announced episodes count)
    num_episodes: int = 0
    media_type: str = ""
    year: int | None = None
    # finished_airing, currently_airing, not_yet_aired
    airing_status: str = ""
    # poster url
    picture: str = ""
    # the entry in the list that was asked for: own or another user's
    list_status: ListStatus | None = None
    # the entry in the own list, whatever list was asked for
    my_list_status: ListStatus | None = None
    # when the title was added to the MAL database (ISO 8601), only if asked for
    created_at: str = ""

    @property
    def url(self) -> str:
        return f"https://myanimelist.net/anime/{self.id}"

    @property
    def all_titles(self) -> list[str]:
        return [self.title, *self.alt_titles]

    @classmethod
    def from_json(cls, node: dict[str, Any], list_status: dict[str, Any] | None = None) -> MalAnime:
        alt = node.get("alternative_titles") or {}
        alt_titles = [t for t in (alt.get("en"), alt.get("ja"), *(alt.get("synonyms") or [])) if t]
        season = node.get("start_season") or {}
        picture = node.get("main_picture") or {}
        return cls(
            id=int(node["id"]),
            title=node.get("title", ""),
            alt_titles=alt_titles,
            num_episodes=int(node.get("num_episodes") or 0),
            media_type=node.get("media_type", ""),
            year=season.get("year"),
            airing_status=node.get("status", ""),
            picture=picture.get("large") or picture.get("medium") or "",
            list_status=ListStatus.from_json(list_status or node.get("my_list_status")),
            my_list_status=ListStatus.from_json(node.get("my_list_status")),
            created_at=node.get("created_at", ""),
        )


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    username: str = ""

    @staticmethod
    def path() -> Path:
        return get_config_dir() / "mal_token.json"

    @classmethod
    def load(cls) -> Token | None:
        """The saved token, None if there is none. A file that is not JSON is moved aside (see storage)."""
        data: dict[str, Any] = storage.read_json(cls.path(), {})
        if not data:
            return None
        refresh_token = data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            logger.warning("no refresh token in %s, log in to MyAnimeList again", cls.path())
            # not a login: kept aside and shown to the user like a broken file, the next login writes a new one
            storage.set_aside(cls.path())
            return None
        # the refresh token is the login: the rest, if broken, is got anew by a refresh at the first request
        access_token, expires_at, username = data.get("access_token"), data.get("expires_at"), data.get("username")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_at, (int, float)):
            access_token, expires_at = "", 0
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(expires_at),
            username=username if isinstance(username, str) else "",
        )

    def save(self) -> None:
        """Raises OSError: the token stays in memory, the next start asks to log in again."""
        # the token gives write access to the user list: readable by the owner only
        storage.write_json(self.path(), asdict(self), private=True)

    @classmethod
    def delete(cls) -> None:
        cls.path().unlink(missing_ok=True)

    @classmethod
    def from_response(cls, data: dict[str, Any], username: str = "", refresh_token: str = "") -> Token:
        """The token from the answer of the token endpoint. `refresh_token`: the current one, kept if MAL sends none."""
        access = data.get("access_token")
        refresh = data.get("refresh_token") or refresh_token
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            msg = "MyAnimeList answered without a token, try again later"
            raise MalError(msg)
        try:
            expires_in = float(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        return cls(access_token=access, refresh_token=refresh, expires_at=time.time() + expires_in, username=username)


# --- OAuth -------------------------------------------------------------------


@dataclass
class AuthRequest:
    """One login attempt. MAL supports only the `plain` PKCE method: challenge == verifier."""

    client_id: str
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(96)[:128])
    state: str = field(default_factory=lambda: secrets.token_urlsafe(16))

    @property
    def url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "state": self.state,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": self.verifier,
            "code_challenge_method": "plain",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def parse_redirect(self, value: str) -> str:
        """The authorization code from the pasted address of the redirect page, or the pasted code itself."""
        value = value.strip()
        if not value:
            msg = "empty authorization code"
            raise MalError(msg)
        # a code is letters and digits: anything like an address must be the redirect of this attempt
        if not set(value) & set("/?&=:"):
            return value
        query = urlsplit(value).query if "?" in value else value
        if not parse_qs(query).keys() & {"code", "error"}:
            msg = "no authorization code in the address"
            raise MalError(msg)
        return self.parse_callback(query)

    def parse_callback(self, query: str, *, accept_stateless_error: bool = False) -> str:
        """The authorization code from the query of the redirect. Strict: only the redirect of this attempt.

        `accept_stateless_error`: an `error` without any `state` is reported as the failure of this attempt (MAL's
        denial redirect may carry no state; it only reports a failure, so it is safe). A different state stays foreign.

        Raises MalError: `_ForeignRedirectError` without the `state` of this attempt.
        """
        params = parse_qs(query)
        if accept_stateless_error and "state" not in params and (error := params.get("error")):
            msg = f"authorization denied: {error[0]}"
            raise MalError(msg)
        if params.get("state") != [self.state]:
            msg = "state mismatch: the address is from another login attempt"
            raise _ForeignRedirectError(msg)
        if error := params.get("error"):
            msg = f"authorization denied: {error[0]}"
            raise MalError(msg)
        code = params.get("code", [])
        if len(code) != 1:
            msg = "no authorization code in the address"
            raise MalError(msg)
        return code[0]


_CALLBACK_PAGE = (
    "<!doctype html><meta charset=utf-8><title>anihub</title>"
    "<body style='font-family:sans-serif;text-align:center;margin-top:20vh'><h2>{}</h2>"
    "<p>You can close this tab and return to the terminal.</p>"
)
_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def wait_for_redirect(auth: AuthRequest) -> str:
    """Run a one-shot http server on REDIRECT_URI and return the authorization code.

    Only the redirect of this login attempt ends the wait: other requests (the favicon, a tab of an earlier
    attempt) are answered and ignored. Raises OSError if the port is busy (then the user can paste the redirect
    url manually), MalError if the user denied access.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()
    connections: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.add(writer)
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            # drain headers
            while await asyncio.wait_for(reader.readline(), 10) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split()
            url = urlsplit(parts[1]) if len(parts) > 1 else None
            if url is None or url.path != _CALLBACK_PATH:
                writer.write(_NOT_FOUND)
                return
            try:
                code = auth.parse_callback(url.query, accept_stateless_error=True)
            except _ForeignRedirectError as e:
                message, ok = f"Login failed: {e}", False
            except MalError as e:
                message, ok = f"Login failed: {e}", False
                if not result.done():
                    result.set_exception(e)
            else:
                message, ok = "Logged in to MyAnimeList", True
                if not result.done():
                    result.set_result(code)
            body = _CALLBACK_PAGE.format(html.escape(message)).encode()
            status = b"200 OK" if ok else b"400 Bad Request"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
        # ValueError: a line over the reader limit, an invalid address
        except (asyncio.TimeoutError, OSError, ValueError):
            pass
        finally:
            connections.discard(writer)
            with contextlib.suppress(OSError):
                await writer.drain()
            writer.close()

    server = await asyncio.start_server(handle, "localhost", REDIRECT_PORT)
    try:
        return await result
    finally:
        # not `async with server`: it waits for every connection, and browsers keep spare ones open,
        # so the login would hang until they time out
        server.close()
        for writer in list(connections):
            writer.close()


# --- client ------------------------------------------------------------------


class MalClient:
    def __init__(self, client_id: str = "", *, proxy: str | None = None, timeout: float = 30) -> None:
        self.client_id = client_id
        self.token = Token.load()
        self.http = httpx.AsyncClient(proxy=proxy or None, timeout=timeout)
        self._refresh_lock = asyncio.Lock()
        # time.time() of the last failed refresh (MAL failing, not a refusal)
        self._refresh_failed_at = 0.0

    @property
    def logged_in(self) -> bool:
        return bool(self.client_id and self.token)

    @property
    def username(self) -> str:
        return self.token.username if self.token else ""

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- auth --------------------------------------------------------------------

    def new_auth_request(self) -> AuthRequest:
        if not self.client_id:
            msg = "MyAnimeList Client ID is not set"
            raise MalAuthError(msg)
        return AuthRequest(self.client_id)

    async def login(self, auth: AuthRequest, code: str) -> str:
        """Exchange the authorization code for tokens, returns the user name.

        Raises OSError if the token could not be saved: logged in until anihub exits.
        """
        data = await self._token_request(
            {
                "client_id": auth.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": auth.verifier,
            }
        )
        self.token = Token.from_response(data)
        try:
            me = await self._request("GET", "/users/@me")
            self.token.username = me.get("name", "") if isinstance(me, dict) else ""
        finally:
            self.token.save()
        return self.token.username

    def logout(self) -> None:
        self.token = None
        try:
            Token.delete()
        except OSError:
            # logged out until anihub exits: the next start reads the token again
            logger.warning("cannot delete %s", Token.path(), exc_info=True)

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint. MalAuthError only if MAL refused the grant: logging in again is the way."""
        try:
            resp = await self.http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as e:
            msg = f"MyAnimeList is unavailable: {e}"
            raise MalError(msg) from e
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.is_success and isinstance(data, dict):
            return data
        # the form is not logged: it has the refresh token or the authorization code
        logger.warning("MyAnimeList token request failed: %s %s", resp.status_code, resp.text[:500])
        error = data.get("error") if isinstance(data, dict) else None
        detail = (data.get("hint") or data.get("message") or error) if isinstance(data, dict) else None
        if not isinstance(error, str):
            error = None
        if resp.status_code in _RATE_LIMITED:
            msg = f"MyAnimeList limits requests ({resp.status_code}), try again in a few minutes"
            raise MalRateLimited(msg)
        # 401 at the token endpoint is always a refusal; 400 only with an OAuth error saying so
        if resp.status_code == _HTTP_UNAUTHORIZED or (resp.status_code == _HTTP_BAD_REQUEST and error in _REFRESH_DEAD):
            msg = f"MyAnimeList auth error {resp.status_code}: {detail}"
            raise MalAuthError(msg)
        if resp.status_code >= _HTTP_SERVER_ERROR:
            msg = f"MyAnimeList is unavailable ({resp.status_code}), try again later"
        elif resp.is_success:
            msg = "MyAnimeList answered not JSON, try again later"
        else:
            msg = f"MyAnimeList auth error {resp.status_code}" + (f": {detail}" if detail else "")
        raise MalError(msg)

    async def _refresh(self, stale: Token) -> None:
        """A new access token. Only a refused refresh token logs out: MAL or the network failing keep the login."""
        async with self._refresh_lock:
            # another request has already refreshed it
            if self.token is not stale:
                return
            # another anihub instance has refreshed it: MAL revoked the refresh token of `stale` then
            if self._take_saved(stale):
                return
            try:
                data = await self._token_request(
                    {"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": stale.refresh_token}
                )
            except MalAuthError:
                # refused because another instance has refreshed it meanwhile
                if self._take_saved(stale):
                    return
                self.token = None
                # the file may hold a token another instance has saved just now: not ours to delete
                saved = Token.load()
                if saved is None or saved.refresh_token == stale.refresh_token:
                    self.logout()
                raise
            self.token = Token.from_response(data, stale.username, stale.refresh_token)
            try:
                self.token.save()
            except OSError:
                # the new token works until anihub exits, the saved one is revoked: the next start asks to log in
                logger.warning("cannot save %s", Token.path(), exc_info=True)

    def _take_saved(self, stale: Token) -> bool:
        """Use the saved token if it is newer than `stale`: refreshed or logged in anew by another instance."""
        saved = Token.load()
        # not newer: the saved one is older if saving `stale` has failed
        if saved is None or saved.refresh_token == stale.refresh_token or saved.expires_at <= stale.expires_at:
            return False
        self.token = saved
        return True

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.logged_in or self.token is None:
            msg = "not logged in to MyAnimeList"
            raise MalAuthError(msg)
        now = time.time()
        # no new attempt right after a failed one while the access token still works
        backing_off = self.token.expires_at > now and now - self._refresh_failed_at < _REFRESH_RETRY_DELAY
        if self.token.expires_at - _EXPIRY_MARGIN < now and not backing_off:
            try:
                await self._refresh(self.token)
            except MalAuthError:
                raise
            except MalError:
                self._refresh_failed_at = time.time()
                # MAL or the network fails for a while: the token still works until it expires
                if self.token is None or self.token.expires_at <= time.time():
                    raise
                logger.warning("MyAnimeList token refresh failed, the current one works for now", exc_info=True)
        for attempt in range(2):
            token = self.token
            if token is None:
                msg = "not logged in to MyAnimeList"
                raise MalAuthError(msg)
            try:
                resp = await self.http.request(
                    method, API_URL + path, headers={"Authorization": f"Bearer {token.access_token}"}, **kwargs
                )
            except httpx.HTTPError as e:
                msg = f"MyAnimeList is unavailable: {e}"
                raise MalError(msg) from e
            if resp.status_code == _HTTP_UNAUTHORIZED and attempt == 0:
                await self._refresh(token)
                continue
            if resp.status_code == _HTTP_UNAUTHORIZED:
                # MAL has just refreshed the token and still refuses it: its failure, the login is kept
                msg = "MyAnimeList refused the request (401), try again later"
                raise MalError(msg)
            if resp.status_code in _RATE_LIMITED:
                msg = f"MyAnimeList limits requests ({resp.status_code}), try again in a few minutes"
                raise MalRateLimited(msg)
            if resp.status_code >= _HTTP_BAD_REQUEST:
                msg = f"MyAnimeList error {resp.status_code}: {resp.text[:200]}"
                raise MalError(msg)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as e:
                msg = f"MyAnimeList answered not JSON ({resp.status_code})"
                raise MalError(msg) from e
        raise AssertionError  # pragma: no cover

    # --- anime ---------------------------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> list[MalAnime]:
        query = query.strip()[:64]
        if len(query) < MIN_QUERY_LENGTH:
            return []
        data = await self._request(
            "GET", "/anime", params={"q": query, "limit": limit, "fields": ANIME_FIELDS, "nsfw": "true"}
        )
        return [MalAnime.from_json(i["node"]) for i in data.get("data", [])]

    async def get_anime(self, anime_id: int, fields: str = ANIME_FIELDS) -> MalAnime:
        data = await self._request("GET", f"/anime/{anime_id}", params={"fields": fields})
        return MalAnime.from_json(data)

    async def get_anime_json(self, anime_id: int, fields: str) -> dict[str, Any]:
        return await self._request("GET", f"/anime/{anime_id}", params={"fields": fields})

    async def related(self, anime_id: int) -> list[tuple[int, str]]:
        """Related titles (sequels, prequels, side stories…): (MAL id, relation type)."""
        data = await self._request("GET", f"/anime/{anime_id}", params={"fields": "related_anime"})
        return [(int(r["node"]["id"]), r.get("relation_type", "")) for r in data.get("related_anime", [])]

    async def page(self, path: str) -> str:
        """A public page of the site (profiles, friends): the API has nothing about other users."""
        try:
            resp = await self.http.get(SITE_URL + path, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        except httpx.HTTPError as e:
            msg = f"MyAnimeList is unavailable: {e}"
            raise MalError(msg) from e
        if resp.status_code == _HTTP_NOT_FOUND:
            msg = "MyAnimeList: no such user"
            raise MalError(msg)
        if resp.status_code >= _HTTP_BAD_REQUEST:
            msg = f"MyAnimeList error {resp.status_code}"
            raise MalError(msg)
        return resp.text

    async def update_list_status(
        self,
        anime_id: int,
        *,
        status: str | None = None,
        score: int | None = None,
        num_watched_episodes: int | None = None,
        is_rewatching: bool | None = None,
    ) -> ListStatus:
        """Add the anime to the list or update its entry. Only passed fields are changed."""
        form: dict[str, str] = {}
        if status is not None:
            if status not in STATUSES:
                msg = f"unknown status {status!r}"
                raise ValueError(msg)
            form["status"] = status
        if score is not None:
            form["score"] = str(max(0, min(10, score)))
        if num_watched_episodes is not None:
            form["num_watched_episodes"] = str(max(0, num_watched_episodes))
        if is_rewatching is not None:
            form["is_rewatching"] = "true" if is_rewatching else "false"
        data = await self._request("PATCH", f"/anime/{anime_id}/my_list_status", data=form)
        result = ListStatus.from_json(data)
        if result is None:
            msg = "MyAnimeList answered without the list status, try again later"
            raise MalError(msg)
        return result

    async def delete_list_status(self, anime_id: int) -> None:
        try:
            await self._request("DELETE", f"/anime/{anime_id}/my_list_status")
        except MalError as e:
            # already not in the list
            if "error 404" not in str(e):
                raise

    async def user_list(self, status: str | None = None, page_size: int = 1000, user: str = "@me") -> list[MalAnime]:
        """The anime list of the user (own by default) or its part with the given status, recently updated first.

        Another user's entries are in `list_status`, own ones in `my_list_status`.
        """
        params: dict[str, Any] = {
            "fields": f"list_status,{ANIME_FIELDS}",
            "sort": "list_updated_at",
            "limit": page_size,
            "offset": 0,
            "nsfw": "true",
        }
        if status:
            params["status"] = status
        result: list[MalAnime] = []
        while True:
            # "@me" must stay as is: MAL redirects "%40me" to an HTML page
            data = await self._request("GET", f"/users/{quote(user, safe='@')}/animelist", params=params)
            items = data.get("data", [])
            result.extend(MalAnime.from_json(i["node"], i.get("list_status")) for i in items)
            if not data.get("paging", {}).get("next") or not items:
                return result
            params["offset"] += page_size
