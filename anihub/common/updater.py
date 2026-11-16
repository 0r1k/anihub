"""Updates from GitHub releases of https://github.com/0r1k/anihub (anicli-api is checked on PyPI).

A release is a `vX.Y` tag with the built wheel and sdist attached (see .github/workflows/release.yml).
`anihub update` reinstalls the tool from the release with the package manager it was installed by.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Literal, TypedDict

import httpx

import anihub

GITHUB_REPO = "0r1k/anihub"
# a fork or a mirror can point the updater elsewhere
RELEASES_API = os.environ.get("ANIHUB_RELEASES_URL", f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
# the release workflow attaches the sdist under this stable name too: one install command for every version
INSTALL_URL = f"https://github.com/{GITHUB_REPO}/releases/latest/download/anihub.tar.gz"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"

InstallMethod = Literal["uv", "pipx", "pip", "source"]

# --- Constants & Types ---


class TPACKAGE(TypedDict):
    package_name: str
    current_version: str
    latest_version: str
    is_outdated: bool


class TVERSION(TypedDict):
    anihub: TPACKAGE
    anicli_api: TPACKAGE


class UpdateExceptionError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    url: str
    notes: str
    wheel_url: str | None = None
    sdist_url: str | None = None

    @property
    def source(self) -> str:
        """What to install: the wheel, the sdist or the tag source archive (none of them needs git)."""
        return self.wheel_url or self.sdist_url or f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{self.tag}.zip"

    @classmethod
    def from_json(cls, data: dict) -> Release:
        tag = data["tag_name"]
        assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
        wheel = next((url for name, url in assets.items() if name.endswith(".whl")), None)
        sdist = next(
            (url for name, url in assets.items() if name.endswith(".tar.gz") and name != "anihub.tar.gz"), None
        )
        return cls(
            version=tag.removeprefix("v"),
            tag=tag,
            url=data.get("html_url", RELEASES_PAGE),
            notes=(data.get("body") or "").strip(),
            wheel_url=wheel,
            sdist_url=sdist or assets.get("anihub.tar.gz"),
        )


# --- versions ------------------------------------------------------------------


def _version_key(version: str) -> tuple[int, ...]:
    """'7.2' -> (7, 2), 'v1.2.10' -> (1, 2, 10). Non-numeric suffixes (rc, dev) are ignored.

    Trailing zeros are dropped: a tag 'v8.1.0' is the same version as '8.1' (otherwise it is offered forever).
    """
    key = []
    for part in version.strip().removeprefix("v").split("."):
        # ASCII digits only: str.isdigit() is true for '²' too, and int() fails on it
        digits = "".join(itertools.takewhile(lambda c: c in string.digits, part))
        if not digits:
            break
        key.append(int(digits))
    while key and key[-1] == 0:
        key.pop()
    return tuple(key)


def _is_outdated(current: str, latest: str) -> bool:
    return bool(current) and _version_key(latest) > _version_key(current)


def get_api_version() -> str:
    return pkg_version("anicli-api")


def _release_from_response(resp: httpx.Response) -> Release | None:
    if resp.status_code == 404:  # noqa: PLR2004
        return None
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":  # noqa: PLR2004
        msg = "GitHub API rate limit exceeded, try again in an hour"
        raise UpdateExceptionError(msg)
    resp.raise_for_status()
    return Release.from_json(resp.json())


def fetch_latest_release(timeout: float = 15, proxy: str | None = None) -> Release | None:
    """The latest GitHub release, None if there are no releases yet. `proxy`: the one of the settings."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, proxy=proxy or None) as client:
            return _release_from_response(client.get(RELEASES_API, headers={"Accept": "application/vnd.github+json"}))
    except (httpx.HTTPError, KeyError, ValueError) as e:
        msg = f"failed to check {RELEASES_PAGE}: {e}"
        raise UpdateExceptionError(msg) from e


def _package(package_name: str, current: str, latest: str | None) -> TPACKAGE:
    """`latest` is None when the check failed."""
    return {
        "package_name": package_name,
        "current_version": current,
        # 'unknown' state instead of crashing the app
        "latest_version": latest or "unknown",
        "is_outdated": latest is not None and _is_outdated(current, latest),
    }


