"""Thin async IMAP wrapper around aioimaplib for the operations we need."""
from __future__ import annotations

import asyncio
import re
import ssl as ssl_mod
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aioimaplib import aioimaplib

from .proxy import ProxyConfig


async def _open_via_proxy(host: str, port: int, ssl_context, proxy: ProxyConfig, timeout: int):
    """Open a TCP+TLS connection via the given proxy. Returns (transport, sock).

    The socket is wrapped using asyncio's create_connection(sock=...) so the
    rest of aioimaplib's protocol machinery works unchanged.
    """
    from python_socks.async_.asyncio import Proxy as SocksProxy

    proxy_url = proxy.url
    sp = SocksProxy.from_url(proxy_url)
    sock = await sp.connect(dest_host=host, dest_port=port, timeout=timeout)
    sock.setblocking(False)
    return sock


class _ProxiedIMAP4SSL(aioimaplib.IMAP4_SSL):
    """IMAP4 over SSL routed through an HTTP/SOCKS proxy.

    Overrides create_client to tunnel via python_socks instead of letting
    aioimaplib open the connection directly.
    """

    def __init__(self, host, port=993, proxy: ProxyConfig | None = None,
                 ssl_context=None, timeout=20):
        self._proxy = proxy
        super().__init__(host=host, port=port, ssl_context=ssl_context, timeout=timeout)

    def create_client(self, host, port, loop, conn_lost_cb=None, ssl_context=None):
        if self._proxy is None:
            return super().create_client(host, port, loop, conn_lost_cb, ssl_context)
        # Mirror upstream: build protocol now, defer the connect as a task.
        self.protocol = aioimaplib.IMAP4ClientProtocol(loop, conn_lost_cb)

        async def _connect():
            sock = await _open_via_proxy(host, port, ssl_context, self._proxy, self.timeout)
            await loop.create_connection(
                lambda: self.protocol,
                sock=sock,
                ssl=ssl_context,
                server_hostname=host,
            )

        loop.create_task(_connect())


@dataclass
class FolderInfo:
    name: str
    flags: list[str]


def _decode_mailbox_name(raw: str) -> str:
    # aioimaplib returns quoted names; strip quotes.
    m = re.search(r'"([^"]*)"$', raw)
    if m:
        return m.group(1)
    parts = raw.rsplit(" ", 1)
    return parts[-1].strip('"')


@asynccontextmanager
async def imap_session(
    host: str, port: int, ssl: bool, user: str, password: str,
    proxy: ProxyConfig | None = None,
):
    ctx = ssl_mod.create_default_context() if ssl else None
    if proxy is not None and ssl:
        client = _ProxiedIMAP4SSL(host=host, port=port, proxy=proxy, ssl_context=ctx, timeout=20)
    elif ssl:
        client = aioimaplib.IMAP4_SSL(host=host, port=port, ssl_context=ctx, timeout=20)
    elif proxy is not None:
        # Plain (non-SSL) IMAP through a proxy is uncommon; refuse to keep code simple.
        raise NotImplementedError("Proxy is only supported for IMAPS (SSL) connections.")
    else:
        client = aioimaplib.IMAP4(host=host, port=port, timeout=20)
    await client.wait_hello_from_server()
    resp = await client.login(user, password)
    if resp.result != "OK":
        raise PermissionError(f"IMAP login failed for {user}: {resp.result} {resp.lines}")
    try:
        yield client
    finally:
        try:
            await client.logout()
        except Exception:
            pass


async def check_credentials(
    host: str, port: int, ssl: bool, user: str, password: str,
    proxy: ProxyConfig | None = None,
) -> bool:
    try:
        async with imap_session(host, port, ssl, user, password, proxy=proxy):
            return True
    except Exception:
        return False


async def list_folders(client) -> list[FolderInfo]:
    resp = await client.list('""', "*")
    out: list[FolderInfo] = []
    if resp.result != "OK":
        return out
    for line in resp.lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line or line.startswith("LIST"):
            continue
        # Format: (\HasNoChildren) "/" "INBOX"
        m = re.match(r"\(([^)]*)\)\s+\"[^\"]*\"\s+(.+)", line)
        if not m:
            continue
        flags = m.group(1).split()
        name = m.group(2).strip().strip('"')
        out.append(FolderInfo(name=name, flags=flags))
    return out


def _find_special(folders: list[FolderInfo], flag: str, *fallbacks: str) -> str | None:
    flag_lc = flag.lower()
    for f in folders:
        if any(fl.lower() == flag_lc for fl in f.flags):
            return f.name
    names_lc = {f.name.lower(): f.name for f in folders}
    for fb in fallbacks:
        if fb.lower() in names_lc:
            return names_lc[fb.lower()]
    return None


