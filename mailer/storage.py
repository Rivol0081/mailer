from __future__ import annotations

import aiosqlite
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id    INTEGER NOT NULL,
    label         TEXT NOT NULL,
    email         TEXT NOT NULL,
    password_enc  BLOB NOT NULL,
    imap_host     TEXT NOT NULL,
    imap_port     INTEGER NOT NULL DEFAULT 993,
    imap_ssl      INTEGER NOT NULL DEFAULT 1,
    sieve_host    TEXT,
    sieve_port    INTEGER NOT NULL DEFAULT 4190,
    filter_on     INTEGER NOT NULL DEFAULT 0,
    filter_action TEXT NOT NULL DEFAULT 'trash',  -- trash | delete | flag
    UNIQUE(tg_user_id, email)
);

CREATE TABLE IF NOT EXISTS keywords (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    keyword    TEXT NOT NULL,
    UNIQUE(account_id, keyword)
);

CREATE TABLE IF NOT EXISTS active_tab (
    tg_user_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS allowlist (
    tg_user_id INTEGER PRIMARY KEY,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
"""


@dataclass
class Account:
    id: int
    tg_user_id: int
    label: str
    email: str
    password_enc: bytes
    imap_host: str
    imap_port: int
    imap_ssl: bool
    sieve_host: str | None
    sieve_port: int
    filter_on: bool
    filter_action: str


def _row_to_account(row) -> Account:
    return Account(
        id=row[0],
        tg_user_id=row[1],
        label=row[2],
        email=row[3],
        password_enc=row[4],
        imap_host=row[5],
        imap_port=row[6],
        imap_ssl=bool(row[7]),
        sieve_host=row[8],
        sieve_port=row[9],
        filter_on=bool(row[10]),
        filter_action=row[11],
    )


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def add_account(
        self,
        tg_user_id: int,
        label: str,
        email: str,
        password_enc: bytes,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        sieve_host: str | None,
        sieve_port: int,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO accounts
                   (tg_user_id, label, email, password_enc, imap_host, imap_port, imap_ssl, sieve_host, sieve_port)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tg_user_id, email) DO UPDATE SET
                     label=excluded.label,
                     password_enc=excluded.password_enc,
                     imap_host=excluded.imap_host,
                     imap_port=excluded.imap_port,
                     imap_ssl=excluded.imap_ssl,
                     sieve_host=excluded.sieve_host,
                     sieve_port=excluded.sieve_port
                   RETURNING id""",
                (tg_user_id, label, email, password_enc, imap_host, imap_port, int(imap_ssl), sieve_host, sieve_port),
            )
            row = await cur.fetchone()
            await db.commit()
            return int(row[0])

    async def list_accounts(self, tg_user_id: int) -> list[Account]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT * FROM accounts WHERE tg_user_id = ? ORDER BY id",
                (tg_user_id,),
            )
            rows = await cur.fetchall()
        return [_row_to_account(r) for r in rows]

    async def get_account(self, account_id: int) -> Account | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = await cur.fetchone()
        return _row_to_account(row) if row else None

    async def delete_account(self, account_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            await db.commit()

    async def set_active(self, tg_user_id: int, account_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO active_tab(tg_user_id, account_id) VALUES (?, ?) "
                "ON CONFLICT(tg_user_id) DO UPDATE SET account_id=excluded.account_id",
                (tg_user_id, account_id),
            )
            await db.commit()

    async def get_active(self, tg_user_id: int) -> Account | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT a.* FROM accounts a JOIN active_tab t ON t.account_id = a.id WHERE t.tg_user_id = ?",
                (tg_user_id,),
            )
            row = await cur.fetchone()
        return _row_to_account(row) if row else None

    async def set_filter_state(self, account_id: int, on: bool, action: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            if action:
                await db.execute(
                    "UPDATE accounts SET filter_on = ?, filter_action = ? WHERE id = ?",
                    (int(on), action, account_id),
                )
            else:
                await db.execute(
                    "UPDATE accounts SET filter_on = ? WHERE id = ?", (int(on), account_id)
                )
            await db.commit()

    async def list_keywords(self, account_id: int) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT keyword FROM keywords WHERE account_id = ? ORDER BY id", (account_id,)
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def add_keyword(self, account_id: int, keyword: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO keywords(account_id, keyword) VALUES (?, ?)",
                (account_id, keyword),
            )
            await db.commit()

    async def remove_keyword(self, account_id: int, keyword: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM keywords WHERE account_id = ? AND keyword = ?",
                (account_id, keyword),
            )
            await db.commit()

    async def list_filter_enabled(self) -> list[Account]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM accounts WHERE filter_on = 1")
            rows = await cur.fetchall()
        return [_row_to_account(r) for r in rows]

    # ---------- allowlist ----------

    async def is_allowed(self, tg_user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM allowlist WHERE tg_user_id = ?", (tg_user_id,)
            )
            return await cur.fetchone() is not None

    async def is_admin(self, tg_user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT is_admin FROM allowlist WHERE tg_user_id = ?", (tg_user_id,)
            )
            row = await cur.fetchone()
        return bool(row and row[0])

    async def allow_user(self, tg_user_id: int, is_admin: bool = False, note: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO allowlist(tg_user_id, is_admin, note) VALUES (?, ?, ?)
                   ON CONFLICT(tg_user_id) DO UPDATE SET
                       is_admin = MAX(allowlist.is_admin, excluded.is_admin),
                       note     = COALESCE(excluded.note, allowlist.note)""",
                (tg_user_id, int(is_admin), note),
            )
            await db.commit()

    async def revoke_user(self, tg_user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM allowlist WHERE tg_user_id = ?", (tg_user_id,))
            await db.commit()

    async def list_allowed(self) -> list[tuple[int, bool, str | None]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT tg_user_id, is_admin, note FROM allowlist ORDER BY is_admin DESC, tg_user_id"
            )
            rows = await cur.fetchall()
        return [(int(r[0]), bool(r[1]), r[2]) for r in rows]

    async def count_allowed(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM allowlist")
            row = await cur.fetchone()
        return int(row[0])
