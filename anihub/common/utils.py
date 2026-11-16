from urllib.parse import urlsplit, urlunsplit

# what httpx connects through (socksio comes with anicli-api): an unknown scheme makes it raise on client creation
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_SCHEMES_TEXT = f"{', '.join(PROXY_SCHEMES[:-1])} or {PROXY_SCHEMES[-1]}"


def is_arabic_digit(s: str) -> bool:
    # str.isdigit() returns True for all number-like Unicode chars
    # >>> "١٢٣".isdigit()
    # True
    # >>> "¹".isdigit()
    # True
    return s.isascii() and s.isdigit()


def validate_proxy_url(value: str) -> str:
    """
    Validate proxy URL.

    Expected formats (scheme: one of PROXY_SCHEMES):
      - scheme://user:password@host:port
      - scheme://host:port

    Raises:
        ValueError: if the URL is invalid, missing required parts or httpx cannot use it.
    Returns:
        str: normalized URL if valid, the one to give the http clients.
    """
    parts = urlsplit(value)

    if not parts.scheme or not parts.netloc:
        msg = f"Missing proxy scheme ({_SCHEMES_TEXT})"
        raise ValueError(msg)
    if parts.scheme not in PROXY_SCHEMES:
        msg = f"Unsupported proxy scheme {parts.scheme!r}, use {_SCHEMES_TEXT}"
        raise ValueError(msg)
    if not parts.hostname:
        msg = "Missing proxy hostname"
        raise ValueError(msg)
    if parts.port is None:
        msg = "Missing proxy port"
        raise ValueError(msg)

    # urlunsplit requires a 5-tuple: (scheme, netloc, path, query, fragment)
    # user info is kept as typed (percent-encoded or not), an IPv6 host gets its brackets back
    userinfo, _, _ = parts.netloc.rpartition("@")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{userinfo}@{host}:{parts.port}" if userinfo else f"{host}:{parts.port}"
    url = urlunsplit((parts.scheme, netloc, "", "", ""))

    # httpx parses stricter than urllib (control characters, hosts): ask the parser that will get the URL.
    # Imported here: httpx takes as long to import as the whole command line interface
    import httpx  # noqa: PLC0415

    try:
        httpx.Proxy(url)
    except (ValueError, httpx.InvalidURL) as e:
        raise ValueError(str(e)) from e
    return url
