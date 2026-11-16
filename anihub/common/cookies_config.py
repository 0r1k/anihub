import logging
import sys
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
    # every installed browser
    "any_browser",
]

if sys.platform == "darwin":
    BASE_BROWSERS.extend(["safari"])
elif sys.platform == "win32":
    BASE_BROWSERS.extend(["internet_explorer", "octo_browser"])

BROWSER_SUPPORTS = BASE_BROWSERS
# rookiepy's own `any_browser` (like `chromium_based` and `firefox_based`) needs the path of a cookie database
_ROOKIEPY_FUNCTIONS = {"any_browser": "load"}

# curl, yt-dlp and browser extensions write HttpOnly cookies (most session cookies) like a comment
HTTPONLY_PREFIX = "#HttpOnly_"
# domain, include subdomains, path, secure, expires, name, value
_NETSCAPE_FIELDS = 7


def read_from_browser(browser_name: str, domains: list[str] | None = None) -> CookieJar:
    """Cookies of the browser: only of `domains` and their subdomains if given, of every site otherwise."""
    try:
        import rookiepy  # noqa: PLC0415 (optional: anihub[cookies])
    except ImportError:
        msg = "rookiepy required"
        raise ImportError(msg) from None
    if domains is not None and not domains:
        return CookieJar()
    func_extract = getattr(rookiepy, _ROOKIEPY_FUNCTIONS.get(browser_name, browser_name), None)
    if func_extract is None:
        msg = f"rookiepy not implemented browser '{browser_name}'"
        raise AttributeError(msg)
    cookies = func_extract(domains)
    return rookiepy.to_cookiejar(cookies)


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
            secure=c["secure"],
            expires=c["expires"] if c["expires"] > 0 else None,
            discard=False,
            comment=None,
            comment_url=None,
            # where http.cookiejar and rookiepy keep the flag
            rest={"HTTPOnly": ""} if c["http_only"] else {},
            rfc2109=False,
        )
        jar.set_cookie(cookie)

    return jar


def parse_netscape_cookie_line(netscape_cookie_line: str) -> dict[str, Any]:
    """The cookie of a Netscape cookies file line, {} for a comment or an empty line.

    Raises ValueError if the line is not a cookie.
    """
    # only the line end: an empty value leaves a trailing tab
    line = netscape_cookie_line.rstrip("\r\n")
    http_only = line.startswith(HTTPONLY_PREFIX)
    if http_only:
        line = line[len(HTTPONLY_PREFIX) :]

    # Skip empty lines and comments
    if not line.strip() or line.lstrip().startswith("#"):
        return {}

    parts = line.split("\t")
    if len(parts) != _NETSCAPE_FIELDS:
        msg = f"{len(parts)} tab-separated fields instead of {_NETSCAPE_FIELDS}"
        raise ValueError(msg)

    domain, domain_flag, path, secure, expires, name, value = parts
    if not domain:
        msg = "no domain"
        raise ValueError(msg)
    try:
        # empty or 0: a session cookie
        expires_at = int(float(expires)) if expires.strip() else 0
    except (ValueError, OverflowError):
        msg = f"bad expiry time {expires!r}"
        raise ValueError(msg) from None

    return {
        "domain": domain,
        "flag": domain_flag.upper() == "TRUE",  # Convert to boolean
        "path": path,
        "secure": secure.upper() == "TRUE",  # Convert to boolean
        "expires": expires_at,  # timestamp
        "name": name,
        "value": value,
        "http_only": http_only,
    }


def parse_netscape_cookies_file(cookie_file: str | Path) -> list[dict[str, Any]]:
    """
    Parse Netscape format cookies file (curl, yt-dlp, browser extensions) into a list of dictionaries.

    Broken lines are skipped with a warning in the log.

    Args:
        cookie_file: Path to the cookies file in Netscape format

    Returns:
        List of cookie dictionaries with keys:
        domain, flag, path, secure, expires, name, value, http_only

    Raises:
        ValueError: the file has broken lines only (it is not in Netscape format)
    """
    cookies = []
    broken = 0
    # line by line: a line in another encoding is skipped like any broken one
    with open(cookie_file, "rb") as f:
        for number, raw in enumerate(f, 1):
            try:
                # utf-8-sig: Windows editors put a BOM in front of the first line
                cookie = parse_netscape_cookie_line(raw.decode("utf-8-sig" if number == 1 else "utf-8"))
            except ValueError as e:  # UnicodeDecodeError too
                broken += 1
                logger.warning("%s, line %d is skipped: %s", cookie_file, number, e)
                continue
            if cookie:
                cookies.append(cookie)
    if broken and not cookies:
        msg = (
            f"{cookie_file}: no cookies in Netscape format "
            "(tab-separated lines: domain, include subdomains, path, secure, expires, name, value)"
        )
        raise ValueError(msg)
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
