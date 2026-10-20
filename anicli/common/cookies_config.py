import sys
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

BASE_BROWSERS = [
    "firefox",
    "librewolf",
    "brave",
    "edge",
    "chrome",
    "chromium",
    "arc",
    "opera",
    "opera_gx",
    "vivaldi",
    "chromium_based",
    "firefox_based",
    "any_browser",
]

if sys.platform == "darwin":
    BASE_BROWSERS.extend(["safari"])
elif sys.platform == "win32":
    BASE_BROWSERS.extend(["internet_explorer", "octo_browser"])

BROWSER_SUPPORTS = BASE_BROWSERS


def read_from_browser(browser_name: str, domains: list[str] | None = None) -> CookieJar:
    try:
        import rookiepy

        try:
            func_extract = getattr(rookiepy, browser_name)
        except AttributeError:
            msg = f"rookiepy not implemented browser '{browser_name}'"
            raise AttributeError(msg)
        cookies = func_extract(domains)
        return rookiepy.to_cookiejar(cookies)
    except ImportError:
        raise ImportError("rookiepy required")


def read_from_netscape_file(filename: str | Path) -> CookieJar:
    cookies = parse_netscape_cookies_file(filename)
    jar = CookieJar()

    for c in cookies:
        cookie = Cookie(
            version=0,
            name=c["name"],
            value=c["value"],
            port=None,
            port_specified=False,
            domain=c["domain"],
            domain_specified=True,
            domain_initial_dot=c["domain"].startswith("."),
            path=c["path"],
            path_specified=True,
            secure=c["secure"],  # type: ignore
            expires=c["expires"] if c["expires"] > 0 else None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)

    return jar


def parse_netscape_cookie_line(netscape_cookie_line: str) -> dict[str, Any]:
    line = netscape_cookie_line.strip()

    # Skip empty lines and comments
    if not netscape_cookie_line or line.startswith("#"):
        return {}

    parts = line.split("\t")

    # Skip lines with incorrect number of fields
    if len(parts) != 7:
        return {}

    domain, domain_flag, path, secure, expires, name, value = parts

    return {
        "domain": domain,
        "flag": domain_flag == "TRUE",  # Convert to boolean
        "path": path,
        "secure": secure == "TRUE",  # Convert to boolean
        "expires": int(expires),  # Convert to timestamp
        "name": name,
        "value": value,
    }


def parse_netscape_cookie_string(netscape_cookie_string: str) -> list[dict[str, str]]:
    cookies = []
    for line in netscape_cookie_string.splitlines():
        cookie = parse_netscape_cookie_line(line)
        if not cookie:
            continue
        cookies.append(cookie)
    return cookies


def parse_netscape_cookies_file(cookie_file: str | Path) -> list[dict[str, Any]]:
    """
    Parse Netscape format cookies file into a list of dictionaries.

    Args:
        cookie_file: Path to the cookies file in Netscape format

    Returns:
        List of cookie dictionaries with keys:
        domain, flag, path, secure, expires, name, value
    """
    cookies = []
    with open(cookie_file, encoding="utf-8") as f:
        for line in f:
            cookie = parse_netscape_cookie_line(line)
            if not cookie:
                continue
            cookies.append(cookie)
    return cookies


def parse_args_headers(raw_headers: list[str]) -> dict[str, str]:
    out = {}
    for line in raw_headers:
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            msg = f"Bad header {line!r}, expected Key=Value"
            raise ValueError(msg)
        out[key.strip()] = value.strip()
    return out


def parse_headers_file(headers_file: str | Path) -> dict[str, str]:
    with open(headers_file, encoding="utf-8") as f:
        lines = [line for line in f.read().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return parse_args_headers(lines)
