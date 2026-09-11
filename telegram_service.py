from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, PeerIdInvalidError, RPCError
from telethon.sessions import MemorySession
from telethon.tl.types import InputPeerUser, User

from analyzer import Decision, DialogStatus, Message, classify
from store import Store


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    history_limit: int
    scan_history_pause_seconds: float = 3.0


class FollowupService:
    def __init__(self, store: Store, settings: TelegramSettings):
        self.store, self.settings = store, settings
        self.sessions_dir = store.path.parent.parent / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def import_dialoghub_accounts(self, hub_db_path: str, sessions_dir: str, title_prefix: str = "") -> int:
        """Register existing DialogHub accounts only; this does not open or send from them."""
        source = sqlite3.connect(hub_db_path)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute("SELECT session_name,title FROM accounts WHERE enabled=1").fetchall()
        finally:
            source.close()
        count = 0
        for row in rows:
            if title_prefix and not (row["title"] or "").casefold().startswith(title_prefix.casefold()):
                continue
            source_path = Path(sessions_dir) / f"{row['session_name']}.session"
            target_path = self.sessions_dir / source_path.name
            if source_path.exists():
                # DialogHub keeps the source session open. SQLite backup gives us a
                # consistent, independent copy without touching DialogHub's file.
                source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
                target = sqlite3.connect(target_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                self.store.import_account(row["session_name"], row["title"], str(target_path))
                count += 1
            else:
                self.store.audit("import_missing_session", f"Сессия не найдена: {source_path}")
        self.store.audit("dialoghub_import", f"Импортировано аккаунтов: {count}")
        return count

    def _client(self, session_path: str) -> TelegramClient:
        """Open a current Telethon client from a copied Pyrogram authorization.

        Pyrogram sessions contain the same Telegram auth key but Pyrogram itself
        does not support several newer Telegram constructors. MemorySession keeps
        conversion read-only: neither DialogHub's file nor our copied session is
        modified during normal scans.
        """
        source = sqlite3.connect(f"file:{Path(session_path)}?mode=ro", uri=True)
        try:
            dc_id, auth_key = next(iter(source.execute("SELECT dc_id,auth_key FROM sessions LIMIT 1")))
        finally:
            source.close()
        servers = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
        session = MemorySession()
        session.set_dc(dc_id, servers.get(dc_id, servers[2]), 443)
        session.auth_key = AuthKey(auth_key)
        return TelegramClient(session, self.settings.api_id, self.settings.api_hash)

    @staticmethod
    def dialoghub_replied_peers(hub_db_path: str) -> dict[str, set[int]]:
        """DialogHub imports only dialogs containing an inbound reply.

        Those peers are already ineligible for this scenario, so using the hub
        as an exclusion index avoids a Telegram history request for each one.
        """
        index: dict[str, set[int]] = {}
        source = sqlite3.connect(f"file:{hub_db_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute("""SELECT a.session_name,d.peer_id
                FROM dialogs d JOIN accounts a ON a.id=d.account_id
                WHERE a.enabled=1""").fetchall()
        finally:
            source.close()
        for row in rows:
            index.setdefault(row["session_name"], set()).add(int(row["peer_id"]))
        return index

    async def check_account(self, account_id: int) -> str:
        """Check only session availability; this never reads dialog history."""
        account = self.store.account(account_id)
        if not account or not account["enabled"]:
            return "disabled"
        if account["send_status"] == "unavailable":
            return "unavailable"
        client = self._client(account["session_path"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.set_account_auth_status(account_id, "unauthorized", "Сессия не авторизована")
                self.store.disable_sending_for_account(account_id, "auth_error")
                self.store.audit("auth_error", "Сессия не авторизована", account_id)
                return "unauthorized"
            self.store.set_account_auth_status(account_id, "authorized")
            self.store.audit("account_checked", "Сессия авторизована; история не читалась", account_id)
            return "authorized"
        except (TimeoutError, OSError, RPCError) as exc:
            # An unavailable check is not treated as a ban. It is skipped for
            # this cycle, without opening any dialog history.
            self.store.set_account_auth_status(account_id, "check_error", exc.__class__.__name__)
            self.store.audit("account_check_error", exc.__class__.__name__, account_id)
            return "check_error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def scan_account(self, account_id: int, replied_peers: set[int] | None = None, prechecked: bool = False) -> dict[str, int]:
        account = self.store.account(account_id)
        if not account or not account["enabled"]:
            return {"skipped": 1}
        if account["send_status"] == "unavailable":
            return {"unavailable": 1}
        if not prechecked:
            check = await self.check_account(account_id)
            if check != "authorized":
                return {check: 1}
        client = self._client(account["session_path"])
        results: dict[str, int] = {}
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.set_account_auth_status(account_id, "unauthorized", "Сессия не авторизована")
                self.store.disable_sending_for_account(account_id, "auth_error")
                self.store.audit("auth_error", "Сессия не авторизована", account_id)
                return {"auth_error": 1}
            known_replied = replied_peers or set()
            async for dialog in client.iter_dialogs():
                chat = dialog.entity
                if not isinstance(chat, User) or getattr(chat, "bot", False):
                    continue
                source_message_id = getattr(dialog.message, "id", None)
                access_hash = getattr(chat, "access_hash", None)
                # Re-listing dialogs is cheap compared to history retrieval. Once
                # a chat has been classified, it is not read again unless a new
                # message changes the dialog's latest-message ID.
                if (source_message_id
                    and self.store.dialog_source_message_id(account_id, chat.id) == source_message_id
                    and not self.store.dialog_requires_recheck(account_id, chat.id)):
                    self.store.update_dialog_access_hash(account_id, chat.id, access_hash)
                    results["unchanged"] = results.get("unchanged", 0) + 1
                    continue
                if chat.id in known_replied:
                    decision = Decision(DialogStatus.REPLIED, "DialogHub уже зафиксировал входящий ответ клиента")
                elif dialog.message and not dialog.message.out:
                    decision = Decision(DialogStatus.REPLIED, "Последнее сообщение в чате — входящий ответ клиента")
                else:
                    try:
                        history = []
                        async for item in client.iter_messages(chat.id, limit=self.settings.history_limit):
                            history.append(Message(item.id, item.date, bool(item.out), item.message or ""))
                        decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(chat.id))
                        # Conservative scan pacing prevents history-import FloodWait.
                        await asyncio.sleep(self.settings.scan_history_pause_seconds)
                    except FloodWaitError as exc:
                        self.store.audit("dialog_history_error", f"FloodWait: {exc.seconds} секунд", account_id, chat.id)
                        results["history_error"] = results.get("history_error", 0) + 1
                        await asyncio.sleep(exc.seconds)
                        continue
                    except (TimeoutError, OSError, RPCError) as exc:
                        self.store.audit("dialog_history_error", f"История не прочитана: {exc.__class__.__name__}", account_id, chat.id)
                        results["history_error"] = results.get("history_error", 0) + 1
                        # Do not save a source ID: the dialog will be retried on a
                        # later scan, but it cannot stop the rest of the account.
                        continue
                name = " ".join(x for x in (chat.first_name, chat.last_name) if x)
                self.store.record_decision(account_id, chat.id, chat.username, name, decision, source_message_id, access_hash)
                if decision.blacklisting_phrase:
                    self.store.add_blacklist(chat.id, f"Автоматически: {decision.blacklisting_phrase}")
                results[decision.status] = results.get(decision.status, 0) + 1
            self.store.audit("scan_finished", f"Просканировано: {sum(results.values())}", account_id)
        except PeerIdInvalidError as exc:
            # A stale/deleted peer is about this one chat, not the sender
            # account. Skip only it and keep the account available.
            return f"peer_error:{exc.__class__.__name__}"
        except RPCError as exc:
            self.store.audit("scan_error", str(exc), account_id)
            results["error"] = 1
        finally:
            if client.is_connected():
                await client.disconnect()
        return results

    async def scan_all(self, hub_db_path: str | None = None) -> dict[str, int]:
        totals: dict[str, int] = {}
        replied_index = self.dialoghub_replied_peers(hub_db_path) if hub_db_path else {}
        for account in self.store.accounts():
            check = await self.check_account(account["id"])
            if check != "authorized":
                result = {check: 1}
            else:
                result = await self.scan_account(account["id"], replied_index.get(account["session_name"], set()), prechecked=True)
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
            access_hash = queue_row["peer_access_hash"]
            entity = None
            if access_hash is not None:
                peer = InputPeerUser(queue_row["peer_id"], access_hash)
            else:
                # Older candidates were collected before access hashes were
                # persisted. Find this existing dialog once and backfill it;
                # do not mistake a missing local cache record for a ban.
                peer = None
                async for dialog in client.iter_dialogs():
                    if getattr(dialog.entity, "id", None) == queue_row["peer_id"]:
                        entity = dialog.entity
                        access_hash = getattr(entity, "access_hash", None)
                        self.store.update_dialog_access_hash(account["id"], queue_row["peer_id"], access_hash)
                        peer = entity
                        break
                if peer is None:
                    return "cancelled"
            history = [Message(item.id, item.date, bool(item.out), item.message or "") async for item in client.iter_messages(peer, limit=self.settings.history_limit)]
            decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(queue_row["peer_id"]))
            self.store.record_decision(account["id"], queue_row["peer_id"], getattr(entity, "username", None), getattr(entity, "first_name", None), decision, peer_access_hash=access_hash)
            if decision.status != DialogStatus.CANDIDATE:
                return "cancelled"
            await client.send_message(peer, queue_row["template"])
            return "sent"
        except FloodWaitError as exc:
            self.store.audit("flood_wait", f"FloodWait: {exc.seconds} секунд", queue_row["account_id"], queue_row["peer_id"])
            return f"flood_wait:{exc.seconds}"
        except RPCError as exc:
            return f"error:{exc.__class__.__name__}"
        except Exception as exc:
            # Treat an unknown Telegram/API failure just like a restriction:
            # the delivery loop will retire this sender instead of retrying it.
            self.store.audit("send_preflight_error", exc.__class__.__name__, queue_row["account_id"], queue_row["peer_id"])
            return f"error:{exc.__class__.__name__}"
        finally:
            if client.is_connected():
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
            last_sent = self.store.last_sent_at(row["account_id"])
            if last_sent:
                # Deterministic jitter gives every account a stable, auditable
                # 10–15 minute pause without storing a second mutable timer.
                spread = max(0, row["max_delay_seconds"] - row["delay_seconds"])
                jitter = int.from_bytes(hashlib.sha256(f"{row['account_id']}:{last_sent.isoformat()}".encode()).digest()[:4], "big") % (spread + 1)
                if datetime.now(timezone.utc) - last_sent.astimezone(timezone.utc) < timedelta(seconds=row["delay_seconds"] + jitter):
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
            elif result.startswith("peer_error:"):
                self.store.mark_queue(row["id"], "cancelled", result)
                self.store.audit("peer_send_skipped", f"Диалог исключён: {result}", row["account_id"], row["peer_id"])
            else:
                self.store.mark_queue(row["id"], "error", result)
                # Restricted, unauthorised and rate-limited accounts are
                # skipped permanently until an operator explicitly restores
                # them.  One bad account must never pause the other accounts
                # or provoke repeated send attempts.
                cancelled = self.store.disable_sending_for_account(row["account_id"], result)
                self.store.audit("account_send_disabled", f"Аккаунт исключён из отправки: {result}; отменено в очереди: {cancelled}", row["account_id"])
            return int(result == "sent")
        return 0