async def find_trash(client) -> str | None:
    folders = await list_folders(client)
    return _find_special(folders, "\\Trash", "Trash", "Корзина", "Удалённые", "Deleted Items")


async def search_keyword(client, folder: str, keyword: str) -> list[str]:
    """Return UIDs (as str) of messages matching the keyword in subject or body."""
    resp = await client.select(folder)
    if resp.result != "OK":
        return []
    # IMAP search: OR SUBJECT "kw" BODY "kw"
    safe = keyword.replace("\\", "\\\\").replace('"', '\\"')
    cmd = f'(OR SUBJECT "{safe}" BODY "{safe}")'
    resp = await client.uid_search(cmd)
    if resp.result != "OK" or not resp.lines:
        return []
    raw = resp.lines[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw.split()


async def move_uids(client, uids: list[str], target_folder: str) -> int:
    if not uids:
        return 0
    uid_set = ",".join(uids)
    # Try UID MOVE (RFC 6851); fall back to COPY + STORE \Deleted + EXPUNGE.
    resp = await client.uid("MOVE", uid_set, target_folder)
    if resp.result == "OK":
        return len(uids)
    resp = await client.uid("COPY", uid_set, target_folder)
    if resp.result != "OK":
        return 0
    await client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
    await client.expunge()
    return len(uids)


async def delete_uids(client, uids: list[str]) -> int:
    if not uids:
        return 0
    uid_set = ",".join(uids)
    resp = await client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
    if resp.result != "OK":
        return 0
    await client.expunge()
    return len(uids)


async def flag_uids(client, uids: list[str], flag: str = "\\Flagged") -> int:
    if not uids:
        return 0
    uid_set = ",".join(uids)
    resp = await client.uid("STORE", uid_set, "+FLAGS", f"({flag})")
    return len(uids) if resp.result == "OK" else 0


async def wipe_folder(client, folder: str) -> int:
    """Permanently delete all messages in the given folder."""
    resp = await client.select(folder)
    if resp.result != "OK":
        return 0
    resp = await client.uid_search("ALL")
    if resp.result != "OK" or not resp.lines:
        return 0
    raw = resp.lines[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    uids = raw.split()
    return await delete_uids(client, uids)


@dataclass
class MailHeader:
    uid: str
    subject: str
    from_addr: str
    date: str
    seen: bool


def _decode_header(raw: str) -> str:
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


async def _fetch_one(client, uid: str) -> MailHeader | None:
    resp = await client.uid(
        "FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
    )
    if resp.result != "OK":
        return None
    flags = ""
    text_parts: list[str] = []
    for line in resp.lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        if not line:
            continue
        mf = re.search(r"FLAGS \(([^)]*)\)", line)
        if mf and not flags:
            flags = mf.group(1)
        if ":" in line and line.lstrip().lower().split(":", 1)[0] in ("subject", "from", "date"):
            text_parts.append(line.strip())
    headers: dict[str, str] = {}
    for part in text_parts:
        key, _, val = part.partition(":")
        headers[key.strip().lower()] = val.strip()
    return MailHeader(
        uid=uid,
        subject=_decode_header(headers.get("subject", "(no subject)")),
        from_addr=_decode_header(headers.get("from", "")),
        date=headers.get("date", ""),
        seen="\\Seen" in flags,
    )


async def fetch_recent(client, folder: str, limit: int = 10) -> list[MailHeader]:
    resp = await client.select(folder)
    if resp.result != "OK":
        return []
    resp = await client.uid_search("ALL")
    if resp.result != "OK" or not resp.lines:
        return []
    raw = resp.lines[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    uids = raw.split()
    if not uids:
        return []
    tail = uids[-limit:][::-1]
    out: list[MailHeader] = []
    for uid in tail:
        h = await _fetch_one(client, uid)
        if h:
            out.append(h)
    return out


async def folder_stats(client, folder: str) -> tuple[int, int]:
    """Return (total, unseen) for the folder, or (-1, -1) if unavailable."""
    resp = await client.select(folder)
    if resp.result != "OK":
        return (-1, -1)
    total = -1
    unseen = -1
    for line in resp.lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        m = re.match(r"(\d+) EXISTS", line)
        if m:
            total = int(m.group(1))
    resp = await client.uid_search("UNSEEN")
    if resp.result == "OK" and resp.lines:
        raw = resp.lines[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        unseen = len([x for x in raw.split() if x.isdigit()])
    return (total, unseen)


async def wipe_all(client) -> dict[str, int]:
    """Empty every selectable folder. Returns {folder: deleted_count}."""
    folders = await list_folders(client)
    out: dict[str, int] = {}
    for f in folders:
        if "\\Noselect" in f.flags:
            continue
        try:
            out[f.name] = await wipe_folder(client, f.name)
        except Exception:
            out[f.name] = -1
    return out
