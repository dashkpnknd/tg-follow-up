"""SQLite ledger. It is the source of idempotency, audit and emergency state."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from analyzer import DEFAULT_FOLLOWUP, DEFAULT_FOLLOWUP_VARIANTS, DEFAULT_STOP_WORDS, MIN_FOLLOWUP_AGE, Decision, DialogStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def checkin_project_key(value: str) -> str:
    """A forgiving comparison key for visible Telegram group titles."""
    return "".join(char for char in (value or "").casefold() if char.isalnum())


PROJECT_TITLE_NOISE = frozenset({
    "вк", "vk", "vkontakte", "вконтакте", "авито", "avito",
    "тг", "tg", "telegram", "телеграм", "target", "таргет",
})


def project_chat_tokens(value: str) -> tuple[str, ...]:
    """Return the meaningful words in a sheet project name or chat title."""
    value = re.sub(r"\bнаб\.?\s+челны\b", "набережные челны", (value or "").casefold())
    return tuple(
        token
        for token in re.findall(r"[a-zа-яё0-9]+", value)
        if token not in PROJECT_TITLE_NOISE
    )


def project_chat_key(value: str) -> str:
    """Comparison key for a sales-project name and its Telegram group title.

    Sales records usually mention one traffic channel (for example ``ВК``),
    while the working group can be titled ``ВК / Авито``. Channel labels are
    not part of the shop identity, so ignore their Russian and Latin spellings.
    """
    without_channel = re.sub(
        r"\b(?:вк|vk|авито|avito|тг|tg|telegram|телеграм)\b",
        " ",
        (value or "").casefold(),
    )
    return checkin_project_key(without_channel)


def project_chat_match_score(project_name: str, group_title: str) -> int:
    """Score a safe match between a spreadsheet project and a group title.

    Exact normalized names score highest. A project name may also be a
    complete, sufficiently specific prefix of a group title: this covers
    practical names such as ``Update Tula`` / ``Update Tula VK Target`` and
    ``King Store Ufa`` / ``King Store Avito Ufa Казань``. It does *not* use
    edit-distance guesses, so a typo or a different shop remains reviewable.
    """
    project_key = "".join(project_chat_tokens(project_name))
    group_key = "".join(project_chat_tokens(group_title))
    if not project_key or not group_key:
        return 0
    if project_key == group_key:
        return 100
    if len(project_key) >= 7 and project_key in group_key:
        return 90
    project_words = set(project_chat_tokens(project_name))
    group_words = set(project_chat_tokens(group_title))
    if len(project_words) >= 2 and project_words.issubset(group_words):
        return 80
    # A sheet may list several cities for one project. Match the full shop
    # name and the first explicitly listed city, never the shop alone.
    parts = (project_name or "").split("|", 1)
    if len(parts) == 2 and "," in parts[1]:
        shop = "".join(project_chat_tokens(parts[0]))
        city = "".join(project_chat_tokens(parts[1].split(",", 1)[0]))
        if len(shop) >= 4 and len(city) >= 4 and shop in group_key and city in group_key:
            return 70
    return 0


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
              last_used_at TEXT, last_error TEXT, cooldown_until TEXT, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blacklist (
              telegram_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by INTEGER);
            CREATE TABLE IF NOT EXISTS stop_words (
              phrase TEXT PRIMARY KEY COLLATE NOCASE, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dialog_state (
              account_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, peer_username TEXT, peer_name TEXT,
              status TEXT NOT NULL, reason TEXT NOT NULL, last_outbound_at TEXT, checked_at TEXT NOT NULL,
              followup_sent_at TEXT, source_message_id INTEGER, peer_access_hash INTEGER,
              PRIMARY KEY(account_id, peer_id), FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS queue (
              id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, status TEXT NOT NULL,
              planned_at TEXT NOT NULL, reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_template TEXT, validated_at TEXT,
              UNIQUE(account_id, peer_id),
              FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, template TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0, max_per_account_per_day INTEGER NOT NULL DEFAULT 20,
              delay_seconds INTEGER NOT NULL DEFAULT 600, max_delay_seconds INTEGER NOT NULL DEFAULT 900, work_start_hour INTEGER NOT NULL DEFAULT 10,
              work_end_hour INTEGER NOT NULL DEFAULT 20,
              auto_include_new INTEGER NOT NULL DEFAULT 1,
              templates_json TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, account_id INTEGER, peer_id INTEGER,
              details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cold_scan_progress (
              account_id INTEGER PRIMARY KEY,
              offset_date TEXT, offset_message_id INTEGER, offset_peer_id INTEGER,
              offset_peer_access_hash INTEGER, offset_peer_kind TEXT,
              completed_at TEXT, updated_at TEXT NOT NULL,
              FOREIGN KEY(account_id) REFERENCES accounts(id));
            -- The sales sheet remains read-only.  This is the bot's private
            -- ledger, used to prevent duplicate 14-day check-ins.
            CREATE TABLE IF NOT EXISTS customer_checkins (
              id INTEGER PRIMARY KEY,
              source_key TEXT UNIQUE NOT NULL,
              source_row INTEGER,
              sale_date TEXT NOT NULL,
              due_date TEXT NOT NULL,
              amount INTEGER NOT NULL,
              project_name TEXT NOT NULL,
              project_key TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new',
              reason TEXT NOT NULL DEFAULT '',
              account_id INTEGER,
              group_peer_id INTEGER,
              group_title TEXT,
              group_username TEXT,
              target_peer_id INTEGER,
              target_access_hash INTEGER,
              target_username TEXT,
              target_name TEXT,
              error_reported_at TEXT,
              sent_at TEXT,
              sent_message_id INTEGER,
              sent_template TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS customer_checkin_candidates (
              checkin_id INTEGER NOT NULL,
              peer_id INTEGER NOT NULL,
              peer_access_hash INTEGER,
              username TEXT,
              name TEXT,
              PRIMARY KEY(checkin_id,peer_id),
              FOREIGN KEY(checkin_id) REFERENCES customer_checkins(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS customer_checkin_group_candidates (
              checkin_id INTEGER NOT NULL,
              peer_id INTEGER NOT NULL,
              title TEXT,
              username TEXT,
              PRIMARY KEY(checkin_id,peer_id),
              FOREIGN KEY(checkin_id) REFERENCES customer_checkins(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS customer_checkin_project_blocks (
              project_key TEXT PRIMARY KEY,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by INTEGER);
            CREATE TABLE IF NOT EXISTS renewal_followups (
              id INTEGER PRIMARY KEY,
              source_key TEXT UNIQUE NOT NULL,
              sheet_name TEXT NOT NULL,
              row_number INTEGER NOT NULL,
              channel TEXT,
              project_name TEXT NOT NULL,
              project_key TEXT NOT NULL,
              amount INTEGER,
              trigger_kind TEXT NOT NULL,
              due_date TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new',
              reason TEXT NOT NULL DEFAULT '',
              account_id INTEGER,
              group_peer_id INTEGER,
              group_title TEXT,
              group_username TEXT,
              target_peer_id INTEGER,
              target_access_hash INTEGER,
              target_username TEXT,
              target_name TEXT,
              error_reported_at TEXT,
              sent_at TEXT,
              sent_message_id INTEGER,
              sent_template TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(account_id) REFERENCES accounts(id));
            CREATE TABLE IF NOT EXISTS renewal_followup_candidates (
              followup_id INTEGER NOT NULL,
              peer_id INTEGER NOT NULL,
              peer_access_hash INTEGER,
              username TEXT,
              name TEXT,
              PRIMARY KEY(followup_id,peer_id),
              FOREIGN KEY(followup_id) REFERENCES renewal_followups(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS renewal_followup_group_candidates (
              followup_id INTEGER NOT NULL,
              peer_id INTEGER NOT NULL,
              title TEXT,
              username TEXT,
              PRIMARY KEY(followup_id,peer_id),
              FOREIGN KEY(followup_id) REFERENCES renewal_followups(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS renewal_project_blocks (
              project_key TEXT PRIMARY KEY,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by INTEGER);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(queue)")}
            if "task_id" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN task_id INTEGER REFERENCES tasks(id)")
            if "sent_template" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN sent_template TEXT")
            if "validated_at" not in columns:
                db.execute("ALTER TABLE queue ADD COLUMN validated_at TEXT")
            state_columns = {row["name"] for row in db.execute("PRAGMA table_info(dialog_state)")}
            if "source_message_id" not in state_columns:
                db.execute("ALTER TABLE dialog_state ADD COLUMN source_message_id INTEGER")
            if "peer_access_hash" not in state_columns:
                db.execute("ALTER TABLE dialog_state ADD COLUMN peer_access_hash INTEGER")
            account_columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "cooldown_until" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN cooldown_until TEXT")
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "max_delay_seconds" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN max_delay_seconds INTEGER NOT NULL DEFAULT 900")
            if "auto_include_new" not in task_columns:
                # Existing tasks retain their current queue until explicitly
                # enabled; from that point new eligible dialogs join them too.
                db.execute("ALTER TABLE tasks ADD COLUMN auto_include_new INTEGER NOT NULL DEFAULT 1")
            if "templates_json" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN templates_json TEXT")
                for task in db.execute("SELECT id,template FROM tasks"):
                    db.execute("UPDATE tasks SET templates_json=? WHERE id=?", (json.dumps([task["template"]], ensure_ascii=False), task["id"]))
            checkin_columns = {row["name"] for row in db.execute("PRAGMA table_info(customer_checkins)")}
            if "sent_template" not in checkin_columns:
                db.execute("ALTER TABLE customer_checkins ADD COLUMN sent_template TEXT")
            if "group_username" not in checkin_columns:
                db.execute("ALTER TABLE customer_checkins ADD COLUMN group_username TEXT")
            renewal_columns = {row["name"] for row in db.execute("PRAGMA table_info(renewal_followups)")}
            for column in ("group_peer_id", "group_title", "group_username"):
                if column not in renewal_columns:
                    db.execute(f"ALTER TABLE renewal_followups ADD COLUMN {column} {'INTEGER' if column == 'group_peer_id' else 'TEXT'}")
            self.set_default(db, "global_paused", "1")
            self.set_default(db, "delivery_enabled", "0")
            self.set_default(db, "followup_template", DEFAULT_FOLLOWUP)
            self.set_default(db, "followup_templates", json.dumps(DEFAULT_FOLLOWUP_VARIANTS, ensure_ascii=False))
            self.set_default(db, "task_max_per_account_per_day", "20")
            self.set_default(db, "task_delay_seconds", "1800")
            self.set_default(db, "task_max_delay_seconds", "3600")
            self.set_default(db, "task_work_start_hour", "10")
            self.set_default(db, "task_work_end_hour", "20")
            self.set_default(db, "timezone", "Europe/Moscow")
            self.set_default(db, "auto_scan_enabled", "1")
            self.set_default(db, "revalidation_active", "0")
            self.set_default(db, "revalidation_resume_delivery", "1")
            self.set_default(db, "revalidation_resume_scan", "1")
            self.set_default(db, "cold_scan_in_progress", "0")
            self.set_default(db, "cold_initial_scan_complete", "0")
            self.set_default(db, "checkin_delivery_enabled", "0")
            self.set_default(db, "checkin_template", "")  # Compatibility with an earlier one-text setup.
            self.set_default(db, "checkin_templates", json.dumps([
                "Приветствую) Решил лично написать узнать, как продвигается работа по трафику?",
                "Приветствую) Решил лично написать узнать, справляется ли у вас отдел продаж с трафиком? В целом сама логика работы с таргетом понятна?",
                "Приветствую) Решил лично узнать, есть ли какие-то сложности в работе с трафиком?",
            ], ensure_ascii=False))
            self.set_default(db, "checkin_min_amount", "0")
            self.set_default(db, "checkin_max_per_day", "5")
            self.set_default(db, "checkin_delay_seconds", "2400")
            self.set_default(db, "checkin_max_delay_seconds", "3600")
            self.set_default(db, "checkin_work_start_hour", "8")
            self.set_default(db, "checkin_work_end_hour", "18")
            self.set_default(db, "renewal_delivery_enabled", "0")
            self.set_default(db, "renewal_white_day", "5")
            self.set_default(db, "renewal_white_grace_days", "7")
            self.set_default(db, "renewal_max_per_day", "5")
            self.set_default(db, "renewal_delay_seconds", "2400")
            self.set_default(db, "renewal_max_delay_seconds", "3600")
            self.set_default(db, "renewal_work_start_hour", "8")
            self.set_default(db, "renewal_work_end_hour", "18")
            self.set_default(db, "operator_cards_deferred", "0")
            self.set_default(db, "renewal_red_templates", json.dumps([
                "Доброго дня) Увидел, что вы взяли паузу в работе - решил лично написать, узнать, какие у вас сложности, если не секрет)",
                "Доброго дня) Увидел, что вы взяли паузу в работе - решил лично узнать, что вас не устроило в трафике, мне важно честно узнать корень проблемы, чтобы в дальнейшем искоренить это - если не секрет)",
                "Доброго дня) Увидел, что взяли паузу в работе\n\nПишу, тк моя цель, чтобы наша услуга была идеальна - можете, пожалуйста, откровенно сказать, что вам не понравилось, чтобы я понимал, над чем работать) Буду прям благодарен за честность",
                "Приветствую, увидел, что вы поставили крест на трафике)\nВам не трудно будет надиктовать обратную связь, что не устроило в нашей работе, тк я каждый день работаю над улучшением продукта и мне это важно.\n\nЗаранее спасибо)",
                "Доброго дня) Увидел, что вы взяли паузу в работе - решил лично написать, тк в целом у многих магазинов на данном этапе +- такие результаты и как у вас - хочется понять корень проблемы, чтобы в будущем это пофиксить",
            ], ensure_ascii=False))
            self.set_default(db, "renewal_white_templates", "[]")
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

    # Cold-outreach scan progress --------------------------------------
    # The dialog list can be very large.  A durable Telegram pagination cursor
    # advances every sender in small turns instead of making later accounts
    # wait for the first account's entire history.
    def prepare_cold_scan(self, account_ids: list[int]) -> None:
        if not account_ids:
            return
        with self.connect() as db:
            db.executemany(
                "INSERT INTO cold_scan_progress(account_id,updated_at) VALUES(?,?) ON CONFLICT(account_id) DO NOTHING",
                [(account_id, utcnow()) for account_id in account_ids],
            )

    def cold_scan_cursor(self, account_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM cold_scan_progress WHERE account_id=?", (account_id,)).fetchone()

    def save_cold_scan_cursor(
        self, account_id: int, *, offset_date: str | None, offset_message_id: int | None,
        offset_peer_id: int | None, offset_peer_access_hash: int | None, offset_peer_kind: str | None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO cold_scan_progress(account_id,offset_date,offset_message_id,offset_peer_id,
                       offset_peer_access_hash,offset_peer_kind,completed_at,updated_at)
                   VALUES(?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(account_id) DO UPDATE SET offset_date=excluded.offset_date,
                       offset_message_id=excluded.offset_message_id,offset_peer_id=excluded.offset_peer_id,
                       offset_peer_access_hash=excluded.offset_peer_access_hash,offset_peer_kind=excluded.offset_peer_kind,
                       completed_at=NULL,updated_at=excluded.updated_at""",
                (account_id, offset_date, offset_message_id, offset_peer_id,
                 offset_peer_access_hash, offset_peer_kind, utcnow()),
            )

    def complete_cold_scan(self, account_id: int) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE cold_scan_progress SET offset_date=NULL,offset_message_id=NULL,offset_peer_id=NULL,
                       offset_peer_access_hash=NULL,offset_peer_kind=NULL,completed_at=?,updated_at=? WHERE account_id=?""",
                (utcnow(), utcnow(), account_id),
            )

    def cold_scan_complete(self, account_ids: list[int]) -> bool:
        if not account_ids:
            return True
        placeholders = ",".join("?" for _ in account_ids)
        with self.connect() as db:
            remaining = db.execute(
                f"SELECT COUNT(*) count FROM cold_scan_progress WHERE account_id IN ({placeholders}) AND completed_at IS NULL",
                account_ids,
            ).fetchone()["count"]
        return int(remaining) == 0

    @staticmethod
    def _normalise_templates(values: list[str] | tuple[str, ...]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = " ".join(str(value).split())[:500]
            if text and text not in result:
                result.append(text)
        if not result:
            raise ValueError("Нужен хотя бы один текст дожима")
        return result

    @staticmethod
    def _normalise_renewal_templates(values: list[str] | tuple[str, ...]) -> list[str]:
        """Keep paragraph breaks in renewal messages while trimming stray spaces."""
        result: list[str] = []
        for value in values:
            lines = [line.strip() for line in str(value).strip().splitlines()]
            text = "\n".join(lines).strip()[:2_000]
            if text and text not in result:
                result.append(text)
        if not result:
            raise ValueError("Нужен хотя бы один текст для продления")
        return result

    def followup_templates(self) -> list[str]:
        raw = self.setting("followup_templates")
        try:
            values = json.loads(raw or "[]")
            if isinstance(values, list):
                return self._normalise_templates(values)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return [self.setting("followup_template", DEFAULT_FOLLOWUP) or DEFAULT_FOLLOWUP]

    def set_followup_templates(self, values: list[str]) -> None:
        templates = self._normalise_templates(values)
        self.set_setting("followup_templates", json.dumps(templates, ensure_ascii=False))
        self.set_setting("followup_template", templates[0])

    def checkin_templates(self) -> list[str]:
        """The approved customer check-in texts in a stable rotation order."""
        raw = self.setting("checkin_templates", "[]")
        try:
            values = json.loads(raw or "[]")
            if isinstance(values, list):
                return self._normalise_templates(values)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        legacy = (self.setting("checkin_template", "") or "").strip()
        return [legacy] if legacy else []

    def set_checkin_templates(self, values: list[str]) -> None:
        templates = self._normalise_templates(values)
        self.set_setting("checkin_templates", json.dumps(templates, ensure_ascii=False))
        self.set_setting("checkin_template", templates[0])

    def task_templates(self, task_id: int) -> list[str]:
        with self.connect() as db:
            row = db.execute("SELECT templates_json,template FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return []
        try:
            values = json.loads(row["templates_json"] or "[]")
            if isinstance(values, list):
                return self._normalise_templates(values)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return [row["template"]]

    def set_task_templates(self, task_id: int, values: list[str]) -> bool:
        templates = self._normalise_templates(values)
        with self.connect() as db:
            result = db.execute(
                "UPDATE tasks SET template=?,templates_json=?,updated_at=? WHERE id=?",
                (templates[0], json.dumps(templates, ensure_ascii=False), utcnow(), task_id),
            )
            return result.rowcount == 1

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

    def account_by_title(self, title: str):
        """Find one explicitly configured sender account, never guess it."""
        wanted = " ".join((title or "").casefold().split())
        if not wanted:
            return None
        with self.connect() as db:
            rows = db.execute("SELECT * FROM accounts WHERE enabled=1").fetchall()
        matches = [row for row in rows if " ".join((row["title"] or "").casefold().split()) == wanted]
        return matches[0] if len(matches) == 1 else None

    # Customer check-ins -------------------------------------------------
    # These records deliberately live only in the bot database.  The sales
    # sheet is an input and must not be changed or annotated by the bot.
    def upsert_customer_checkin(
        self,
        *,
        source_key: str,
        source_row: int,
        sale_date: str,
        due_date: str,
        amount: int,
        project_name: str,
    ) -> tuple[int, bool]:
        # A store can purchase VK and Avito together on the same day. Those
        # rows are one customer check-in, not two messages.
        key = project_chat_key(project_name)
        with self.connect() as db:
            row = db.execute("SELECT id FROM customer_checkins WHERE source_key=?", (source_key,)).fetchone()
            if row:
                db.execute(
                    "UPDATE customer_checkins SET source_row=?,sale_date=?,due_date=?,amount=?,project_name=?,project_key=?,updated_at=? WHERE id=?",
                    (source_row, sale_date, due_date, amount, project_name, key, utcnow(), row["id"]),
                )
                return int(row["id"]), False
            same_day = db.execute(
                "SELECT id FROM customer_checkins WHERE project_key=? AND sale_date=? AND status!='merged' ORDER BY id LIMIT 1",
                (key, sale_date),
            ).fetchone()
            if same_day:
                cursor = db.execute(
                    """INSERT INTO customer_checkins(source_key,source_row,sale_date,due_date,amount,project_name,project_key,status,reason,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_key, source_row, sale_date, due_date, amount, project_name, key, "merged",
                     f"Объединено с другой услугой этого магазина за {sale_date}", utcnow(), utcnow()),
                )
                return int(cursor.lastrowid), False
            if self._project_is_blocked(db, key):
                status, reason = "blocked", "Проект исключён оператором"
            else:
                status, reason = "new", "Ожидает поиск чата и контакта"
            cursor = db.execute(
                """INSERT INTO customer_checkins(source_key,source_row,sale_date,due_date,amount,project_name,project_key,status,reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (source_key, source_row, sale_date, due_date, amount, project_name, key, status, reason, utcnow(), utcnow()),
            )
            return int(cursor.lastrowid), True

    def merge_existing_same_day_checkins(self) -> int:
        """One-time-safe consolidation for channel rows already imported."""
        merged = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,sale_date,project_name,status,sent_at FROM customer_checkins ORDER BY sale_date,id"
            ).fetchall()
            masters: dict[tuple[str, str], int] = {}
            for row in rows:
                key = project_chat_key(row["project_name"])
                db.execute("UPDATE customer_checkins SET project_key=? WHERE id=?", (key, row["id"]))
                if row["status"] == "merged" or row["sent_at"]:
                    continue
                group = (key, row["sale_date"])
                if group not in masters:
                    masters[group] = int(row["id"])
                    continue
                db.execute(
                    "UPDATE customer_checkins SET status='merged',reason=?,error_reported_at=NULL,updated_at=? WHERE id=?",
                    (f"Объединено с другой услугой этого магазина за {row['sale_date']}", utcnow(), row["id"]),
                )
                merged += 1
        return merged

    @staticmethod
    def _project_is_blocked(db: sqlite3.Connection, project_key: str) -> bool:
        return bool(project_key and db.execute("SELECT 1 FROM customer_checkin_project_blocks WHERE project_key=?", (project_key,)).fetchone())

    def checkin(self, checkin_id: int):
        with self.connect() as db:
            return db.execute("""SELECT c.*,a.title AS account_title
                FROM customer_checkins c LEFT JOIN accounts a ON a.id=c.account_id WHERE c.id=?""", (checkin_id,)).fetchone()

    def checkins_to_resolve(self, limit: int = 20):
        with self.connect() as db:
            return db.execute(
                """SELECT * FROM customer_checkins
                   WHERE status='new'
                     OR (status='needs_review' AND reason LIKE 'Аккаунт отправителя недоступен:%')
                   ORDER BY CASE WHEN status='new' THEN 0 ELSE 1 END,due_date,id LIMIT ?""",
                (limit,),
            ).fetchall()

    def checkin_candidates(self, checkin_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM customer_checkin_candidates WHERE checkin_id=? ORDER BY name,username,peer_id", (checkin_id,)).fetchall()

    def checkin_group_candidates(self, checkin_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM customer_checkin_group_candidates WHERE checkin_id=? ORDER BY title,peer_id", (checkin_id,)).fetchall()

    def mark_checkin_review(
        self,
        checkin_id: int,
        reason: str,
        candidates: list[tuple[int, int | None, str | None, str | None]] | None = None,
        group_candidates: list[tuple[int, str | None, str | None]] | None = None,
        *,
        account_id: int | None = None,
        group_peer_id: int | None = None,
        group_title: str | None = None,
        group_username: str | None = None,
        preserve_reported: bool = False,
    ) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM customer_checkin_candidates WHERE checkin_id=?", (checkin_id,))
            db.execute("DELETE FROM customer_checkin_group_candidates WHERE checkin_id=?", (checkin_id,))
            db.executemany(
                "INSERT INTO customer_checkin_candidates(checkin_id,peer_id,peer_access_hash,username,name) VALUES(?,?,?,?,?)",
                [(checkin_id, peer_id, access_hash, username, name) for peer_id, access_hash, username, name in (candidates or [])],
            )
            db.executemany(
                "INSERT INTO customer_checkin_group_candidates(checkin_id,peer_id,title,username) VALUES(?,?,?,?)",
                [(checkin_id, peer_id, title, username) for peer_id, title, username in (group_candidates or [])],
            )
            db.execute(
                """UPDATE customer_checkins SET status='needs_review',reason=?,target_peer_id=NULL,target_access_hash=NULL,
                   target_username=NULL,target_name=NULL,account_id=COALESCE(?,account_id),
                   group_peer_id=COALESCE(?,group_peer_id),group_title=COALESCE(?,group_title),
                   group_username=COALESCE(?,group_username),
                   error_reported_at=CASE WHEN ? THEN error_reported_at ELSE NULL END,updated_at=? WHERE id=?""",
                (reason[:500], account_id, group_peer_id, group_title, group_username, int(preserve_reported), utcnow(), checkin_id),
            )

    def mark_checkin_ready(
        self,
        checkin_id: int,
        *,
        account_id: int,
        group_peer_id: int | None,
        group_title: str | None,
        peer_id: int,
        access_hash: int | None,
        username: str | None,
        name: str | None,
        reason: str = "Контакт найден автоматически",
        group_username: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE customer_checkins SET status='ready',reason=?,account_id=?,group_peer_id=?,group_title=?,group_username=?,
                   target_peer_id=?,target_access_hash=?,target_username=?,target_name=?,error_reported_at=NULL,updated_at=? WHERE id=?
                """,
                (reason[:500], account_id, group_peer_id, group_title, group_username, peer_id, access_hash, username, name, utcnow(), checkin_id),
            )

    def mark_checkin_error(self, checkin_id: int, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE customer_checkins SET status='error',reason=?,error_reported_at=NULL,updated_at=? WHERE id=?",
                (reason[:500], utcnow(), checkin_id),
            )

    def reset_checkin_exception_reports(self) -> int:
        with self.connect() as db:
            return db.execute("UPDATE customer_checkins SET error_reported_at=NULL WHERE status IN ('needs_review','error')").rowcount

    def mark_checkin_skipped(self, checkin_id: int, reason: str = "Пропущено оператором") -> None:
        with self.connect() as db:
            db.execute("UPDATE customer_checkins SET status='skipped',reason=?,updated_at=? WHERE id=?", (reason[:500], utcnow(), checkin_id))

    def block_checkin_project(self, checkin_id: int, created_by: int | None = None) -> None:
        with self.connect() as db:
            row = db.execute("SELECT project_key FROM customer_checkins WHERE id=?", (checkin_id,)).fetchone()
            if not row:
                return
            db.execute(
                "INSERT INTO customer_checkin_project_blocks(project_key,reason,created_at,created_by) VALUES(?,?,?,?) ON CONFLICT(project_key) DO UPDATE SET reason=excluded.reason,created_by=excluded.created_by",
                (row["project_key"], "Не писать больше: решение оператора", utcnow(), created_by),
            )
            db.execute("UPDATE customer_checkins SET status='blocked',reason=?,updated_at=? WHERE id=?", ("Проект исключён оператором", utcnow(), checkin_id))

    def mark_checkin_reported(self, checkin_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE customer_checkins SET error_reported_at=?,updated_at=? WHERE id=?", (utcnow(), utcnow(), checkin_id))

    def checkin_exceptions_to_report(self, limit: int = 20):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM customer_checkins WHERE status IN ('needs_review','error') AND error_reported_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()

    def pending_checkin_delivery(self, due_on_or_before: str, limit: int = 20):
        """Return only check-ins whose 14-day date has actually arrived."""
        with self.connect() as db:
            return db.execute(
                """SELECT c.*,a.session_path,a.enabled,a.auth_status,a.send_status
                   FROM customer_checkins c JOIN accounts a ON a.id=c.account_id
                   WHERE c.status='ready' AND c.due_date<=?
                     AND a.enabled=1 AND a.auth_status='authorized' AND a.send_status!='unavailable'
                   ORDER BY c.due_date,c.id LIMIT ?""",
                (due_on_or_before, limit),
            ).fetchall()

    def mark_checkin_sent(self, checkin_id: int, message_id: int, template: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE customer_checkins SET status='sent',reason=?,sent_at=?,sent_message_id=?,sent_template=?,updated_at=? WHERE id=?",
                ("Сообщение отправлено", utcnow(), message_id, template, utcnow(), checkin_id),
            )

    def next_checkin_template(self) -> str | None:
        """Round-robin from saved sends rather than random selection."""
        templates = self.checkin_templates()
        if not templates:
            return None
        with self.connect() as db:
            sent_count = int(db.execute("SELECT COUNT(*) count FROM customer_checkins WHERE status='sent'").fetchone()["count"])
        return templates[sent_count % len(templates)]

    def checkin_summary(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) count FROM customer_checkins GROUP BY status ORDER BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def checkin_sent_today(self, timezone_name: str = "Europe/Moscow") -> int:
        local_now = datetime.now(ZoneInfo(timezone_name))
        start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) count FROM customer_checkins WHERE status='sent' AND sent_at>=?", (start_utc,)).fetchone()["count"])

    def last_checkin_sent_at(self) -> datetime | None:
        with self.connect() as db:
            row = db.execute("SELECT MAX(sent_at) value FROM customer_checkins WHERE status='sent'").fetchone()
        return datetime.fromisoformat(row["value"]) if row and row["value"] else None

    # Renewal follow-ups -------------------------------------------------
    # The renewal sheet is read-only input.  Color changes, operator choices
    # and delivery state remain in this local ledger.
    def renewal_templates(self, trigger_kind: str) -> list[str]:
        if trigger_kind not in {"red", "white"}:
            raise ValueError("Неизвестный тип продления")
        try:
            values = json.loads(self.setting(f"renewal_{trigger_kind}_templates", "[]") or "[]")
            return self._normalise_renewal_templates(values) if isinstance(values, list) else []
        except (ValueError, TypeError, json.JSONDecodeError):
            return []

    def set_renewal_templates(self, trigger_kind: str, values: list[str]) -> None:
        if trigger_kind not in {"red", "white"}:
            raise ValueError("Неизвестный тип продления")
        self.set_setting(f"renewal_{trigger_kind}_templates", json.dumps(self._normalise_renewal_templates(values), ensure_ascii=False))

    def upsert_renewal_followup(
        self,
        *,
        source_key: str,
        sheet_name: str,
        row_number: int,
        channel: str,
        project_name: str,
        amount: int | None,
        trigger_kind: str,
        due_date: str,
        today: str,
    ) -> tuple[int, bool]:
        if trigger_kind not in {"red", "white"}:
            raise ValueError("Неизвестный тип продления")
        project_key = project_chat_key(project_name)
        with self.connect() as db:
            existing = db.execute("SELECT id,status FROM renewal_followups WHERE source_key=?", (source_key,)).fetchone()
            if existing:
                if existing["status"] not in {"sent", "skipped", "blocked"}:
                    db.execute(
                        "UPDATE renewal_followups SET sheet_name=?,row_number=?,channel=?,project_name=?,project_key=?,amount=?,trigger_kind=?,due_date=?,updated_at=? WHERE id=?",
                        (sheet_name, row_number, channel, project_name, project_key, amount, trigger_kind, due_date, utcnow(), existing["id"]),
                    )
                    if trigger_kind == "red" and existing["status"] in {"waiting", "expired"}:
                        db.execute("UPDATE renewal_followups SET status='new',reason='Строка стала красной',updated_at=? WHERE id=?", (utcnow(), existing["id"]))
                return int(existing["id"]), False
            same_project = db.execute(
                "SELECT id,status,trigger_kind FROM renewal_followups WHERE sheet_name=? AND project_key=? AND status!='merged' ORDER BY id LIMIT 1",
                (sheet_name, project_key),
            ).fetchone()
            if same_project:
                # VK and Avito rows for one shop represent one customer.  A
                # red state wins over a waiting white state for that customer.
                if trigger_kind == "red" and same_project["trigger_kind"] != "red":
                    db.execute(
                        "UPDATE renewal_followups SET row_number=?,channel=?,project_name=?,amount=?,trigger_kind='red',due_date=?,status='new',reason='Строка стала красной',updated_at=? WHERE id=?",
                        (row_number, channel, project_name, amount, due_date, utcnow(), same_project["id"]),
                    )
                cursor = db.execute(
                    """INSERT INTO renewal_followups(source_key,sheet_name,row_number,channel,project_name,project_key,amount,trigger_kind,due_date,status,reason,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_key, sheet_name, row_number, channel, project_name, project_key, amount, trigger_kind, due_date,
                     "merged", f"Объединено с другой услугой этого магазина на листе «{sheet_name}»", utcnow(), utcnow()),
                )
                return int(cursor.lastrowid), False
            if self._renewal_project_is_blocked(db, project_key):
                status, reason = "blocked", "Проект исключён оператором"
            elif trigger_kind == "white" and due_date > today:
                status, reason = "waiting", "Ожидает дату проверки белой строки"
            elif trigger_kind == "white" and due_date < today:
                grace = int(self.setting("renewal_white_grace_days", "7") or "7")
                age = (datetime.fromisoformat(today).date() - datetime.fromisoformat(due_date).date()).days
                status, reason = ("expired", "Срок белой строки уже прошёл") if age > grace else ("new", "Белая строка ожидает проверку")
            else:
                status, reason = "new", "Красная строка найдена"
            cursor = db.execute(
                """INSERT INTO renewal_followups(source_key,sheet_name,row_number,channel,project_name,project_key,amount,trigger_kind,due_date,status,reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_key, sheet_name, row_number, channel, project_name, project_key, amount, trigger_kind, due_date, status, reason, utcnow(), utcnow()),
            )
            return int(cursor.lastrowid), True

    @staticmethod
    def _renewal_project_is_blocked(db: sqlite3.Connection, project_key: str) -> bool:
        return bool(project_key and db.execute("SELECT 1 FROM renewal_project_blocks WHERE project_key=?", (project_key,)).fetchone())

    def reconcile_renewal_colors(self, today: str, current_red_keys: set[str], current_white_keys: set[str]) -> None:
        """Suspend unsent work when its source no longer has the required color."""
        with self.connect() as db:
            rows = db.execute("SELECT id,status,trigger_kind,source_key,due_date FROM renewal_followups WHERE source_key NOT LIKE 'unified:%' AND status IN ('new','ready','needs_review','error','waiting','inactive')").fetchall()
            for row in rows:
                valid = row["source_key"] in (current_red_keys if row["trigger_kind"] == "red" else current_white_keys)
                if not valid and row["status"] != "inactive":
                    db.execute("UPDATE renewal_followups SET status='inactive',reason=?,updated_at=? WHERE id=?", ("Строка больше не имеет нужного цвета или отсутствует", utcnow(), row["id"]))
                elif valid and row["status"] == "inactive":
                    status = "waiting" if row["trigger_kind"] == "white" and row["due_date"] > today else "new"
                    db.execute("UPDATE renewal_followups SET status=?,reason=?,updated_at=? WHERE id=?", (status, "Строка снова подходит для кампании", utcnow(), row["id"]))

    def activate_due_white_renewals(self, today: str, current_white_keys: set[str] | None = None) -> int:
        """Activate due white rows without reviving stale waiting cases.

        A row may have been waiting in the local ledger while the service was
        stopped.  Apply the same grace window here as when a row is first
        discovered; otherwise a months-old white row could be sent merely
        because the process came back online.
        """
        today_date = datetime.fromisoformat(today).date()
        grace = int(self.setting("renewal_white_grace_days", "7") or "7")
        activated = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,due_date,source_key FROM renewal_followups WHERE status='waiting' AND due_date<=?",
                (today,),
            ).fetchall()
            for row in rows:
                if current_white_keys is not None and row["source_key"] not in current_white_keys:
                    continue
                age = (today_date - datetime.fromisoformat(row["due_date"]).date()).days
                if age > grace:
                    db.execute(
                        "UPDATE renewal_followups SET status='expired',reason='Срок белой строки уже прошёл',updated_at=? WHERE id=?",
                        (utcnow(), row["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE renewal_followups SET status='new',reason='Наступила дата проверки белой строки',updated_at=? WHERE id=?",
                        (utcnow(), row["id"]),
                    )
                    activated += 1
        return activated

    def renewal_followup(self, followup_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM renewal_followups WHERE id=?", (followup_id,)).fetchone()

    def renewal_followups_to_resolve(self, limit: int = 20):
        with self.connect() as db:
            return db.execute(
                """SELECT * FROM renewal_followups
                   WHERE source_key NOT LIKE 'unified:%' AND (status='new'
                     OR (status='needs_review' AND reason LIKE 'Аккаунт отправителя недоступен:%'))
                   ORDER BY CASE WHEN status='new' THEN 0 ELSE 1 END,due_date,id LIMIT ?""",
                (limit,),
            ).fetchall()

    def renewal_candidates(self, followup_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM renewal_followup_candidates WHERE followup_id=? ORDER BY name,username,peer_id", (followup_id,)).fetchall()

    def renewal_group_candidates(self, followup_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM renewal_followup_group_candidates WHERE followup_id=? ORDER BY title,peer_id", (followup_id,)).fetchall()

    def mark_renewal_review(
        self,
        followup_id: int,
        reason: str,
        candidates: list[tuple[int, int | None, str | None, str | None]] | None = None,
        account_id: int | None = None,
        group_peer_id: int | None = None,
        group_title: str | None = None,
        group_username: str | None = None,
        group_candidates: list[tuple[int, str | None, str | None]] | None = None,
        *,
        preserve_reported: bool = False,
    ) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM renewal_followup_candidates WHERE followup_id=?", (followup_id,))
            db.execute("DELETE FROM renewal_followup_group_candidates WHERE followup_id=?", (followup_id,))
            db.executemany(
                "INSERT INTO renewal_followup_candidates(followup_id,peer_id,peer_access_hash,username,name) VALUES(?,?,?,?,?)",
                [(followup_id, peer_id, access_hash, username, name) for peer_id, access_hash, username, name in (candidates or [])],
            )
            db.executemany(
                "INSERT INTO renewal_followup_group_candidates(followup_id,peer_id,title,username) VALUES(?,?,?,?)",
                [(followup_id, peer_id, title, username) for peer_id, title, username in (group_candidates or [])],
            )
            db.execute(
                """UPDATE renewal_followups SET status='needs_review',reason=?,target_peer_id=NULL,target_access_hash=NULL,target_username=NULL,target_name=NULL,
                   account_id=COALESCE(?,account_id),group_peer_id=?,group_title=?,group_username=?,error_reported_at=CASE WHEN ? THEN error_reported_at ELSE NULL END,updated_at=? WHERE id=?""",
                (reason[:500], account_id, group_peer_id, group_title, group_username, int(preserve_reported), utcnow(), followup_id),
            )

    def mark_renewal_ready(
        self,
        followup_id: int,
        *,
        account_id: int,
        peer_id: int,
        access_hash: int | None,
        username: str | None,
        name: str | None,
        reason: str,
        group_peer_id: int | None = None,
        group_title: str | None = None,
        group_username: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE renewal_followups SET status='ready',reason=?,account_id=?,group_peer_id=COALESCE(?,group_peer_id),group_title=COALESCE(?,group_title),group_username=COALESCE(?,group_username),
                   target_peer_id=?,target_access_hash=?,target_username=?,target_name=?,error_reported_at=NULL,updated_at=? WHERE id=?""",
                (reason[:500], account_id, group_peer_id, group_title, group_username, peer_id, access_hash, username, name, utcnow(), followup_id),
            )

    def mark_renewal_error(self, followup_id: int, reason: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE renewal_followups SET status='error',reason=?,error_reported_at=NULL,updated_at=? WHERE id=?", (reason[:500], utcnow(), followup_id))

    def reset_renewal_exception_reports(self) -> int:
        with self.connect() as db:
            return db.execute("UPDATE renewal_followups SET error_reported_at=NULL WHERE status IN ('needs_review','error')").rowcount

    def mark_renewal_skipped(self, followup_id: int, reason: str = "Пропущено оператором") -> None:
        with self.connect() as db:
            db.execute("UPDATE renewal_followups SET status='skipped',reason=?,updated_at=? WHERE id=?", (reason[:500], utcnow(), followup_id))

    def block_renewal_project(self, followup_id: int, created_by: int | None = None) -> None:
        with self.connect() as db:
            row = db.execute("SELECT project_key FROM renewal_followups WHERE id=?", (followup_id,)).fetchone()
            if not row:
                return
            db.execute("INSERT INTO renewal_project_blocks(project_key,reason,created_at,created_by) VALUES(?,?,?,?) ON CONFLICT(project_key) DO UPDATE SET reason=excluded.reason,created_by=excluded.created_by", (row["project_key"], "Не писать больше: решение оператора", utcnow(), created_by))
            db.execute("UPDATE renewal_followups SET status='blocked',reason=?,updated_at=? WHERE id=?", ("Проект исключён оператором", utcnow(), followup_id))

    def mark_renewal_reported(self, followup_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE renewal_followups SET error_reported_at=?,updated_at=? WHERE id=?", (utcnow(), utcnow(), followup_id))

    def renewal_exceptions_to_report(self, limit: int = 20):
        with self.connect() as db:
            return db.execute("SELECT * FROM renewal_followups WHERE status IN ('needs_review','error') AND error_reported_at IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()

    def pending_renewal_delivery(self, limit: int = 20):
        with self.connect() as db:
            return db.execute("""SELECT r.*,a.session_path,a.enabled,a.auth_status,a.send_status
                FROM renewal_followups r JOIN accounts a ON a.id=r.account_id
                WHERE r.status='ready' AND r.source_key NOT LIKE 'unified:%' AND a.enabled=1 AND a.auth_status='authorized' AND a.send_status!='unavailable'
                ORDER BY r.due_date,r.id LIMIT ?""", (limit,)).fetchall()

    def mark_renewal_sent(self, followup_id: int, message_id: int, template: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE renewal_followups SET status='sent',reason=?,sent_at=?,sent_message_id=?,sent_template=?,updated_at=? WHERE id=?", ("Сообщение отправлено", utcnow(), message_id, template, utcnow(), followup_id))

    def next_renewal_template(self, trigger_kind: str) -> str | None:
        templates = self.renewal_templates(trigger_kind)
        if not templates:
            return None
        with self.connect() as db:
            count = int(db.execute("SELECT COUNT(*) count FROM renewal_followups WHERE status='sent' AND trigger_kind=?", (trigger_kind,)).fetchone()["count"])
        return templates[count % len(templates)]

    def renewal_summary(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) count FROM renewal_followups GROUP BY status ORDER BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def renewal_sent_today(self, timezone_name: str = "Europe/Moscow") -> int:
        local_now = datetime.now(ZoneInfo(timezone_name))
        since = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) count FROM renewal_followups WHERE status='sent' AND sent_at>=?", (since,)).fetchone()["count"])

    def last_renewal_sent_at(self) -> datetime | None:
        with self.connect() as db:
            row = db.execute("SELECT MAX(sent_at) value FROM renewal_followups WHERE status='sent'").fetchone()
        return datetime.fromisoformat(row["value"]) if row and row["value"] else None

    def set_account_auth_status(self, account_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE accounts SET auth_status=?,last_error=COALESCE(?,last_error) WHERE id=?",
                (status, error[:500] if error else None, account_id),
            )

    def restore_authorized_cold_accounts(self, session_names: list[str]) -> list[int]:
        """Restore senders incorrectly retired by the old broad error handler.

        Only a session which is *currently authorized* is restored.  A truly
        logged-out account remains unavailable; recipient errors and temporary
        Telegram limits never permanently retire a sender.
        """
        names = [name for name in session_names if name]
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT id FROM accounts
                    WHERE session_name IN ({placeholders})
                      AND enabled=1 AND auth_status='authorized'
                      AND send_status='unavailable'
                      AND COALESCE(last_error,'') NOT LIKE 'auth_error%'""",
                names,
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                id_placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE accounts SET send_status='available',cooldown_until=NULL,last_error=NULL WHERE id IN ({id_placeholders})",
                    ids,
                )
        return ids

    def requeue_candidate_dialogs(self, account_ids: list[int]) -> int:
        """Put only previously classified candidates back into safety audit.

        This is deliberately narrower than reviving an old queue wholesale:
        refusals, applications, replies and everyone already messaged remain
        untouched.  Every revived row receives a new full-history preflight
        before it can be delivered.
        """
        if not account_ids:
            return 0
        placeholders = ",".join("?" for _ in account_ids)
        with self.connect() as db:
            result = db.execute(
                f"""UPDATE queue AS q
                    SET status='pending', validated_at=NULL, last_error=NULL,
                        reason='Возобновлено после проверки доступности аккаунта', updated_at=?
                    WHERE q.account_id IN ({placeholders})
                      AND q.status IN ('cancelled','error')
                      AND EXISTS (
                        SELECT 1 FROM dialog_state ds
                        WHERE ds.account_id=q.account_id AND ds.peer_id=q.peer_id
                          AND ds.status='candidate'
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM queue sent
                        WHERE sent.peer_id=q.peer_id AND sent.status='sent'
                      )""",
                (utcnow(), *account_ids),
            )
            return int(result.rowcount)

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

    def dialog_source_message_id(self, account_id: int, peer_id: int) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT source_message_id FROM dialog_state WHERE account_id=? AND peer_id=?", (account_id, peer_id)).fetchone()
        return int(row["source_message_id"]) if row and row["source_message_id"] else None

    def update_dialog_access_hash(self, account_id: int, peer_id: int, access_hash: int | None) -> None:
        if access_hash is None:
            return
        with self.connect() as db:
            db.execute(
                "UPDATE dialog_state SET peer_access_hash=COALESCE(peer_access_hash,?) WHERE account_id=? AND peer_id=?",
                (access_hash, account_id, peer_id),
            )

    def dialog_requires_recheck(self, account_id: int, peer_id: int) -> bool:
        """Return true when a quiet dialog can become eligible without a new message.

        A chat whose last outbound message is less than 48 hours old has the
        same latest-message id when it crosses the 48-hour threshold.  It must
        therefore not be skipped by the incremental scanner.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT status,last_outbound_at FROM dialog_state WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            ).fetchone()
        if not row or row["status"] != DialogStatus.TOO_FRESH or not row["last_outbound_at"]:
            return False
        return datetime.fromisoformat(row["last_outbound_at"]) + MIN_FOLLOWUP_AGE <= datetime.now(timezone.utc)

    @staticmethod
    def _active_auto_task_id(db: sqlite3.Connection) -> int | None:
        row = db.execute(
            "SELECT id FROM tasks WHERE enabled=1 AND auto_include_new=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def record_decision(self, account_id: int, peer_id: int, username: str | None, name: str | None, decision: Decision, source_message_id: int | None = None, peer_access_hash: int | None = None) -> None:
        with self.connect() as db:
            prior = db.execute("SELECT followup_sent_at FROM dialog_state WHERE account_id=? AND peer_id=?", (account_id, peer_id)).fetchone()
            sent_at = prior["followup_sent_at"] if prior else None
            db.execute("""INSERT INTO dialog_state(account_id,peer_id,peer_username,peer_name,status,reason,last_outbound_at,checked_at,followup_sent_at,source_message_id,peer_access_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,peer_id) DO UPDATE SET
                peer_username=excluded.peer_username,peer_name=excluded.peer_name,status=excluded.status,reason=excluded.reason,
                last_outbound_at=excluded.last_outbound_at,checked_at=excluded.checked_at,source_message_id=excluded.source_message_id,
                peer_access_hash=COALESCE(excluded.peer_access_hash,dialog_state.peer_access_hash)""",
                (account_id, peer_id, username, name, decision.status, decision.reason,
                 decision.relevant_outbound_at.isoformat() if decision.relevant_outbound_at else None, utcnow(), sent_at, source_message_id, peer_access_hash))
            if decision.status == DialogStatus.CANDIDATE and not sent_at:
                active_task_id = self._active_auto_task_id(db)
                db.execute("""INSERT INTO queue(account_id,peer_id,status,planned_at,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(account_id,peer_id) DO UPDATE SET status=CASE WHEN queue.status IN ('sent','sending') THEN queue.status ELSE 'pending' END,
                  reason=excluded.reason,validated_at=queue.validated_at,
                  updated_at=excluded.updated_at""", (account_id, peer_id, "pending", utcnow(), decision.reason, utcnow(), utcnow()))
                # Do not move a candidate that was deliberately put into a
                # different task.  Only unassigned, newly discovered dialogs
                # join the currently enabled automatic task.
                if active_task_id is not None:
                    db.execute(
                        "UPDATE queue SET task_id=?,updated_at=? WHERE account_id=? AND peer_id=? AND task_id IS NULL AND status='pending'",
                        (active_task_id, utcnow(), account_id, peer_id),
                    )
            elif decision.status != DialogStatus.CANDIDATE:
                db.execute("UPDATE queue SET status='cancelled',reason=?,updated_at=? WHERE account_id=? AND peer_id=? AND status IN ('pending','error')", (decision.reason, utcnow(), account_id, peer_id))

    def cold_followup_sent_to_peer(self, peer_id: int) -> bool:
        """Whether this person has already received a cold follow-up.

        A person can be visible from more than one mailing account.  The
        campaign is per person, not per account, so a prior send from any
        account suppresses every other copy of that dialog.
        """
        with self.connect() as db:
            return bool(db.execute(
                "SELECT 1 FROM queue WHERE peer_id=? AND status='sent' LIMIT 1",
                (peer_id,),
            ).fetchone())

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
        now = utcnow()
        with self.connect() as db:
            # Consider the head of each account's queue, rather than the first
            # N rows globally.  Without this, a large first account could hide
            # all other accounts behind its own 30–60 minute cooldown.
            return db.execute("""SELECT q.*,a.session_name,a.session_path,a.enabled,ds.peer_access_hash,t.name task_name,t.template,t.templates_json,
                 t.max_per_account_per_day,t.delay_seconds,t.max_delay_seconds,t.work_start_hour,t.work_end_hour
                 FROM queue q JOIN accounts a ON a.id=q.account_id JOIN tasks t ON t.id=q.task_id
                 LEFT JOIN dialog_state ds ON ds.account_id=q.account_id AND ds.peer_id=q.peer_id
                 WHERE q.status='pending' AND q.validated_at IS NOT NULL AND a.enabled=1 AND a.auth_status='authorized' AND a.send_status!='unavailable'
                   AND (a.cooldown_until IS NULL OR a.cooldown_until<=?) AND t.enabled=1
                 AND q.id IN (
                   SELECT MIN(q2.id) FROM queue q2
                   JOIN accounts a2 ON a2.id=q2.account_id
                   JOIN tasks t2 ON t2.id=q2.task_id
                   WHERE q2.status='pending' AND q2.validated_at IS NOT NULL AND a2.enabled=1 AND a2.auth_status='authorized' AND a2.send_status!='unavailable'
                     AND (a2.cooldown_until IS NULL OR a2.cooldown_until<=?) AND t2.enabled=1
                   GROUP BY q2.account_id
                 )
                 ORDER BY q.planned_at,q.id LIMIT ?""", (now, now, limit)).fetchall()

    def mark_queue(self, queue_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE queue SET status=?,last_error=?,attempts=attempts+?,updated_at=? WHERE id=?", (status, error, int(status == 'error'), utcnow(), queue_id))

    def pending_revalidation(self, limit: int = 1):
        """Rows not yet checked against their complete conversation history."""
        with self.connect() as db:
            return db.execute("""SELECT q.*,a.session_name,a.session_path,a.enabled,ds.peer_access_hash,t.name task_name,t.template,t.templates_json,
                 t.max_per_account_per_day,t.delay_seconds,t.max_delay_seconds,t.work_start_hour,t.work_end_hour
                 FROM queue q JOIN accounts a ON a.id=q.account_id JOIN tasks t ON t.id=q.task_id
                 LEFT JOIN dialog_state ds ON ds.account_id=q.account_id AND ds.peer_id=q.peer_id
                 WHERE q.status='pending' AND q.validated_at IS NULL AND a.enabled=1
                 AND a.auth_status='authorized' AND a.send_status!='unavailable' AND t.enabled=1
                 ORDER BY q.id LIMIT ?""", (limit,)).fetchall()

    def has_unvalidated_cold_candidates(self) -> bool:
        with self.connect() as db:
            return bool(db.execute(
                "SELECT 1 FROM queue WHERE status='pending' AND validated_at IS NULL LIMIT 1"
            ).fetchone())

    def mark_validated(self, queue_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE queue SET validated_at=?,updated_at=? WHERE id=?", (utcnow(), utcnow(), queue_id))

    def revalidation_summary(self) -> dict[str, int]:
        with self.connect() as db:
            row = db.execute("""SELECT
                SUM(CASE WHEN status='pending' AND validated_at IS NULL THEN 1 ELSE 0 END) remaining,
                SUM(CASE WHEN status='pending' AND validated_at IS NOT NULL THEN 1 ELSE 0 END) safe,
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) excluded
                FROM queue""").fetchone()
        return {key: int(row[key] or 0) for key in ("remaining", "safe", "excluded")}

    def mark_sent(self, queue_id: int, account_id: int, peer_id: int, template: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE queue SET status='sent',sent_template=?,updated_at=? WHERE id=?", (template, utcnow(), queue_id))
            db.execute("UPDATE dialog_state SET status='followup_sent',reason='Дожим успешно отправлен',followup_sent_at=?,checked_at=? WHERE account_id=? AND peer_id=?", (utcnow(), utcnow(), account_id, peer_id))


    def disable_sending_for_account(self, account_id: int, reason: str) -> int:
        """Stop delivery from one unavailable account without stopping the task.

        A restricted or banned Telegram account is not retried automatically:
        retrying it adds risk but cannot help the remaining healthy accounts.
        """
        with self.connect() as db:
            db.execute(
                "UPDATE accounts SET send_status='unavailable',cooldown_until=NULL,last_error=? WHERE id=?",
                (reason[:500], account_id),
            )
            result = db.execute(
                "UPDATE queue SET status='cancelled',reason=?,updated_at=? WHERE account_id=? AND status IN ('pending','error','sending')",
                (f"Аккаунт недоступен для отправки: {reason[:300]}", utcnow(), account_id),
            )
            return result.rowcount

    def cooldown_sending_for_account(self, account_id: int, reason: str, hours: int = 24) -> str:
        """Pause a rate-limited sender while preserving its untouched queue."""
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE accounts SET send_status='available',cooldown_until=?,last_error=? WHERE id=?",
                (until, reason[:500], account_id),
            )
            db.execute(
                "UPDATE queue SET status='pending',last_error=?,updated_at=? WHERE account_id=? AND status='sending'",
                (reason[:500], utcnow(), account_id),
            )
        return until

    def sent_today(self, account_id: int, task_id: int | None = None) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) c FROM queue WHERE account_id=? AND status='sent' AND updated_at>=date('now') AND (? IS NULL OR task_id=?)", (account_id, task_id, task_id)).fetchone()["c"]

    def sent_today_total(self, timezone_name: str = "Europe/Moscow") -> int:
        """Count all sends since the start of the current local calendar day."""
        local_now = datetime.now(ZoneInfo(timezone_name))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_midnight.astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM queue WHERE status='sent' AND updated_at>=?",
                (start_utc,),
            ).fetchone()["c"]

    def latest_sent(self):
        with self.connect() as db:
            return db.execute("""SELECT q.sent_template,a.title FROM queue q
                JOIN accounts a ON a.id=q.account_id WHERE q.status='sent'
                ORDER BY q.updated_at DESC,q.id DESC LIMIT 1""").fetchone()

    def last_sent_at(self, account_id: int) -> datetime | None:
        with self.connect() as db:
            row = db.execute("SELECT MAX(updated_at) AS value FROM queue WHERE account_id=? AND status='sent'", (account_id,)).fetchone()
        return datetime.fromisoformat(row["value"]) if row and row["value"] else None

    def create_task(self, name: str, sample_limit: int, template: str | None = None) -> tuple[int, int]:
        """Create a task from the current pool; enabled tasks also accept new candidates."""
        templates = [template] if template else self.followup_templates()
        effective_template = templates[0]
        max_per_day = int(self.setting("task_max_per_account_per_day", "20") or "20")
        delay = int(self.setting("task_delay_seconds", "600") or "600")
        max_delay = int(self.setting("task_max_delay_seconds", "900") or "900")
        work_start = int(self.setting("task_work_start_hour", "10") or "10")
        work_end = int(self.setting("task_work_end_hour", "20") or "20")
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO tasks(name,template,max_per_account_per_day,delay_seconds,max_delay_seconds,work_start_hour,work_end_hour,auto_include_new,templates_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (name.strip()[:100], effective_template, max_per_day, delay, max(max_delay, delay), work_start, work_end, 1, json.dumps(templates, ensure_ascii=False), utcnow(), utcnow()))
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
            if enabled and result.rowcount:
                # Include candidates found before the task was started.  Future
                # candidates are assigned atomically in record_decision().
                db.execute(
                    "UPDATE queue SET task_id=?,updated_at=? WHERE status='pending' AND task_id IS NULL",
                    (task_id, utcnow()),
                )
            return result.rowcount == 1
