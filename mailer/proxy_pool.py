"""Round-robin proxy rotator, keyed by account id.

A single ProxyRotator instance is shared by the bot and the filter daemon;
both go through `next_for(account)` so consecutive IMAP/Sieve connections for
the same account land on different proxies.
"""
from __future__ import annotations

import threading
from typing import Protocol

from .proxy import ProxyConfig, ProxyParseError, parse_list


class _AccountLike(Protocol):
    id: int
    proxies: str | None


class ProxyRotator:
    def __init__(self) -> None:
        self._counters: dict[int, int] = {}
        self._lock = threading.Lock()

    def _list_for(self, account: _AccountLike) -> list[ProxyConfig]:
        if not account.proxies:
            return []
        try:
            return parse_list(account.proxies)
        except ProxyParseError:
            return []

    def next_for(self, account: _AccountLike) -> ProxyConfig | None:
        proxies = self._list_for(account)
        if not proxies:
            return None
        with self._lock:
            n = self._counters.get(account.id, 0)
            self._counters[account.id] = n + 1
        return proxies[n % len(proxies)]

    def count_for(self, account: _AccountLike) -> int:
        return len(self._list_for(account))

    def reset(self, account_id: int) -> None:
        with self._lock:
            self._counters.pop(account_id, None)
