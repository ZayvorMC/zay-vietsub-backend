"""SQLite database management for Zay_Vietsub.vn."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ALLOWED_JOB_COLS = frozenset(
    {
        "title",
        "duration",
        "status",
        "progress",
        "current_step",
        "step_message",
        "has_speech",
        "pass1_subtitles_json",
        "pass2_subtitles_json",
        "settings_json",
        "input_video_path",
        "output_video_path",
        "output_video_url",
        "error_message",
        "credit_deducted",
        "updated_at",
    }
)

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "zay_vietsub.db"
STORAGE_DIR = BASE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
TEMP_DIR = STORAGE_DIR / "temp"
OUTPUTS_DIR = STORAGE_DIR / "outputs"


def ensure_directories() -> None:
    """Ensure required storage directories exist."""
    for folder in (STORAGE_DIR, UPLOADS_DIR, TEMP_DIR, OUTPUTS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return utc_now().isoformat()


def next_month_iso(from_dt: datetime | None = None) -> str:
    """Return ISO timestamp 30 days from given datetime."""
    base = from_dt or utc_now()
    return (base + timedelta(days=30)).isoformat()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection with row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize SQLite schema, migrate columns, and seed system settings & Owner."""
    ensure_directories()
    now = utc_now_iso()
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                is_banned INTEGER NOT NULL DEFAULT 0,
                plan TEXT NOT NULL DEFAULT 'free',
                monthly_quota INTEGER NOT NULL DEFAULT 10,
                monthly_used INTEGER NOT NULL DEFAULT 0,
                extra_credits INTEGER NOT NULL DEFAULT 0,
                cycle_reset_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                amount_vnd INTEGER NOT NULL,
                credits_added INTEGER NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS translation_jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_url TEXT,
                source_lang TEXT NOT NULL,
                title TEXT,
                duration REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                current_step INTEGER DEFAULT 0,
                step_message TEXT,
                has_speech INTEGER DEFAULT 1,
                pass1_subtitles_json TEXT,
                pass2_subtitles_json TEXT,
                settings_json TEXT,
                input_video_path TEXT,
                output_video_path TEXT,
                output_video_url TEXT,
                error_message TEXT,
                credit_deducted INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_user_created
                ON translation_jobs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tx_user_created
                ON transactions(user_id, created_at DESC);
            """
        )

        # Safe column migrations for existing databases
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "role" not in existing_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"
            )
        if "is_banned" not in existing_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0"
            )

        # Default maintenance settings
        conn.execute(
            """
            INSERT OR IGNORE INTO system_settings (key, value, updated_at)
            VALUES ('maintenance_mode', '0', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO system_settings (key, value, updated_at)
            VALUES ('maintenance_message', 'Hệ thống Zay_Vietsub Beta đang bảo trì nâng cấp máy chủ. Vui lòng quay lại sau!', ?)
            """,
            (now,),
        )

        # Ensure designated Owner account always holds Owner role & unlimited quota
        owner_row = conn.execute(
            "SELECT id FROM users WHERE LOWER(email) = 'hauhuythh@gmail.com'"
        ).fetchone()
        if owner_row:
            conn.execute(
                """
                UPDATE users
                SET role = 'owner', plan = 'owner', monthly_quota = 999999,
                    monthly_used = 0, extra_credits = 999999, is_banned = 0
                WHERE id = ?
                """,
                (owner_row["id"],),
            )


def get_system_settings() -> dict[str, Any]:
    """Return global server state including maintenance mode and banner message."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM system_settings"
        ).fetchall()
    kv = {r["key"]: r["value"] for r in rows}
    updated = max((r["updated_at"] for r in rows), default=utc_now_iso())
    return {
        "maintenance_mode": kv.get("maintenance_mode", "0") == "1",
        "maintenance_message": kv.get(
            "maintenance_message",
            "Hệ thống Zay_Vietsub Beta đang bảo trì nâng cấp máy chủ. Vui lòng quay lại sau!",
        ),
        "updated_at": updated,
    }


def set_system_maintenance(enabled: bool, message: str | None = None) -> dict[str, Any]:
    """Enable or disable server maintenance mode."""
    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('maintenance_mode', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("1" if enabled else "0", now),
        )
        if message is not None and message.strip():
            conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ('maintenance_message', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (message.strip(), now),
            )
    return get_system_settings()


def create_job_record(
    job_id: str,
    user_id: int,
    source_type: str,
    source_url: str | None,
    source_lang: str,
    title: str,
    settings: dict[str, Any],
    input_video_path: str | None = None,
    credit_deducted: int = 1,
) -> None:
    """Insert a new translation job record."""
    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO translation_jobs (
                job_id, user_id, source_type, source_url, source_lang, title,
                status, progress, current_step, step_message, settings_json,
                input_video_path, credit_deducted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 2, 1, 'Đang khởi tạo tác vụ...', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                source_type,
                source_url,
                source_lang,
                title,
                json.dumps(settings, ensure_ascii=False),
                input_video_path,
                credit_deducted,
                now,
                now,
            ),
        )


def update_job_record(job_id: str, **fields: Any) -> None:
    """Update specific whitelisted columns of a translation job."""
    if not fields:
        return
    unknown = set(fields) - ALLOWED_JOB_COLS
    if unknown:
        raise ValueError(f"Invalid job update fields: {sorted(unknown)}")
    fields["updated_at"] = utc_now_iso()
    assignments = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values()) + [job_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE translation_jobs SET {assignments} WHERE job_id = ?",
            values,
        )


def get_job_record(job_id: str) -> dict[str, Any] | None:
    """Fetch a translation job as a dictionary."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM translation_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if not row:
        return None
    return _deserialize_job(row)


def list_user_jobs(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent jobs for a user."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM translation_jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_deserialize_job(r) for r in rows]


def _scrub_host_paths(text: str | None) -> str | None:
    if not text:
        return text
    cleaned = re.sub(r"[A-Za-z]:\\[^\s'\"<>|]+", "[server-file]", str(text))
    cleaned = re.sub(r"[A-Za-z]:/[^\s'\"<>|]+", "[server-file]", cleaned)
    cleaned = re.sub(r"(?:/Users|/home|/var|/tmp|/etc)/[^\s'\"<>|]+", "[server-file]", cleaned)
    return cleaned


def serialize_public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Strip internal filesystem paths and raw JSON blobs before returning to HTTP clients."""
    hidden = {
        "input_video_path",
        "output_video_path",
        "pass1_subtitles_json",
        "pass2_subtitles_json",
        "settings_json",
    }
    return {k: v for k, v in job.items() if k not in hidden}


def _deserialize_job(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if data.get("error_message"):
        data["error_message"] = _scrub_host_paths(data["error_message"])
    for json_col, target_col in (
        ("pass1_subtitles_json", "pass1_subtitles"),
        ("pass2_subtitles_json", "pass2_subtitles"),
        ("settings_json", "settings"),
    ):
        raw = data.get(json_col)
        try:
            data[target_col] = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data[target_col] = None
    return data
