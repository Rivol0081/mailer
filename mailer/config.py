import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    fernet_key: bytes
    db_path: Path
    filter_interval: int
    admin_ids: frozenset[int]


def _parse_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


def load_settings() -> Settings:
    token = os.environ.get("BOT_TOKEN", "").strip()
    key = os.environ.get("FERNET_KEY", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set (see .env.example)")
    if not key:
        raise RuntimeError("FERNET_KEY is not set (see .env.example)")
    db_path = Path(os.environ.get("DB_PATH", "./data/mailer.db")).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    interval = int(os.environ.get("FILTER_INTERVAL", "120"))
    admin_ids = _parse_ids(os.environ.get("ADMIN_IDS", ""))
    return Settings(
        bot_token=token,
        fernet_key=key.encode(),
        db_path=db_path,
        filter_interval=interval,
        admin_ids=admin_ids,
    )
