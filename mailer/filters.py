"""Filter engine: hybrid Sieve / client-side IMAP polling."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from . import imap_client, sieve_client
from .crypto import CredCipher
from .storage import Account, Storage


log = logging.getLogger(__name__)


@dataclass
class FilterReport:
    mode: str           # "sieve" or "client"
    folders: dict[str, int]  # folder -> count moved/deleted/flagged
    error: str | None = None

    @property
    def total(self) -> int:
        return sum(v for v in self.folders.values() if v > 0)


async def install_or_skip_sieve(
    account: Account, cipher: CredCipher, keywords: list[str]
) -> tuple[str, str | None]:
    """Try to install a Sieve script. Return (mode, error)."""
    if not account.sieve_host or not keywords:
        return ("client", None)
    password = cipher.decrypt(account.password_enc)
    try:
        await sieve_client.install_filter(
            account.sieve_host, account.sieve_port,
            account.email, password,
            keywords, account.filter_action,
        )
        return ("sieve", None)
    except Exception as e:
        log.info("Falling back to client-side filter for %s: %s", account.email, e)
        return ("client", str(e))


async def remove_sieve_safe(account: Account, cipher: CredCipher) -> None:
    if not account.sieve_host:
        return
    password = cipher.decrypt(account.password_enc)
    try:
        await sieve_client.remove_filter(
            account.sieve_host, account.sieve_port, account.email, password
        )
    except Exception as e:
        log.info("Sieve remove failed for %s: %s", account.email, e)


async def run_client_filter_once(account: Account, cipher: CredCipher, keywords: list[str]) -> FilterReport:
    """Single pass of client-side filtering on INBOX."""
    if not keywords:
        return FilterReport(mode="client", folders={})
    password = cipher.decrypt(account.password_enc)
    affected: dict[str, int] = {}
    try:
        async with imap_client.imap_session(
            account.imap_host, account.imap_port, account.imap_ssl,
            account.email, password,
        ) as client:
            all_uids: set[str] = set()
            for kw in keywords:
                uids = await imap_client.search_keyword(client, "INBOX", kw)
                all_uids.update(uids)
            uids_list = sorted(all_uids, key=int) if all_uids else []
            if not uids_list:
                return FilterReport(mode="client", folders={})
            if account.filter_action == "delete":
                affected["INBOX"] = await imap_client.delete_uids(client, uids_list)
            elif account.filter_action == "flag":
                affected["INBOX"] = await imap_client.flag_uids(client, uids_list)
            else:  # trash
                trash = await imap_client.find_trash(client) or "Trash"
                # Need to re-select INBOX since find_trash listed folders.
                await client.select("INBOX")
                affected["INBOX"] = await imap_client.move_uids(client, uids_list, trash)
        return FilterReport(mode="client", folders=affected)
    except Exception as e:
        log.exception("Client filter pass failed for %s", account.email)
        return FilterReport(mode="client", folders=affected, error=str(e))


class FilterDaemon:
    """Periodic client-side filter runner for accounts where Sieve isn't available."""

    def __init__(self, storage: Storage, cipher: CredCipher, interval: int):
        self.storage = storage
        self.cipher = cipher
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="filter-daemon")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                accounts = await self.storage.list_filter_enabled()
                for acc in accounts:
                    keywords = await self.storage.list_keywords(acc.id)
                    if not keywords:
                        continue
                    report = await run_client_filter_once(acc, self.cipher, keywords)
                    if report.total:
                        log.info(
                            "Client filter [%s]: %d messages processed", acc.email, report.total
                        )
            except Exception:
                log.exception("Filter daemon iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
