from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from click import Choice
from rich import get_console
from typer import BadParameter, Option

import anihub
from anihub.common.cookies_config import (
    BROWSER_SUPPORTS,
    parse_args_headers,
    parse_headers_file,
    read_from_browser,
    read_from_netscape_file,
)
from anihub.common.extractors import source_domains

from .common import storage
from .common.players import PLAYERS
from .common.utils import validate_proxy_url

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .common.updater import TPACKAGE

app = typer.Typer(
    pretty_exceptions_enable=False,
    pretty_exceptions_short=False,
    pretty_exceptions_show_locals=False,
)

BROWSER_CHOICE = Choice(BROWSER_SUPPORTS)
console = get_console()


def _get_extractors_choice():
    from anihub.common.extractors import get_extractor_modules

    return Choice(get_extractor_modules())


# typer callbacks
def _cb_check_proxy_opt(proxy: str | None) -> str | None:
    if not proxy:
        return proxy
    try:
        return validate_proxy_url(proxy)
    except ValueError as e:
        msg = f"Invalid proxy URL: {e}"
        raise BadParameter(msg) from e


def _proxy_or_saved(proxy: str | None) -> str | None:
    """`--proxy` of the command, otherwise the proxy saved in the settings (F2). `--proxy ""`: no proxy."""
    if proxy is not None:
        return proxy or None
    # the settings come with the TUI: loaded only when they are needed
    from .tui.session import Settings

    # read only: a broken file stays for the TUI, which moves it aside and tells the user
    saved = Settings.load(read_only=True).proxy
    if Settings.path() in storage.unusable_files:
        console.print(
            f"{Settings.path()} could not be read, the saved proxy is not used. Fix the file or pass --proxy",
            style="yellow",
            markup=False,
            highlight=False,
        )
    if not saved:
        return None
    try:
        return validate_proxy_url(saved)
    except ValueError as e:
        # like the TUI does with it (anihub 8.2 and older saved any scheme), F9 included
        msg = f"The saved proxy is not used, anihub connects directly: {e}. Fix it in settings (F2) or pass --proxy"
        console.print(msg, style="yellow", markup=False, highlight=False)
        return None


# release notes longer than that are cut in the update prompt
_NOTES_LIMIT = 2000
# no [default: ...]: rich markup of the help takes it for a tag and drops it
UPDATE_PROXY_HELP = (
    "proxy for the requests to GitHub and PyPI, like scheme://user:password@host:port, '' for none. "
    "Default: the proxy saved in the settings (F2)"
)


@app.command(help="Show app, anicli-api versions and exit")
def version():
    from rich.panel import Panel  # noqa
    from .common.updater import get_api_version

    api_version = get_api_version()
    renderable = f"anihub : [bold]{anihub.__version__}[/bold]\nanicli-api: [bold]{api_version}[/bold]"
    panel = Panel(renderable, title="Versions", expand=False)
    console.print(panel)


@app.command(help="update anihub from GitHub releases (asks before installing)")
def update(
    # a bool option is a flag: typer deprecates is_flag and is going to remove it
    force: Annotated[bool, Option("--force", help="reinstall even if the latest version is installed")] = False,
    yes: Annotated[bool, Option("-y", "--yes", help="do not ask for confirmation")] = False,
    proxy: Annotated[str | None, Option("--proxy", help=UPDATE_PROXY_HELP, callback=_cb_check_proxy_opt)] = None,
):
    import typer
    from rich.markdown import Markdown

    from .common import updater

    current = anihub.__version__
    try:
        # the installer below goes to GitHub by the network settings of the system (HTTPS_PROXY ...) as before
        release = updater.fetch_latest_release(proxy=_proxy_or_saved(proxy))
    except updater.UpdateExceptionError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    if release is None:
        console.print(f"No releases yet: {updater.RELEASES_PAGE}")
        return
    if not updater._is_outdated(current, release.version) and not force:
        console.print(f"[green]anihub {current} is up to date ✓[/]")
        return

    console.print(f"[bold]anihub {current} → {release.version}[/]  {release.url}")
    if release.notes:
        notes = release.notes if len(release.notes) < _NOTES_LIMIT else release.notes[:_NOTES_LIMIT] + "\n\n…"
        console.print(Markdown(notes))
    try:
        method = updater.install_method()
        command = updater.update_command(method, release.source)
    except updater.UpdateExceptionError as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1) from e

    console.print(f"[dim]{method}: {' '.join(command)}[/]")
    if not yes and not typer.confirm(f"Update anihub to {release.version}?", default=True):
        console.print("Cancelled")
        return
    code = updater.run_update(command)
    if code is None:
        console.print("The update continues in a new window, anihub is closing")
    elif code:
        console.print(f"[red]Update failed (exit code {code})[/]")
        raise typer.Exit(code)
    else:
        console.print(f"[green]anihub {release.version} installed ✓[/]")


