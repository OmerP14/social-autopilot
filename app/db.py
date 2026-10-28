"""SQLite: içerik planı, durumlar, fotoğraf kullanımı ve Instagram token'ları (tüm firmalar tek dosya)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from app.config import settings

# İçerik durumları
DRAFT = "DRAFT"            # planlandı, henüz üretilmedi
PENDING = "PENDING"        # üretildi, Telegram'da onay bekliyor
APPROVED = "APPROVED"      # onaylandı, saati gelince paylaşılacak
REJECTED = "REJECTED"
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"          # üretim ya da paylaşım hatası (Telegram'a bildirilir)
MANUAL = "MANUAL"          # Reels: video Telegram'a gönderildi, kullanıcı Instagram'dan müzikle elle paylaşacak

SCHEMA = """
CREATE TABLE IF NOT EXISTS contents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client        TEXT NOT NULL,
    month         TEXT NOT NULL,              -- 2026-11
    scheduled_at  TEXT NOT NULL,              -- ISO, Europe/Istanbul
    kind          TEXT NOT NULL,              -- post | story | reel | story_video
    spec          TEXT NOT NULL,              -- planlayıcının JSON çıktısı (şablon, metinler, fotoğraflar)
    caption       TEXT NOT NULL DEFAULT '',
    media         TEXT,                       -- üretilen dosya (output/...)
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    feedback      TEXT,                       -- son "Düzelt" notu
    tg_message_id INTEGER,
    ig_media_id   TEXT,
    permalink     TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contents_due ON contents(status, scheduled_at);

CREATE TABLE IF NOT EXISTS photo_usage (
    client   TEXT NOT NULL,
    photo    TEXT NOT NULL,                   -- katalog id
    used_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    client  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (client, key)
);

CREATE TABLE IF NOT EXISTS ig_accounts (
    client        TEXT PRIMARY KEY,
    ig_user_id    TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def add_content(client: str, month: str, scheduled_at: str, kind: str, spec: dict) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO contents (client, month, scheduled_at, kind, spec, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client, month, scheduled_at, kind, json.dumps(spec, ensure_ascii=False), now(), now()),
        )
        return cur.lastrowid


def get(content_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()


def update(content_id: int, **fields) -> None:
    if "spec" in fields and not isinstance(fields["spec"], str):
        fields["spec"] = json.dumps(fields["spec"], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE contents SET {cols} WHERE id = ?", (*fields.values(), content_id))


def month_contents(client: str, month: str) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM contents WHERE client = ? AND month = ? ORDER BY scheduled_at",
            (client, month),
        ).fetchall()


def by_status(status: str) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT * FROM contents WHERE status = ? ORDER BY scheduled_at", (status,)
        ).fetchall()


def due(before_iso: str) -> list[sqlite3.Row]:
    """Saati gelmiş, onaylı içerikler."""
    with connect() as con:
        return con.execute(
            "SELECT * FROM contents WHERE status = ? AND scheduled_at <= ? ORDER BY scheduled_at",
            (APPROVED, before_iso),
        ).fetchall()


def delete_month(client: str, month: str, statuses: tuple[str, ...] = (DRAFT, PENDING, FAILED)) -> int:
    """Planı yeniden üretmeden önce onaylanmamış içerikleri temizler."""
    marks = ",".join("?" * len(statuses))
    with connect() as con:
        cur = con.execute(
            f"DELETE FROM contents WHERE client = ? AND month = ? AND status IN ({marks})",
            (client, month, *statuses),
        )
        return cur.rowcount


def range_contents(client: str, start: str, end: str) -> list[sqlite3.Row]:
    """[start, end) aralığındaki içerikler (ISO tarih/saat)."""
    with connect() as con:
        return con.execute(
            "SELECT * FROM contents WHERE client = ? AND scheduled_at >= ? AND scheduled_at < ? ORDER BY scheduled_at",
            (client, start, end),
        ).fetchall()


def delete_range(client: str, start: str, end: str,
                 statuses: tuple[str, ...] = (DRAFT, PENDING, FAILED)) -> int:
    marks = ",".join("?" * len(statuses))
    with connect() as con:
        cur = con.execute(
            f"DELETE FROM contents WHERE client = ? AND scheduled_at >= ? AND scheduled_at < ?"
            f" AND status IN ({marks})", (client, start, end, *statuses))
        return cur.rowcount


def get_setting(client: str, key: str, default: str = "") -> str:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE client = ? AND key = ?", (client, key)).fetchone()
    return row["value"] if row else default


def set_setting(client: str, key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO settings (client, key, value) VALUES (?, ?, ?)"
                    " ON CONFLICT(client, key) DO UPDATE SET value = excluded.value", (client, key, value))


def mark_photos_used(client: str, photo_ids: list[str]) -> None:
    with connect() as con:
        con.executemany(
            "INSERT INTO photo_usage (client, photo, used_at) VALUES (?, ?, ?)",
            [(client, p, now()) for p in photo_ids],
        )


def photo_use_counts(client: str) -> dict[str, int]:
    with connect() as con:
        rows = con.execute(
            "SELECT photo, COUNT(*) AS n FROM photo_usage WHERE client = ? GROUP BY photo", (client,)
        ).fetchall()
    return {r["photo"]: r["n"] for r in rows}


def save_ig_account(client: str, ig_user_id: str, token: str, expires_at: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO ig_accounts (client, ig_user_id, access_token, expires_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(client) DO UPDATE SET ig_user_id = excluded.ig_user_id,"
            " access_token = excluded.access_token, expires_at = excluded.expires_at",
            (client, ig_user_id, token, expires_at),
        )


def ig_account(client: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM ig_accounts WHERE client = ?", (client,)).fetchone()


def ig_accounts() -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute("SELECT * FROM ig_accounts").fetchall()
