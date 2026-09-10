"""SQLite ledger. It is the source of idempotency, audit and emergency state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from analyzer import DEFAULT_FOLLOWUP, DEFAULT_STOP_WORDS, Decision, DialogStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY, session_name TEXT UNIQUE NOT NULL, title TEXT,
              phone TEXT, username TEXT, session_path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              auth_status TEXT NOT NULL DEFAULT 'unknown', send_status TEXT NOT NULL DEFAULT 'unknown',
              last_used_at TEXT, last_error TEXT, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blacklist (
              telegram_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by INTEGER);
            CREATE TABLE IF NOT EXISTS stop_words (
              phrase TEXT PRIMARY KEY COLLATE NOCASE, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dialog_state (
              account_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, peer_username TEXT, peer_name TEXT,
              status TEXT NOT NULL, reason TEXT NOT NULL, last_outbound_at TEXT, checked_at TEXT NOT NULL,
              followup_sent_at TEXT, PRIMARY KEY(account_id, peer_id), FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS queue (
              id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, status TEXT NOT NULL,
              planned_at TEXT NOT NULL, reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(account_id, peer_id),
              FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, template TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0, max_per_account_per_day INTEGER NOT NULL DEFAULT 20,
              delay_seconds INTEGER NOT NULL DEFAULT 120, work_start_hour INTEGER NOT NULL DEFAULT 10,
              work_end_hour INTEGER NOT NULL DEFAULT 20, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, account_id INTEGER, peer_id INTEGER,
              details TEXT NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(queue)")}
            if "task_id" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN task_id INTEGER REFERENCES tasks(id)")
            self.set_default(db, "global_paused", "1")
            self.set_default(db, "delivery_enabled", "0")
            self.set_default(db, "followup_template", DEFAULT_FOLLOWUP)
            self.set_default(db, "task_max_per_account_per_day", "20")
            self.set_default(db, "task_delay_seconds", "120")
            self.set_default(db, "task_work_start_hour", "10")
            self.set_default(db, "task_work_end_hour", "20")
            self.set_default(db, "timezone", "Europe/Moscow")
            for phrase in DEFAULT_STOP_WORDS:
                db.execute("INSERT OR IGNORE INTO stop_words(phrase,created_at) VALUES(?,?)", (phrase, utcnow()))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def set_default(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def audit(self, kind: str, details: str, account_id: int | None = None, peer_id: int | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO audit_log(at,kind,account_id,peer_id,details) VALUES(?,?,?,?,?)", (utcnow(), kind, account_id, peer_id, details))

    def import_account(self, session_name: str, title: str | None, session_path: str) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO accounts(session_name,title,session_path,imported_at) VALUES(?,?,?,?)
                ON CONFLICT(session_name) DO UPDATE SET title=excluded.title,session_path=excluded.session_path""",
                (session_name, title or session_name, session_path, utcnow()))

    def accounts(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM accounts ORDER BY title,session_name").fetchall()

    def account(self, account_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def active_stop_words(self) -> list[str]:
        with self.connect() as db:
            return [row["phrase"] for row in db.execute("SELECT phrase FROM stop_words WHERE enabled=1 ORDER BY phrase")]

    def stop_words(self):
        with self.connect() as db:
            return db.execute("SELECT phrase,enabled FROM stop_words ORDER BY phrase").fetchall()

    def add_stop_word(self, phrase: str) -> None:
        phrase = " ".join(phrase.split())[:200]
        if not phrase:
            raise ValueError("Пустая фраза")
        with self.connect() as db:
            db.execute("INSERT INTO stop_words(phrase,enabled,created_at) VALUES(?,?,?) ON CONFLICT(phrase) DO UPDATE SET enabled=1", (phrase, 1, utcnow()))

    def remove_stop_word(self, phrase: str) -> bool:
        with self.connect() as db:
            result = db.execute("DELETE FROM stop_words WHERE phrase=? COLLATE NOCASE", (phrase,))
            return result.rowcount == 1

    def add_blacklist(self, peer_id: int, reason: str, created_by: int | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO blacklist(telegram_id,reason,created_at,created_by) VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET reason=excluded.reason", (peer_id, reason, utcnow(), created_by))

    def is_blacklisted(self, peer_id: int) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM blacklist WHERE telegram_id=?", (peer_id,)).fetchone() is not None

    def record_decision(self, account_id: int, peer_id: int, username: str | None, name: str | None, decision: Decision) -> None:
        with self.connect() as db:
            prior = db.execute("SELECT followup_sent_at FROM dialog_state WHERE account_id=? AND peer_id=?", (account_id, peer_id)).fetchone()
            sent_at = prior["followup_sent_at"] if prior else None
            db.execute("""INSERT INTO dialog_state(account_id,peer_id,peer_username,peer_name,status,reason,last_outbound_at,checked_at,followup_sent_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,peer_id) DO UPDATE SET
                peer_username=excluded.peer_username,peer_name=excluded.peer_name,status=excluded.status,reason=excluded.reason,
                last_outbound_at=excluded.last_outbound_at,checked_at=excluded.checked_at""",
                (account_id, peer_id, username, name, decision.status, decision.reason,
                 decision.relevant_outbound_at.isoformat() if decision.relevant_outbound_at else None, utcnow(), sent_at))
            if decision.status == DialogStatus.CANDIDATE and not sent_at:
                db.execute("""INSERT INTO queue(account_id,peer_id,status,planned_at,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(account_id,peer_id) DO UPDATE SET status=CASE WHEN queue.status IN ('sent','sending') THEN queue.status ELSE 'pending' END,
                  reason=excluded.reason,updated_at=excluded.updated_at""", (account_id, peer_id, "pending", utcnow(), decision.reason, utcnow(), utcnow()))
            elif decision.status != DialogStatus.CANDIDATE:
                db.execute("UPDATE queue SET status='cancelled',reason=?,updated_at=? WHERE account_id=? AND peer_id=? AND status IN ('pending','error')", (decision.reason, utcnow(), account_id, peer_id))

    def candidate_summary(self):
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) count FROM dialog_state GROUP BY status ORDER BY status").fetchall()
        return {row["status"]: row["count"] for row in rows}

    def queue_summary(self):
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) count FROM queue GROUP BY status ORDER BY status").fetchall()
        return {row["status"]: row["count"] for row in rows}

    def exception_summary(self):
        with self.connect() as db:
            blacklisted = db.execute("SELECT COUNT(*) count FROM blacklist").fetchone()["count"]
            rows = db.execute("SELECT status,COUNT(*) count FROM dialog_state WHERE status IN ('application','refusal','blacklisted') GROUP BY status").fetchall()
        result = {row["status"]: row["count"] for row in rows}
        result["blacklist_total"] = blacklisted
        return result

    def recent_logs(self, limit: int = 15):
        with self.connect() as db:
            return db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def pending(self, limit: int = 50):
        with self.connect() as db:
            return db.execute("""SELECT q.*,a.session_name,a.session_path,a.enabled,t.name task_name,t.template,
                 t.max_per_account_per_day,t.delay_seconds,t.work_start_hour,t.work_end_hour
                 FROM queue q JOIN accounts a ON a.id=q.account_id JOIN tasks t ON t.id=q.task_id
                 WHERE q.status='pending' AND a.enabled=1 AND t.enabled=1 ORDER BY q.planned_at LIMIT ?""", (limit,)).fetchall()

    def mark_queue(self, queue_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE queue SET status=?,last_error=?,attempts=attempts+?,updated_at=? WHERE id=?", (status, error, int(status == 'error'), utcnow(), queue_id))

    def mark_sent(self, queue_id: int, account_id: int, peer_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE queue SET status='sent',updated_at=? WHERE id=?", (utcnow(), queue_id))
            db.execute("UPDATE dialog_state SET status='followup_sent',reason='Дожим успешно отправлен',followup_sent_at=?,checked_at=? WHERE account_id=? AND peer_id=?", (utcnow(), utcnow(), account_id, peer_id))

    def sent_today(self, account_id: int, task_id: int | None = None) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) c FROM queue WHERE account_id=? AND status='sent' AND updated_at>=date('now') AND (? IS NULL OR task_id=?)", (account_id, task_id, task_id)).fetchone()["c"]

    def last_sent_at(self, account_id: int) -> datetime | None:
        with self.connect() as db:
            row = db.execute("SELECT MAX(updated_at) AS value FROM queue WHERE account_id=? AND status='sent'", (account_id,)).fetchone()
        return datetime.fromisoformat(row["value"]) if row and row["value"] else None

    def create_task(self, name: str, sample_limit: int, template: str | None = None) -> tuple[int, int]:
        """Snapshot a small global test sample (or full task) without later scans."""
        effective_template = template or self.setting("followup_template") or DEFAULT_FOLLOWUP
        max_per_day = int(self.setting("task_max_per_account_per_day", "20") or "20")
        delay = int(self.setting("task_delay_seconds", "120") or "120")
        work_start = int(self.setting("task_work_start_hour", "10") or "10")
        work_end = int(self.setting("task_work_end_hour", "20") or "20")
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO tasks(name,template,max_per_account_per_day,delay_seconds,work_start_hour,work_end_hour,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", (name.strip()[:100], effective_template, max_per_day, delay, work_start, work_end, utcnow(), utcnow()))
            task_id = cursor.lastrowid
            if sample_limit == 0:
                result = db.execute("UPDATE queue SET task_id=?,updated_at=? WHERE status='pending' AND task_id IS NULL", (task_id, utcnow()))
            else:
                result = db.execute("""UPDATE queue SET task_id=?,updated_at=? WHERE id IN (
                    SELECT id FROM queue WHERE status='pending' AND task_id IS NULL ORDER BY planned_at,id LIMIT ?
                )""", (task_id, utcnow(), sample_limit))
            return task_id, result.rowcount

    def tasks(self):
        with self.connect() as db:
            return db.execute("""SELECT t.*,COUNT(q.id) queued,
                SUM(CASE WHEN q.status='sent' THEN 1 ELSE 0 END) sent
                FROM tasks t LEFT JOIN queue q ON q.task_id=t.id GROUP BY t.id ORDER BY t.id DESC""").fetchall()

    def set_task_enabled(self, task_id: int, enabled: bool) -> bool:
        with self.connect() as db:
            result = db.execute("UPDATE tasks SET enabled=?,updated_at=? WHERE id=?", (int(enabled), utcnow(), task_id))
            return result.rowcount == 1