@app.command(help="check for a new anihub release on GitHub and anicli-api on PyPI")
def check_updates(
    proxy: Annotated[str | None, Option("--proxy", help=UPDATE_PROXY_HELP, callback=_cb_check_proxy_opt)] = None,
):
    import asyncio

    from rich import box
    from rich.table import Table

    from .common.updater import check_for_updates

    result = asyncio.run(check_for_updates(proxy=_proxy_or_saved(proxy)))
    # mypy types the values of a TypedDict as object: here every one is a package
    packages = cast("Iterable[TPACKAGE]", result.values())
    table = Table("Package", "Current", "Latest", "Status", box=box.ROUNDED, title="Update check")
    for info in packages:
        status = "[bold yellow]Available ⬆[/]" if info["is_outdated"] else "[green]Up-to-date ✓[/]"
        table.add_row(info["package_name"], info["current_version"], info["latest_version"], status)
    console.print(table)
    if any(info["is_outdated"] for info in packages):
        console.print("TIP: run `anihub update` to install the new version")


def _cb_check_read_browser_cookie_opt(browser_name: str | None) -> CookieJar | None:
    if not browser_name:
        return None
    try:
        # the sites of all sources: the source is chosen later (saved settings, F2) and can change while it runs
        return read_from_browser(browser_name, source_domains())
    except ImportError as e:
        msg = "rookiepy dependency required. Add via `anihub[cookies]`"
        raise BadParameter(msg) from e
    except Exception as e:
        raise BadParameter(str(e)) from e


def _cb_check_read_netscape_cookies_opt(netscape_cookies: Path | None) -> CookieJar | None:
    if not netscape_cookies:
        return None
    try:
        return read_from_netscape_file(netscape_cookies)
    except Exception as e:
        raise BadParameter(str(e)) from e


def _cb_parse_headers_file(headers_file: Path | None) -> dict[str, str] | None:
    if not headers_file:
        return None
    try:
        return parse_headers_file(headers_file)
    except Exception as e:
        raise BadParameter(str(e)) from e


def _cb_parse_headers(headers: list[str] | None) -> list[str] | None:
    if headers:
        try:
            parse_args_headers(headers)
        except ValueError as e:
            raise BadParameter(str(e)) from e
    return headers


