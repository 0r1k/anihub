from http.cookiejar import CookieJar
from pathlib import Path
from typing import Annotated, cast

import typer
from click import Choice
from rich import get_console
from typer import BadParameter, Option

import anicli
from anicli.common.cookies_config import (
    BROWSER_SUPPORTS,
    parse_args_headers,
    parse_headers_file,
    read_from_browser,
    read_from_netscape_file,
)

from .common.players import PLAYERS
from .common.utils import validate_proxy_url

app = typer.Typer(
    pretty_exceptions_enable=False,
    pretty_exceptions_short=False,
    pretty_exceptions_show_locals=False,
)

BROWSER_CHOICE = Choice(BROWSER_SUPPORTS)
console = get_console()


def _get_extractors_choice():
    from anicli.common.extractors import get_extractor_modules

    return Choice(get_extractor_modules())


@app.command(help="Show app, anicli-api versions and exit")
def version():
    from rich.panel import Panel  # noqa
    from .common.updater import get_api_version

    api_version = get_api_version()
    renderable = f"anihub : [bold]{anicli.__version__}[/bold]\nanicli-api: [bold]{api_version}[/bold]"
    panel = Panel(renderable, title="Versions", expand=False)
    console.print(panel)


@app.command(help="update anihub from GitHub releases (asks before installing)")
def update(
    force: Annotated[
        bool, Option("--force", is_flag=True, help="reinstall even if the latest version is installed")
    ] = False,
    yes: Annotated[bool, Option("-y", "--yes", is_flag=True, help="do not ask for confirmation")] = False,
):
    import typer
    from rich.markdown import Markdown

    from .common import updater

    current = anicli.__version__
    try:
        release = updater.fetch_latest_release()
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
        notes = release.notes if len(release.notes) < 2000 else release.notes[:2000] + "\n\n…"
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
def check_updates():
    import asyncio

    from rich import box
    from rich.table import Table

    from .common.updater import check_for_updates

    result = asyncio.run(check_for_updates())
    table = Table("Package", "Current", "Latest", "Status", box=box.ROUNDED, title="Update check")
    for info in result.values():
        status = "[bold yellow]Available ⬆[/]" if info["is_outdated"] else "[green]Up-to-date ✓[/]"
        table.add_row(info["package_name"], info["current_version"], info["latest_version"], status)
    console.print(table)
    if any(info["is_outdated"] for info in result.values()):
        console.print("TIP: run `anihub update` to install the new version")


# typer callbacks
def _cb_check_proxy_opt(proxy: str | None) -> str | None:
    if not proxy:
        return proxy
    try:
        validate_proxy_url(proxy)
    except ValueError as e:
        raise BadParameter(f"Invalid proxy URL: {e}") from e
    return proxy


def _cb_check_read_browser_cookie_opt(browser_name: str | None) -> CookieJar | None:
    if not browser_name:
        return None
    try:
        return read_from_browser(browser_name)
    except ImportError as e:
        raise BadParameter("rookiepy dependency required. Add via `anihub[cookies]`") from e
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
    # fmt: off
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
        Option("--lang", click_type=Choice(["en", "ru", "uk"]), help="interface language [default: saved or en]"),
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
    # fmt: on
    if search and ongoing:
        msg = "Not allowed pass both `--search` and `--ongoing` options. Pick once option"
        raise BadParameter(msg)

    from .tui import AnicliApp, Session, Settings
    from .tui.app import EXIT_UPDATE

    headers = parse_args_headers(header) if header else {}
    if headers_file:
        headers.update(cast(dict[str, str], headers_file))

    cookies: CookieJar | None = None
    for jar in (extract_cookies_from_browser, netscape_cookies):
        if not jar:
            continue
        jar = cast(CookieJar, jar)
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
    if AnicliApp(Session(settings), query=search, ongoing=ongoing).run() == EXIT_UPDATE:
        # confirmed in the app (F9): the terminal is free for the installer output now
        update(force=False, yes=True)


# backward compatibility: `anihub cli ...` runs the same TUI
app.command("cli", hidden=True, help="alias of `tui` command")(tui)


def main():
    app()


if __name__ == "__main__":
    main()
