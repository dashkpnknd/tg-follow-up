from __future__ import annotations

import asyncio
import json
import hashlib
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.errors import (
    FloodWaitError, PasswordHashInvalidError, PeerIdInvalidError,
    PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberInvalidError,
    RPCError, SessionPasswordNeededError,
)
from telethon.errors.common import TypeNotFoundError
from telethon.sessions import MemorySession
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser, User
from pyrogram import Client as PyrogramClient
from pyrogram.errors import FloodWait as PyrogramFloodWait, RPCError as PyrogramRPCError

from analyzer import DEFAULT_FOLLOWUP_VARIANTS, Decision, DialogStatus, Message, classify
from store import Store


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    history_limit: int
    scan_history_pause_seconds: float = 3.0
    scan_dialog_batch_size: int = 25


class FollowupService:
    def __init__(self, store: Store, settings: TelegramSettings):
        self.store, self.settings = store, settings
        self.customer_delivery_lock = asyncio.Lock()
        self.sessions_dir = store.path.parent.parent / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # Local Traffic is deliberately independent of DialogHub.  It is connected
    # once by an administrator through the control bot and its Telethon session
    # stays on this server only.
    manual_sender_session_name = "local-traffic"
    manual_sender_title = "Local Traffic"

    @classmethod
    def is_local_traffic_account(cls, account) -> bool:
        """Return whether an account is reserved for customer campaigns.

        Local Traffic is allowed to serve the 14-day and renewal workflows
        only.  It must never enter the cold-outreach scanner or delivery path,
        even when it shares the common ``accounts`` ledger with mailing
        accounts.  Check both stable session name and normalized display title
        so an account imported from DialogHub cannot bypass this boundary.
        """
        if not account:
            return False
        session_name = " ".join((account["session_name"] or "").casefold().split())
        title = " ".join((account["title"] or "").casefold().split())
        return session_name == cls.manual_sender_session_name or title == cls.manual_sender_title.casefold()

    def is_cold_sender_account(self, account) -> bool:
        """Allow cold outreach only from the explicitly synced mailer pool.

        An empty pool preserves compatibility for an unconfigured deployment.
        Once configured, an account outside that list is never scanned or used
        for a cold message, even if it happens to be enabled in the shared
        account ledger.
        """
        if not account or self.is_local_traffic_account(account):
            return False
        try:
            configured = json.loads(self.store.setting("cold_sender_session_names", "[]") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            configured = []
        allowed = {str(item).casefold() for item in configured if str(item).strip()}
        return not allowed or (account["session_name"] or "").casefold() in allowed

    def _manual_sender_path(self) -> Path:
        return self.sessions_dir / f"{self.manual_sender_session_name}.session"

    def _manual_sender_client(self) -> TelegramClient:
        # Passing a path without the suffix makes Telethon use its own native
        # SQLite session format.  _client() can later read that session too.
        return TelegramClient(str(self._manual_sender_path().with_suffix("")), self.settings.api_id, self.settings.api_hash)

    def _clear_manual_login(self) -> None:
        for key in ("local_traffic_login_phone", "local_traffic_login_hash"):
            self.store.set_setting(key, "")

    def _register_manual_sender(self) -> None:
        self.store.import_account(self.manual_sender_session_name, self.manual_sender_title, str(self._manual_sender_path()))
        account = self.store.account_by_title(self.manual_sender_title)
        if account:
            self.store.set_account_auth_status(int(account["id"]), "authorized")
        self._clear_manual_login()
        self.store.audit("local_traffic_authorized", "Local Traffic подключён через контрольного бота")

    async def begin_manual_sender_login(self, phone: str) -> str:
        clean_phone = "".join(char for char in phone if char.isdigit() or char == "+")
        if not clean_phone.startswith("+") or len(clean_phone) < 8:
            return "invalid_phone"
        client = self._manual_sender_client()
        try:
            await client.connect()
            if await client.is_user_authorized():
                self._register_manual_sender()
                return "already_authorized"
            sent = await client.send_code_request(clean_phone)
            self.store.set_setting("local_traffic_login_phone", clean_phone)
            self.store.set_setting("local_traffic_login_hash", sent.phone_code_hash)
            return "code_sent"
        except PhoneNumberInvalidError:
            return "invalid_phone"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _preflight_with_pyrogram(self, queue_row, templates: list[str], known_templates: list[str], *, send: bool) -> str:
        """Full-history safety gate using the account's native session."""
        account = self.store.account(queue_row["account_id"])
        if not account or self.store.cold_followup_sent_to_peer(int(queue_row["peer_id"])):
            return "cancelled"
        client = self._pyrogram_client(account["session_path"])
        try:
            await client.start()
            peer_id = int(queue_row["peer_id"])
            history = []
            async for item in client.get_chat_history(peer_id):
                history.append(Message(
                    int(item.id), item.date, bool(getattr(item, "outgoing", False)),
                    item.text or item.caption or "",
                ))
            decision = classify(
                history,
                stop_words=self.store.active_stop_words(),
                is_blacklisted=self.store.is_blacklisted(peer_id),
                followup_text=known_templates,
            )
            chat = await client.get_chat(peer_id)
            self.store.record_decision(
                int(account["id"]), peer_id, getattr(chat, "username", None),
                getattr(chat, "first_name", None), decision,
            )
            if decision.status != DialogStatus.CANDIDATE:
                return "cancelled"
            if not send:
                return "validated"
            template = random.SystemRandom().choice(templates)
            await client.send_message(peer_id, template)
            return f"sent:{template}"
        except PyrogramFloodWait as exc:
            return f"flood_wait:{exc.value}"
        except PyrogramRPCError as exc:
            return f"error:{exc.__class__.__name__}"
        except Exception as exc:
            return f"error:{exc.__class__.__name__}"
        finally:
            if client.is_connected:
                await client.stop()

    async def finish_manual_sender_login(self, code: str) -> str:
        phone = self.store.setting("local_traffic_login_phone", "") or ""
        code_hash = self.store.setting("local_traffic_login_hash", "") or ""
        if not phone or not code_hash:
            return "not_started"
        client = self._manual_sender_client()
        try:
            await client.connect()
            await client.sign_in(phone=phone, code="".join(char for char in code if char.isdigit()), phone_code_hash=code_hash)
            self._register_manual_sender()
            return "authorized"
        except SessionPasswordNeededError:
            return "password_needed"
        except PhoneCodeInvalidError:
            return "invalid_code"
        except PhoneCodeExpiredError:
            self._clear_manual_login()
            return "expired_code"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def finish_manual_sender_password(self, password: str) -> str:
        if not (self.store.setting("local_traffic_login_phone", "") or ""):
            return "not_started"
        client = self._manual_sender_client()
        try:
            await client.connect()
            await client.sign_in(password=password)
            self._register_manual_sender()
            return "authorized"
        except PasswordHashInvalidError:
            return "invalid_password"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

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

    def import_dialoghub_account_by_title(self, hub_db_path: str, sessions_dir: str, title: str) -> int:
        """Copy exactly one named DialogHub account for the customer check-in.

        The existing cold-follow-up import can remain limited to its own owner
        label.  This prevents the 14-day campaign from silently selecting a
        different sender account.
        """
        wanted = " ".join((title or "").casefold().split())
        if not wanted:
            return 0
        source = sqlite3.connect(hub_db_path)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute("SELECT session_name,title FROM accounts WHERE enabled=1").fetchall()
        finally:
            source.close()
        matches = [row for row in rows if " ".join((row["title"] or "").casefold().split()) == wanted]
        if len(matches) != 1:
            self.store.audit("checkin_account_import_error", f"Аккаунт «{title}» найден: {len(matches)}")
            return 0
        row = matches[0]
        source_path = Path(sessions_dir) / f"{row['session_name']}.session"
        target_path = self.sessions_dir / source_path.name
        if not source_path.exists():
            self.store.audit("checkin_account_import_error", f"Сессия не найдена: {source_path}")
            return 0
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        self.store.import_account(row["session_name"], row["title"], str(target_path))
        self.store.audit("checkin_account_import", f"Импортирован аккаунт: {row['title']}")
        return 1

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

    def _pyrogram_client(self, session_path: str) -> PyrogramClient:
        """Open the native session when Telethon cannot decode a new layer."""
        return PyrogramClient(
            str(Path(session_path).with_suffix("")),
            api_id=self.settings.api_id,
            api_hash=self.settings.api_hash,
            no_updates=True,
        )

    def client_for_account(self, account_id: int) -> TelegramClient | None:
        account = self.store.account(account_id)
        if not account or not account["enabled"]:
            return None
        return self._client(account["session_path"])

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

    @staticmethod
    def _cursor_peer(cursor):
        """Rebuild a Telethon pagination peer from the durable cursor."""
        if not cursor or not cursor["offset_peer_id"] or not cursor["offset_peer_kind"]:
            return None
        peer_id = int(cursor["offset_peer_id"])
        kind = cursor["offset_peer_kind"]
        if kind == "user" and cursor["offset_peer_access_hash"] is not None:
            return InputPeerUser(peer_id, int(cursor["offset_peer_access_hash"]))
        if kind == "chat":
            return InputPeerChat(peer_id)
        if kind == "channel" and cursor["offset_peer_access_hash"] is not None:
            return InputPeerChannel(peer_id, int(cursor["offset_peer_access_hash"]))
        return None

    @staticmethod
    def _dialog_cursor(dialog):
        peer = getattr(dialog, "input_entity", None)
        message = getattr(dialog, "message", None)
        if isinstance(peer, InputPeerUser):
            kind, peer_id, access_hash = "user", peer.user_id, peer.access_hash
        elif isinstance(peer, InputPeerChat):
            kind, peer_id, access_hash = "chat", peer.chat_id, None
        elif isinstance(peer, InputPeerChannel):
            kind, peer_id, access_hash = "channel", peer.channel_id, peer.access_hash
        else:
            return None
        date, message_id = getattr(message, "date", None), getattr(message, "id", None)
        if not date or not message_id:
            return None
        return {
            "offset_date": date.isoformat(), "offset_message_id": int(message_id),
            "offset_peer_id": int(peer_id), "offset_peer_access_hash": access_hash,
            "offset_peer_kind": kind,
        }

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
            cursor = self.store.cold_scan_cursor(account_id)
            offset_peer = self._cursor_peer(cursor)
            iterator_args = {"limit": self.settings.scan_dialog_batch_size}
            if cursor and cursor["offset_date"] and cursor["offset_message_id"] and offset_peer:
                iterator_args.update({
                    "offset_date": datetime.fromisoformat(cursor["offset_date"]),
                    "offset_id": int(cursor["offset_message_id"]), "offset_peer": offset_peer,
                })
            seen, last_cursor = 0, None
            async for dialog in client.iter_dialogs(**iterator_args):
                seen += 1
                last_cursor = self._dialog_cursor(dialog) or last_cursor
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
                        decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(chat.id), followup_text=self.store.followup_templates())
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
                if decision.status == DialogStatus.CANDIDATE and self.store.cold_followup_sent_to_peer(int(chat.id)):
                    decision = Decision(
                        DialogStatus.FOLLOWUP_SENT,
                        "Дожим уже был отправлен этому человеку с другого аккаунта",
                        decision.relevant_outbound_at,
                    )
                self.store.record_decision(account_id, chat.id, chat.username, name, decision, source_message_id, access_hash)
                if decision.blacklisting_phrase:
                    self.store.add_blacklist(chat.id, f"Автоматически: {decision.blacklisting_phrase}")
                results[decision.status] = results.get(decision.status, 0) + 1
            if seen < self.settings.scan_dialog_batch_size:
                self.store.complete_cold_scan(account_id)
                results["scan_complete"] = 1
            elif last_cursor:
                self.store.save_cold_scan_cursor(account_id, **last_cursor)
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
        self.store.set_setting("cold_scan_in_progress", "1")
        try:
            replied_index = self.dialoghub_replied_peers(hub_db_path) if hub_db_path else {}
            accounts = [account for account in self.store.accounts() if self.is_cold_sender_account(account)]
            self.store.prepare_cold_scan([int(account["id"]) for account in accounts])
            # Establish current state for the whole pool first.  A slow dialog
            # history on one sender must not delay the availability check of the
            # remaining accounts.
            checks = {int(account["id"]): await self.check_account(account["id"]) for account in accounts}
            for account in accounts:
                check = checks[int(account["id"])]
                if check != "authorized":
                    result = {check: 1}
                else:
                    result = await self.scan_account(account["id"], replied_index.get(account["session_name"], set()), prechecked=True)
                for key, value in result.items():
                    totals[key] = totals.get(key, 0) + value
        finally:
            self.store.set_setting("cold_scan_in_progress", "0")
            accounts = [account for account in self.store.accounts() if self.is_cold_sender_account(account)]
            if self.store.cold_scan_complete([int(account["id"]) for account in accounts]):
                self.store.set_setting("cold_initial_scan_complete", "1")
            # Fresh candidates cannot enter delivery until the separate,
            # full-history gate has marked them validated.  Keeping the audit
            # loop alive through the whole scan closes the race where an early
            # empty batch could otherwise declare the audit complete.
            if self.store.has_unvalidated_cold_candidates():
                self.store.set_setting("revalidation_active", "1")
        return totals

    async def preflight_and_send(self, queue_row, *, send: bool = True) -> str:
        """Re-read the complete dialog before delivery or a no-send audit."""
        if send and (self.store.setting("global_paused") == "1" or self.store.setting("delivery_enabled") != "1"):
            return "blocked"
        account = self.store.account(queue_row["account_id"])
        if not account or not account["enabled"]:
            return "cancelled"
        if not self.is_cold_sender_account(account):
            # Defense in depth for legacy queue rows: a cold message must
            # originate from the configured mailing pool, never Local Traffic
            # or an unrelated account in the common ledger.
            return "cancelled"
        if self.store.cold_followup_sent_to_peer(int(queue_row["peer_id"])):
            return "cancelled"
        try:
            templates = self.store._normalise_templates(json.loads(queue_row["templates_json"] or "[]"))
        except (ValueError, TypeError, json.JSONDecodeError):
            templates = [queue_row["template"]]
        known_templates = list(dict.fromkeys([
            *templates,
            *self.store.followup_templates(),
            *DEFAULT_FOLLOWUP_VARIANTS,
        ]))
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
            # A 100-message tail is insufficient for safety: a client can have
            # agreed to a call earlier and received a later outbound message.
            # This final gate reads the complete existing dialog and rejects
            # any chat with even one inbound reply.
            history = [Message(item.id, item.date, bool(item.out), item.message or "") async for item in client.iter_messages(peer, limit=None)]
            # Keep every approved historical wording in the match set.  A
            # template change must never permit a second cold message to a
            # person who received an earlier approved variant.
            decision = classify(history, stop_words=self.store.active_stop_words(), is_blacklisted=self.store.is_blacklisted(queue_row["peer_id"]), followup_text=known_templates)
            self.store.record_decision(account["id"], queue_row["peer_id"], getattr(entity, "username", None), getattr(entity, "first_name", None), decision, peer_access_hash=access_hash)
            if decision.status != DialogStatus.CANDIDATE:
                return "cancelled"
            if not send:
                return "validated"
            template = random.SystemRandom().choice(templates)
            await client.send_message(peer, template)
            return f"sent:{template}"
        except FloodWaitError as exc:
            self.store.audit("flood_wait", f"FloodWait: {exc.seconds} секунд", queue_row["account_id"], queue_row["peer_id"])
            return f"flood_wait:{exc.seconds}"
        except TypeNotFoundError:
            # Some native mailing sessions receive a Telegram constructor that
            # Telethon cannot decode. Their Pyrogram session remains usable.
            if client.is_connected():
                await client.disconnect()
            return await self._preflight_with_pyrogram(queue_row, templates, known_templates, send=send)
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

    async def run_revalidation_tick(self) -> str:
        """Validate one queued dialog without sending a message."""
        rows = self.store.pending_revalidation(limit=1)
        if not rows:
            if self.store.has_unvalidated_cold_candidates():
                return "waiting_for_account"
            if self.store.setting("cold_scan_in_progress") == "1" or self.store.setting("cold_initial_scan_complete") != "1":
                return "waiting_for_scan"
            return "complete"
        row = rows[0]
        result = await self.preflight_and_send(row, send=False)
        if result == "validated":
            self.store.mark_validated(row["id"])
            return "validated"
        if result == "cancelled" or result.startswith("peer_error:"):
            self.store.mark_queue(row["id"], "cancelled", result)
            return "excluded"
        if result == "auth_error" or result.startswith("error:") or result.startswith("flood_wait"):
            self.store.mark_queue(row["id"], "error", result)
            self.store.audit("revalidation_error", result, row["account_id"], row["peer_id"])
            return "error"
        return result

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
            if result.startswith("sent:"):
                template = result.removeprefix("sent:")
                self.store.mark_sent(row["id"], row["account_id"], row["peer_id"], template)
                self.store.audit("followup_sent", f"Дожим отправлен: {template}", row["account_id"], row["peer_id"])
            elif result == "cancelled":
                self.store.mark_queue(row["id"], "cancelled")
            elif result == "blocked":
                self.store.mark_queue(row["id"], "pending")
            elif result.startswith("peer_error:"):
                self.store.mark_queue(row["id"], "cancelled", result)
                self.store.audit("peer_send_skipped", f"Диалог исключён: {result}", row["account_id"], row["peer_id"])
            elif result == "auth_error":
                self.store.mark_queue(row["id"], "error", result)
                cancelled = self.store.disable_sending_for_account(row["account_id"], result)
                self.store.audit("account_send_disabled", f"Аккаунт исключён из отправки: {result}; отменено в очереди: {cancelled}", row["account_id"])
            elif result.startswith("flood_wait:") or result in {"error:PeerFloodError", "error:PeerFlood"}:
                # Telegram's spam/rate warning is neither a recipient problem
                # nor a permanent ban.  Preserve the complete queue and give
                # this sender one full day of rest before it automatically
                # takes the next eligible card.
                until = self.store.cooldown_sending_for_account(row["account_id"], result, hours=24)
                self.store.audit("account_cooldown", f"Лимит Telegram; отлёжка до {until}", row["account_id"])
            else:
                # A recipient or protocol failure is not proof that the whole
                # sender account is unavailable.  Keep its other chats intact.
                self.store.mark_queue(row["id"], "error", result)
                self.store.audit("cold_delivery_error", result, row["account_id"], row["peer_id"])
            return int(result.startswith("sent:"))
        return 0