async def _github_package(client: httpx.AsyncClient) -> TPACKAGE:
    try:
        resp = await client.get(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
        release = _release_from_response(resp)
    except (httpx.HTTPError, KeyError, ValueError, UpdateExceptionError):
        release = None
    return _package("anihub", anihub.__version__, release.version if release else None)


async def get_package_version_from_pypi(client: httpx.AsyncClient, package_name: str, current_version: str) -> TPACKAGE:
    try:
        response = await client.get(f"https://pypi.org/pypi/{package_name}/json")
        response.raise_for_status()
        latest_version = response.json()["info"]["version"]
    except (httpx.HTTPError, KeyError, ValueError):
        latest_version = None
    return _package(package_name, current_version, latest_version)


async def check_for_updates(proxy: str | None = None) -> TVERSION:
    """anihub (GitHub releases) and anicli-api (PyPI) versions, through the proxy of the settings."""
    api_version = get_api_version()
    try:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True, proxy=proxy or None)
    except ValueError:
        # a proxy httpx cannot use: no check at all rather than a request around the proxy
        return {
            "anihub": _package("anihub", anihub.__version__, None),
            "anicli_api": _package("anicli-api", api_version, None),
        }
    async with client:
        own, api = await asyncio.gather(
            _github_package(client), get_package_version_from_pypi(client, "anicli-api", api_version)
        )
    return {"anihub": own, "anicli_api": api}


# --- installation --------------------------------------------------------------


def _is_editable() -> bool:
    try:
        direct_url = distribution("anihub").read_text("direct_url.json")
    except PackageNotFoundError:
        return True  # not installed at all: running from the source tree
    if not direct_url:
        return False
    return bool(json.loads(direct_url).get("dir_info", {}).get("editable"))


def install_method() -> InstallMethod:
    """How the running anihub was installed (from the location of its environment)."""
    if _is_editable():
        return "source"
    prefix = Path(sys.prefix)
    # files the tool managers put into the environments they create
    if (prefix / "uv-receipt.toml").is_file():
        return "uv"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    return "pip"


def update_command(method: InstallMethod, source: str) -> list[str]:
    if method == "uv":
        uv = shutil.which("uv")
        if not uv:
            msg = "uv is not found in PATH"
            raise UpdateExceptionError(msg)
        # --upgrade: refresh dependencies (anicli-api) too
        return [uv, "tool", "install", "--force", "--upgrade", source]
    if method == "pipx":
        pipx = shutil.which("pipx")
        if not pipx:
            msg = "pipx is not found in PATH"
            raise UpdateExceptionError(msg)
        return [pipx, "install", "--force", source]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", source]
    msg = "anihub runs from a source checkout: update it with `git pull`"
    raise UpdateExceptionError(msg)


# Run by Python and not by `cmd /c`: cmd.exe parses the command line by its own rules, and a space, & % ^ ( ) or "
# in a path (C:\Users\John Smith, C:\Program Files\Python312) or in the url break the command or run another one.
# Arguments: the pause in seconds, then the command.
_DETACHED_UPDATE = """\
import subprocess, sys, time
time.sleep(float(sys.argv[1]))
try:
    code = subprocess.call(sys.argv[2:])
except OSError as e:
    print(e)
    code = 1
print()
print('anihub is updated' if code == 0 else f'Update failed (exit code {code})')
try:
    input('Press Enter to close this window')
except EOFError:
    pass
"""
# seconds for anihub to exit after it starts the update
EXIT_PAUSE = 2


def detached_update_command(command: list[str], pause: float = EXIT_PAUSE) -> list[str]:
    """The update `command` started after a `pause`, its arguments go to it as they are.

    The script runs on the base Python of the environment: the update replaces the environment, and its
    python.exe would be locked while the script runs.
    """
    python = getattr(sys, "_base_executable", "") or sys.executable
    if not os.path.isfile(python):
        python = sys.executable
    # -I: nothing from the current directory or PYTHON* variables gets into the script
    return [python, "-I", "-c", _DETACHED_UPDATE, str(pause), *command]


def run_update(command: list[str]) -> int | None:
    """Run the update command with its output in the terminal. Returns the exit code.

    On Windows the running anihub.exe is locked: the command is started in a new console after a pause and
    anihub should exit right away (None is returned).
    """
    if os.name == "nt":
        subprocess.Popen(  # noqa: S603
            detached_update_command(command),
            creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
        )
        return None
    return subprocess.run(command, check=False).returncode  # noqa: S603
