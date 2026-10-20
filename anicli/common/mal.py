"""MyAnimeList API v2 client: OAuth2 (PKCE) login, user anime list management.

Docs: https://myanimelist.net/apiconfig/references/api/v2
The user registers an own API client (App Type "other") at https://myanimelist.net/apiconfig
with the redirect URL `REDIRECT_URI` and puts its Client ID into the settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from anicli.common.config import get_config_dir

logger = logging.getLogger(__name__)

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_URL = "https://api.myanimelist.net/v2"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
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
# refresh the access token a bit before it expires
_EXPIRY_MARGIN = 300


class MalError(Exception):
    pass


class MalAuthError(MalError):
    """Not logged in or the refresh token is revoked/expired: the user should log in again."""


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
    list_status: ListStatus | None = None

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
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            return cls(**data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            logger.warning("broken MAL token file %s", cls.path(), exc_info=True)
            return None

    def save(self) -> None:
        path = self.path()
        # the token gives write access to the user list: readable by the owner only
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=4)

    @classmethod
    def delete(cls) -> None:
        cls.path().unlink(missing_ok=True)

    @classmethod
    def from_response(cls, data: dict[str, Any], username: str = "") -> Token:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + int(data.get("expires_in", 0)),
            username=username,
        )


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
        """Extract the authorization code from the redirect url (or accept the bare code)."""
        value = value.strip()
        if "code=" not in value and "error=" not in value:
            if not value:
                msg = "empty authorization code"
                raise MalError(msg)
            return value
        query = parse_qs(urlsplit(value).query or value.split("?", 1)[-1])
        if error := query.get("error"):
            msg = f"authorization denied: {error[0]}"
            raise MalError(msg)
        if query.get("state", [self.state])[0] != self.state:
            msg = "state mismatch: the url is from another login attempt"
            raise MalError(msg)
        try:
            return query["code"][0]
        except (KeyError, IndexError):
            msg = "no authorization code in the url"
            raise MalError(msg) from None


_CALLBACK_PAGE = (
    "<!doctype html><meta charset=utf-8><title>anihub</title>"
    "<body style='font-family:sans-serif;text-align:center;margin-top:20vh'><h2>{}</h2>"
    "<p>You can close this tab and return to the terminal.</p>"
)


async def wait_for_redirect(auth: AuthRequest) -> str:
    """Run a one-shot http server on REDIRECT_URI and return the authorization code.

    Raises OSError if the port is busy (then the user can paste the redirect url manually).
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            # drain headers
            while await asyncio.wait_for(reader.readline(), 10) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split()
            target = parts[1] if len(parts) > 1 else ""
            if not target.startswith("/callback"):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            try:
                code = auth.parse_redirect(target)
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
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                await writer.drain()
                writer.close()

    server = await asyncio.start_server(handle, "localhost", REDIRECT_PORT)
    try:
        async with server:
            return await result
    finally:
        server.close()


# --- client ------------------------------------------------------------------


class MalClient:
    def __init__(self, client_id: str = "", *, proxy: str | None = None, timeout: float = 30) -> None:
        self.client_id = client_id
        self.token = Token.load()
        self.http = httpx.AsyncClient(proxy=proxy or None, timeout=timeout)
        self._refresh_lock = asyncio.Lock()

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
        """Exchange the authorization code for tokens, returns the user name."""
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
            self.token.username = me.get("name", "")
        finally:
            self.token.save()
        return self.token.username

    def logout(self) -> None:
        self.token = None
        Token.delete()

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self.http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as e:
            msg = f"MyAnimeList is unavailable: {e}"
            raise MalError(msg) from e
        if resp.status_code >= 400:
            try:
                detail = resp.json()
                detail = detail.get("hint") or detail.get("message") or detail.get("error") or resp.text
            except ValueError:
                detail = resp.text
            msg = f"MyAnimeList auth error {resp.status_code}: {detail}"
            raise MalAuthError(msg)
        return resp.json()

    async def _refresh(self, stale: Token) -> None:
        async with self._refresh_lock:
            # another request has already refreshed it
            if self.token is not stale:
                return
            try:
                data = await self._token_request(
                    {"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": stale.refresh_token}
                )
            except MalAuthError:
                self.logout()
                raise
            self.token = Token.from_response(data, stale.username)
            self.token.save()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.logged_in or self.token is None:
            msg = "not logged in to MyAnimeList"
            raise MalAuthError(msg)
        if self.token.expires_at - _EXPIRY_MARGIN < time.time():
            await self._refresh(self.token)
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
            if resp.status_code == 401 and attempt == 0:
                await self._refresh(token)
                continue
            if resp.status_code == 401:
                self.logout()
                msg = "MyAnimeList session expired, log in again"
                raise MalAuthError(msg)
            if resp.status_code >= 400:
                msg = f"MyAnimeList error {resp.status_code}: {resp.text[:200]}"
                raise MalError(msg)
            return resp.json() if resp.content else {}
        raise AssertionError  # pragma: no cover

    # --- anime ---------------------------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> list[MalAnime]:
        # MAL rejects queries shorter than 3 characters
        query = query.strip()[:64]
        if len(query) < 3:
            return []
        data = await self._request(
            "GET", "/anime", params={"q": query, "limit": limit, "fields": ANIME_FIELDS, "nsfw": "true"}
        )
        return [MalAnime.from_json(i["node"]) for i in data.get("data", [])]

    async def get_anime(self, anime_id: int) -> MalAnime:
        data = await self._request("GET", f"/anime/{anime_id}", params={"fields": ANIME_FIELDS})
        return MalAnime.from_json(data)

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
        assert result is not None
        return result

    async def delete_list_status(self, anime_id: int) -> None:
        try:
            await self._request("DELETE", f"/anime/{anime_id}/my_list_status")
        except MalError as e:
            # already not in the list
            if "error 404" not in str(e):
                raise

    async def user_list(self, status: str | None = None, page_size: int = 100) -> list[MalAnime]:
        """The user anime list (or its part with the given status), recently updated first."""
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
            data = await self._request("GET", "/users/@me/animelist", params=params)
            items = data.get("data", [])
            result.extend(MalAnime.from_json(i["node"], i.get("list_status")) for i in items)
            if not data.get("paging", {}).get("next") or not items:
                return result
            params["offset"] += page_size
