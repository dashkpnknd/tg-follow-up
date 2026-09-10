from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import User

from analyzer import Decision, DialogStatus, Message, classify
from store import Store


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    history_limit: int


class FollowupService:
    def __init__(self, store: Store, settings: TelegramSettings):
        self.store, self.settings = store, settings

    def import_dialoghub_accounts(self, hub_db_path: str, sessions_dir: str) -> int:
        """Register existing DialogHub accounts only; this does not open or send from them."""
        source = sqlite3.connect(hub_db_path)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute("SELECT session_name,title FROM accounts WHERE enabled=1").fetchall()
        finally:
            source.close()
        count = 0
        for row in rows:
            path = Path(sessions_dir) / f"{row['session_name']}.session"
            if path.exists():
                self.store.import_account(row["session_name"], row["title"], str(path))
                count += 1
            else:
                self.store.audit("import_missing_session", f"Сессия не найдена: {path}")
        self.store.audit("dialoghub_import", f"Импортировано аккаунтов: {count}")
        return count

    def _client(self, session_path: str) -> TelegramClient:
        path = Path(session_path)
        # Telethon receives a basename and adds .session itself.
        return TelegramClient(str(path.with_suffix("")), self.settings.api_id, self.settings.api_hash)

    async def scan_account(self, account_id: int) -> dict[str, int]:
        account = self.store.account(account_id)
        if not account or not account["enabled"]:
            return {"skipped": 1}
        client = self._client(account["session_path"])
        results: dict[str, int] = {}
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.audit("auth_error", "Сессия не авторизована", account_id)
                return {"auth_error": 1}
            async for dialog in client.iter_dialogs():
                if not isinstance(dialog.entity, User) or getattr(dialog.entity, "bot", False):
                    continue
                history = []
                async for item in client.iter_messages(dialog.id, limit=self.settings.history_limit):
                    history.append(Message(item.id, item.date, bool(item.out), item.message or ""))
                decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(dialog.id))
                name = " ".join(x for x in (dialog.entity.first_name, dialog.entity.last_name) if x)
                self.store.record_decision(account_id, dialog.id, dialog.entity.username, name, decision)
                if decision.blacklisting_phrase:
                    self.store.add_blacklist(dialog.id, f"Автоматически: {decision.blacklisting_phrase}")
                results[decision.status] = results.get(decision.status, 0) + 1
            self.store.audit("scan_finished", f"Просканировано: {sum(results.values())}", account_id)
        except RPCError as exc:
            self.store.audit("scan_error", str(exc), account_id)
            results["error"] = 1
        finally:
            await client.disconnect()
        return results

    async def scan_all(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for account in self.store.accounts():
            result = await self.scan_account(account["id"])
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
        return totals

    async def preflight_and_send(self, queue_row) -> str:
        """Re-read the full dialog at send time. It refuses to bypass Telegram limits."""
        if self.store.setting("global_paused") == "1" or self.store.setting("delivery_enabled") != "1":
            return "blocked"
        account = self.store.account(queue_row["account_id"])
        if not account or not account["enabled"]:
            return "cancelled"
        client = self._client(account["session_path"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return "auth_error"
            entity = await client.get_entity(queue_row["peer_id"])
            history = [Message(item.id, item.date, bool(item.out), item.message or "") async for item in client.iter_messages(entity, limit=self.settings.history_limit)]
            decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(queue_row["peer_id"]))
            self.store.record_decision(account["id"], queue_row["peer_id"], getattr(entity, "username", None), getattr(entity, "first_name", None), decision)
            if decision.status != DialogStatus.CANDIDATE:
                return "cancelled"
            await client.send_message(entity, queue_row["template"])
            return "sent"
        except FloodWaitError as exc:
            self.store.audit("flood_wait", f"FloodWait: {exc.seconds} секунд", queue_row["account_id"], queue_row["peer_id"])
            return f"flood_wait:{exc.seconds}"
        except RPCError as exc:
            return f"error:{exc.__class__.__name__}"
        finally:
            await client.disconnect()

    async def run_delivery_tick(self) -> int:
        """Deliver at most one eligible item per tick; harmless while paused or no task is enabled."""
        if self.store.setting("global_paused") == "1" or self.store.setting("delivery_enabled") != "1":
            return 0
        for row in self.store.pending(limit=50):
            local_hour = __import__("datetime").datetime.now(ZoneInfo(self.store.setting("timezone", "Europe/Moscow"))).hour
            if not row["work_start_hour"] <= local_hour < row["work_end_hour"]:
                continue
            if self.store.sent_today(row["account_id"], row["task_id"]) >= row["max_per_account_per_day"]:
                continue
            self.store.mark_queue(row["id"], "sending")
            result = await self.preflight_and_send(row)
            if result == "sent":
                self.store.mark_sent(row["id"], row["account_id"], row["peer_id"])
                self.store.audit("followup_sent", "Дожим отправлен после финальной проверки", row["account_id"], row["peer_id"])
            elif result == "cancelled":
                self.store.mark_queue(row["id"], "cancelled")
            elif result == "blocked":
                self.store.mark_queue(row["id"], "pending")
            else:
                self.store.mark_queue(row["id"], "error", result)
                if result.startswith("flood_wait"):
                    self.store.set_task_enabled(row["task_id"], False)
                    self.store.audit("task_paused_floodwait", f"Задача {row['task_id']} остановлена: {result}", row["account_id"])
            return int(result == "sent")
        return 0
