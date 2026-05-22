"""IMAP/ManageSieve server auto-discovery.

Strategy:
1. Hard-coded map for well-known providers (fast path, no network).
2. Mozilla autoconfig: https://autoconfig.thunderbird.net/v1.1/<domain>
3. DNS SRV record _imaps._tcp.<domain> (best effort).
"""
from __future__ import annotations

import asyncio
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx


@dataclass
class ServerInfo:
    imap_host: str
    imap_port: int = 993
    imap_ssl: bool = True
    sieve_host: str | None = None
    sieve_port: int = 4190


# Curated map for popular providers (saves us a network round-trip).
KNOWN_PROVIDERS: dict[str, ServerInfo] = {
    "gmail.com": ServerInfo("imap.gmail.com"),
    "googlemail.com": ServerInfo("imap.gmail.com"),
    "outlook.com": ServerInfo("outlook.office365.com"),
    "hotmail.com": ServerInfo("outlook.office365.com"),
    "live.com": ServerInfo("outlook.office365.com"),
    "office365.com": ServerInfo("outlook.office365.com"),
    "yahoo.com": ServerInfo("imap.mail.yahoo.com"),
    "ymail.com": ServerInfo("imap.mail.yahoo.com"),
    "aol.com": ServerInfo("imap.aol.com"),
    "icloud.com": ServerInfo("imap.mail.me.com"),
    "me.com": ServerInfo("imap.mail.me.com"),
    "mac.com": ServerInfo("imap.mail.me.com"),
    "yandex.ru": ServerInfo("imap.yandex.ru", sieve_host="imap.yandex.ru"),
    "yandex.com": ServerInfo("imap.yandex.com", sieve_host="imap.yandex.com"),
    "ya.ru": ServerInfo("imap.yandex.ru", sieve_host="imap.yandex.ru"),
    "mail.ru": ServerInfo("imap.mail.ru"),
    "bk.ru": ServerInfo("imap.mail.ru"),
    "inbox.ru": ServerInfo("imap.mail.ru"),
    "list.ru": ServerInfo("imap.mail.ru"),
    "fastmail.com": ServerInfo("imap.fastmail.com", sieve_host="imap.fastmail.com"),
    "protonmail.com": ServerInfo("127.0.0.1", 1143, False),  # Proton Bridge
    "proton.me": ServerInfo("127.0.0.1", 1143, False),
    "gmx.com": ServerInfo("imap.gmx.com"),
    "gmx.de": ServerInfo("imap.gmx.net"),
    "web.de": ServerInfo("imap.web.de"),
    "zoho.com": ServerInfo("imap.zoho.com"),
}


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower().strip()


async def _try_autoconfig(domain: str) -> ServerInfo | None:
    urls = [
        f"https://autoconfig.thunderbird.net/v1.1/{domain}",
        f"https://autoconfig.{domain}/mail/config-v1.1.xml",
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    ]
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        for url in urls:
            try:
                r = await client.get(url)
                if r.status_code != 200 or not r.text.strip().startswith("<"):
                    continue
                info = _parse_autoconfig_xml(r.text)
                if info:
                    return info
            except (httpx.HTTPError, ET.ParseError):
                continue
    return None


def _parse_autoconfig_xml(xml_text: str) -> ServerInfo | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for srv in root.iter("incomingServer"):
        if srv.attrib.get("type") != "imap":
            continue
        host_el = srv.find("hostname")
        port_el = srv.find("port")
        sock_el = srv.find("socketType")
        if host_el is None or not host_el.text:
            continue
        port = int(port_el.text) if (port_el is not None and port_el.text) else 993
        ssl = (sock_el.text or "").upper() in ("SSL", "TLS") if sock_el is not None else True
        return ServerInfo(host_el.text.strip(), port, ssl)
    return None


async def _try_dns_srv(domain: str) -> ServerInfo | None:
    def _resolve() -> str | None:
        try:
            import dns.resolver  # type: ignore
        except ImportError:
            return None
        try:
            answers = dns.resolver.resolve(f"_imaps._tcp.{domain}", "SRV")
            for a in answers:
                return str(a.target).rstrip(".")
        except Exception:
            return None
        return None

    host = await asyncio.to_thread(_resolve)
    return ServerInfo(host) if host else None


async def _try_common_prefixes(domain: str) -> ServerInfo | None:
    """Last-resort heuristic: try imap.<domain> / mail.<domain>."""
    def _resolves(host: str) -> bool:
        try:
            socket.gethostbyname(host)
            return True
        except OSError:
            return False

    for prefix in ("imap.", "mail.", ""):
        host = prefix + domain
        if await asyncio.to_thread(_resolves, host):
            return ServerInfo(host)
    return None


async def discover(email: str) -> ServerInfo | None:
    """Return an IMAP ServerInfo for the given email, or None if not found."""
    domain = domain_of(email)
    if domain in KNOWN_PROVIDERS:
        return KNOWN_PROVIDERS[domain]
    info = await _try_autoconfig(domain)
    if info:
        return info
    info = await _try_dns_srv(domain)
    if info:
        return info
    return await _try_common_prefixes(domain)