@app.command(help="run TUI application (play video via mpv, vlc or another player)")
def tui(
    # None: use value saved in the settings screen (F2)
    source: Annotated[
        str | None,
        Option(
            "-s",
            "--source",
            click_type=_get_extractors_choice(),
            help="extractor source [default: saved in settings or animego]",
        ),
    ] = None,
    quality: Annotated[
        int | None,
        Option(
            "-q", "--quality", help="default video quality, fallback to the closest available [default: saved or 1080]"
        ),
    ] = None,
    search: Annotated[str | None, Option("--search", help="call search by query after start")] = None,
    ongoing: Annotated[bool, Option("--ongoing", help="show ongoing after start")] = False,
    player: Annotated[
        str | None,
        Option("-p", "--player", click_type=Choice(PLAYERS), help="video player [default: saved or mpv]"),
    ] = None,
    player_command: Annotated[
        str | None,
        Option("--player-command", help="command of the custom player (with `--player custom`), eg: celluloid"),
    ] = None,
    player_args: Annotated[
        str | None,
        # -mo/--mpv-opts: backward compatibility
        Option("-pa", "--player-args", "-mo", "--mpv-opts", help="extra player arguments (should be a string)"),
    ] = None,
    lang: Annotated[
        str | None,
        Option("--lang", click_type=Choice(["en", "uk"]), help="interface language [default: saved or en]"),
    ] = None,
    m3u_size: Annotated[
        int | None, Option("--m3u-size", min=1, help="max episodes in player playlist [default: saved or 6]")
    ] = None,
    timeout: Annotated[
        int | None, Option("--timeout", min=1, help="http client timeout (seconds) [default: saved or 60]")
    ] = None,
    proxy: Annotated[
        str | None,
        Option(
            "--proxy",
            help="Proxy for anicli-api client, like scheme://user:password@host:port",
            callback=_cb_check_proxy_opt,
        ),
    ] = None,
    extract_cookies_from_browser: Annotated[
        str | None,
        Option(
            "--cookies-from-browser",
            help="extract cookies from browser",
            click_type=BROWSER_CHOICE,
            # !!! cast to CookieJar type if arg passed else None
            callback=_cb_check_read_browser_cookie_opt,
        ),
    ] = None,
    netscape_cookies: Annotated[
        Path | None,
        Option(
            "--cookies",
            help="read cookies from file in netscape format",
            # !!! cast to CookieJar type if arg passed else None
            callback=_cb_check_read_netscape_cookies_opt,
        ),
    ] = None,
    header: Annotated[
        list[str] | None,
        Option(
            "-H",
            "--header",
            help="Add extra headers to HTTP client, can be used multiple times (Key=Value)",
            callback=_cb_parse_headers,
        ),
    ] = None,
    headers_file: Annotated[
        Path | None,
        Option(
            "--header-file",
            help="Path to file with headers (one per line, Key=Value)",
            # !!! cast to Dict[str,str] type if arg passed else None
            callback=_cb_parse_headers_file,
        ),
    ] = None,
) -> None:
    if search and ongoing:
        msg = "Not allowed pass both `--search` and `--ongoing` options. Pick once option"
        raise BadParameter(msg)

    from .tui import AnihubApp, Session, Settings
    from .tui.app import EXIT_UPDATE

    headers = parse_args_headers(header) if header else {}
    if headers_file:
        headers.update(cast(dict[str, str], headers_file))

    cookies: CookieJar | None = None
    # the option callbacks have turned both values into cookie jars
    for value in (extract_cookies_from_browser, netscape_cookies):
        if not value:
            continue
        jar = cast(CookieJar, value)
        if cookies is None:
            cookies = jar
        else:
            for cookie in jar:
                cookies.set_cookie(cookie)

    # command line options override saved settings for this run only
    settings = Settings.load(
        extractor_name=source,
        quality=quality,
        player=player,
        player_command=player_command,
        player_args=player_args,
        language=lang,
        m3u_size=m3u_size,
        timeout=timeout,
        proxy=proxy,
        headers=headers,
        cookies=cookies,
    )
    session = Session(settings)
    if AnihubApp(session, query=search, ongoing=ongoing).run() == EXIT_UPDATE:
        # confirmed in the app (F9): the terminal is free for the installer output now.
        # The proxy the run ended with (changed in F2 or not, None if the saved one is refused)
        update(force=False, yes=True, proxy=session.proxy or "")


# backward compatibility: `anihub cli ...` runs the same TUI
app.command("cli", hidden=True, help="alias of `tui` command")(tui)


def main():
    app()


if __name__ == "__main__":
    main()
