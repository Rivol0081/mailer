"""Proxy URL parsing for HTTP/HTTPS/SOCKS5 in four common credential layouts.

Accepted forms (scheme prefix optional, defaults to http):

    login:password@ip:port
    ip:port@login:password
    login:password:ip:port
    ip:port:login:password
    ip:port                 (no auth)
    scheme://...            (any of the above with explicit scheme)

scheme in {http, https, socks5, socks5h, socks4}.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


@dataclass(frozen=True)
class ProxyConfig:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password is not None:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def short(self) -> str:
        prefix = self.scheme + "://"
        if self.username:
            return f"{prefix}{self.username}:***@{self.host}:{self.port}"
        return f"{prefix}{self.host}:{self.port}"


class ProxyParseError(ValueError):
    pass


def _is_port(s: str) -> bool:
    return s.isdigit() and 1 <= int(s) <= 65535


def _looks_like_host_port(left: str, right: str) -> bool:
    """Heuristic: 'left:right' is host:port if right is a port number
    AND left looks like a hostname/IP (no spaces, has a dot or all digits or letters)."""
    if not _is_port(right):
        return False
    if not left or " " in left:
        return False
    return True


def parse(raw: str, default_scheme: str = "http") -> ProxyConfig:
    s = raw.strip()
    if not s:
        raise ProxyParseError("empty proxy string")

    # Scheme prefix
    scheme = default_scheme
    if "://" in s:
        scheme, _, s = s.partition("://")
        scheme = scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise ProxyParseError(f"unsupported scheme: {scheme}")
        s = s.strip()
        if not s:
            raise ProxyParseError("missing host after scheme")

    host: str
    port: int
    user: str | None = None
    pwd: str | None = None

    # Form A/B: contains '@'
    if "@" in s:
        left, _, right = s.rpartition("@")
        # Decide which side is host:port and which is login:password
        def _split_pair(p: str) -> tuple[str, str] | None:
            if p.count(":") != 1:
                return None
            a, b = p.split(":")
            return (a, b)

        l = _split_pair(left)
        r = _split_pair(right)
        if l and _looks_like_host_port(l[0], l[1]) and not (r and _looks_like_host_port(r[0], r[1])):
            # ip:port@login:password
            host, port_s = l
            user_s, pwd_s = right.split(":", 1) if ":" in right else (right, "")
            user, pwd = user_s, pwd_s
            port = int(port_s)
        elif r and _looks_like_host_port(r[0], r[1]):
            # login:password@ip:port  (canonical)
            host, port_s = r
            port = int(port_s)
            if ":" in left:
                user_s, pwd_s = left.split(":", 1)
                user, pwd = user_s, pwd_s
            else:
                user = left
        else:
            raise ProxyParseError(f"cannot find host:port around '@' in {raw!r}")

    else:
        # Form C/D or no auth: split by ':'
        parts = s.split(":")
        if len(parts) == 2:
            # ip:port (no auth)
            h, p_ = parts
            if not _looks_like_host_port(h, p_):
                raise ProxyParseError(f"expected ip:port, got {raw!r}")
            host, port = h, int(p_)
        elif len(parts) == 4:
            a, b, c, d = parts
            b_is_port = _is_port(b)
            d_is_port = _is_port(d)
            if b_is_port and not d_is_port:
                # ip:port:login:password
                host, port = a, int(b)
                user, pwd = c, d
            elif d_is_port and not b_is_port:
                # login:password:ip:port
                user, pwd = a, b
                host, port = c, int(d)
            elif b_is_port and d_is_port:
                # Ambiguous (both halves look like ip:port). Prefer login:password:ip:port,
                # which is the more common dump format.
                user, pwd = a, b
                host, port = c, int(d)
            else:
                raise ProxyParseError(f"cannot detect port in {raw!r}")
        else:
            raise ProxyParseError(
                f"expected one of host:port, host:port:user:pass, user:pass:host:port, got {raw!r}"
            )

    if not host:
        raise ProxyParseError("empty host")
    if not (1 <= port <= 65535):
        raise ProxyParseError(f"port out of range: {port}")
    if user == "":
        user = None
    if pwd == "":
        pwd = None

    return ProxyConfig(scheme=scheme, host=host, port=port, username=user, password=pwd)


def parse_list(raw: str, default_scheme: str = "http") -> list[ProxyConfig]:
    """Parse a multi-line/multi-entry proxy block.

    Splits on newlines, commas, semicolons. Ignores blank lines and lines
    starting with '#'. Each entry must be a valid proxy string parseable by
    parse(). Raises ProxyParseError on the first bad entry, with the line
    index in the message.
    """
    out: list[ProxyConfig] = []
    entries: list[str] = []
    for chunk in raw.replace(";", "\n").replace(",", "\n").splitlines():
        line = chunk.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    for i, entry in enumerate(entries, 1):
        try:
            out.append(parse(entry, default_scheme=default_scheme))
        except ProxyParseError as e:
            raise ProxyParseError(f"entry {i} ({entry!r}): {e}") from e
    return out
