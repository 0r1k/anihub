from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import httpx
import pytest
from typer.testing import CliRunner

from anihub.common import updater
from anihub.common.config import get_config_dir
from anihub.main import app

PROXY = "socks5://127.0.0.1:1080"


@pytest.mark.parametrize(
    ("current", "latest", "outdated"),
    [
        ("8.1", "8.1.0", False),
        ("8.1.0", "8.1", False),
        ("8.0", "8", False),
        ("8.1", "8.2", True),
        ("8.2", "8.10", True),
        ("8.10", "8.9", False),
        ("8.1", "v8.1.1", True),
        ("8.2", "unknown", False),
        ("8.2", "8.²", False),
        ("0.0.0+dev", "8.2", True),
    ],
)
def test_is_outdated(current, latest, outdated):
    assert updater._is_outdated(current, latest) is outdated


def test_release_tag_with_zero_patch_is_not_newer():
    release = updater.Release.from_json({"tag_name": "v8.2.0", "assets": []})
    assert release.version == "8.2.0"
    assert not updater._is_outdated("8.2", release.version)


# --- proxy -------------------------------------------------------------------


def _github_and_pypi(request: httpx.Request) -> httpx.Response:
    if request.url.host == "pypi.org":
        return httpx.Response(200, json={"info": {"version": "0.99"}})
    return httpx.Response(200, json={"tag_name": "v99.0", "html_url": updater.RELEASES_PAGE, "assets": []})


@pytest.fixture
def clients(monkeypatch):
    """The http clients of the updater answer offline; the arguments they are created with are kept."""
    created: list[dict] = []

    def offline(real):
        def create(**kwargs):
            created.append(dict(kwargs))
            kwargs.pop("proxy", None)  # the mock transport stands for the proxy
            return real(transport=httpx.MockTransport(_github_and_pypi), **kwargs)

        return create

    monkeypatch.setattr(httpx, "AsyncClient", offline(httpx.AsyncClient))
    monkeypatch.setattr(httpx, "Client", offline(httpx.Client))
    return created


def test_check_for_updates_through_proxy(clients):
    result = asyncio.run(updater.check_for_updates(proxy=PROXY))

    # one client for GitHub and PyPI
    assert [c["proxy"] for c in clients] == [PROXY]
    assert result["anihub"]["latest_version"] == "99.0"
    assert result["anihub"]["is_outdated"]
    assert result["anicli_api"]["latest_version"] == "0.99"


def test_check_for_updates_without_proxy(clients):
    asyncio.run(updater.check_for_updates(proxy=""))
    assert [c["proxy"] for c in clients] == [None]


def test_check_for_updates_with_broken_proxy():
    # httpx refuses the scheme before any request: nothing goes around the proxy
    result = asyncio.run(updater.check_for_updates(proxy="foo://127.0.0.1:1"))
    assert result["anihub"]["latest_version"] == "unknown"
    assert result["anicli_api"]["latest_version"] == "unknown"
    assert not result["anihub"]["is_outdated"]


def test_fetch_latest_release_through_proxy(clients):
    release = updater.fetch_latest_release(proxy=PROXY)
    assert release is not None
    assert release.version == "99.0"
    assert [c["proxy"] for c in clients] == [PROXY]


def test_fetch_latest_release_with_broken_proxy():
    with pytest.raises(updater.UpdateExceptionError):
        updater.fetch_latest_release(proxy="foo://127.0.0.1:1")


@pytest.fixture
def fetched(monkeypatch):
    """Proxies `anihub update` checks the release with: there is no release, nothing is installed."""
    proxies: list[str | None] = []

    def fetch_latest_release(proxy: str | None = None, **_: object) -> None:
        proxies.append(proxy)

    monkeypatch.setattr(updater, "fetch_latest_release", fetch_latest_release)
    return proxies


def _save_proxy(proxy: str) -> None:
    (get_config_dir() / "settings.json").write_text(json.dumps({"proxy": proxy}), encoding="utf-8")


def test_update_command_uses_saved_proxy(fetched):
    _save_proxy(PROXY)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert fetched == [PROXY]


@pytest.mark.parametrize(("option", "proxy"), [("http://127.0.0.1:8080", "http://127.0.0.1:8080"), ("", None)])
def test_update_command_proxy_option_wins(fetched, option, proxy):
    _save_proxy(PROXY)
    result = CliRunner().invoke(app, ["update", "--proxy", option])
    assert result.exit_code == 0, result.output
    assert fetched == [proxy]


def test_update_command_rejects_bad_proxy(fetched):
    result = CliRunner().invoke(app, ["update", "--proxy", "foo://127.0.0.1:1"])
    assert result.exit_code == 2
    assert fetched == []


def test_update_command_never_goes_around_a_broken_saved_proxy(fetched):
    _save_proxy("foo://127.0.0.1:1")
    result = CliRunner().invoke(app, ["update"])
    assert "foo://127.0.0.1:1" not in fetched
    if result.exit_code:
        assert "proxy" in result.output


def test_check_updates_command_uses_saved_proxy(monkeypatch):
    proxies = []

    async def check_for_updates(proxy: str | None = None) -> dict:
        proxies.append(proxy)
        return {}

    monkeypatch.setattr(updater, "check_for_updates", check_for_updates)
    _save_proxy(PROXY)
    result = CliRunner().invoke(app, ["check-updates"])
    assert result.exit_code == 0, result.output
    assert proxies == [PROXY]


# --- Windows: the update in a new console --------------------------------------


def _detached(args: list[str], code: int = 0) -> subprocess.CompletedProcess[str]:
    installer = [sys.executable, "-c", f"import json, sys; print(json.dumps(sys.argv[1:])); sys.exit({code})", *args]
    command = updater.detached_update_command(installer, pause=0)
    # stdin is empty: the script does not wait for Enter
    return subprocess.run(command, input="", capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


def test_detached_update_passes_arguments_as_they_are():
    # what cmd.exe would split, expand or unquote
    args = [
        r"C:\Users\John Smith\.local\bin\uv.exe",
        "https://example.com/anihub.whl?a=1&b=%41",
        "%PATH%",
        'say "hi"',
        "^(x)|y>z",
        "end\\",
    ]
    done = _detached(args)
    assert json.loads(done.stdout.splitlines()[0]) == args, done.stderr
    assert "anihub is updated" in done.stdout


def test_detached_update_reports_failure():
    done = _detached([], code=3)
    assert "Update failed (exit code 3)" in done.stdout, done.stderr


def test_detached_update_does_not_use_a_shell():
    command = updater.detached_update_command(["uv", "tool", "install", "x & y"])
    assert command[1:3] == ["-I", "-c"]
    assert command[-4:] == ["uv", "tool", "install", "x & y"]
    assert "cmd" not in command[0].lower().rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
