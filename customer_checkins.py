"""Fourteen-day customer check-ins sourced from an unchanged sales sheet.

The Google Sheet is intentionally read-only.  A compact SQLite ledger in
``Store`` records work already done, decisions by the operator and messages
sent.  This makes a daily import safe even when rows are re-read many times.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import Channel, Chat, User

from analyzer import BLACKLIST_WORDS, normalized
from store import Store, checkin_project_key, project_chat_key, project_chat_match_score
from telegram_service import FollowupService


RUSSIAN_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def column_index(value: str) -> int:
    """Turn an ordinary sheet column letter (B/C/D) into a zero-based index."""
    text = (value or "").strip().upper()
    if text.isdigit():
        return max(0, int(text) - 1)
    if not re.fullmatch(r"[A-Z]+", text):
        raise ValueError(f"Некорректный столбец: {value!r}")
    number = 0
    for letter in text:
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number - 1


def parse_sheet_date(value: str, default_year: int, today: date | None = None) -> date | None:
    text = " ".join((value or "").strip().casefold().split())
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", text)
    if match and match.group(2) in RUSSIAN_MONTHS:
        try:
            month = RUSSIAN_MONTHS[match.group(2)]
            year = int(match.group(3) or default_year)
            # The current sales ledger shows dates without the year and spans
            # December through the current month.  A future-looking month is
            # therefore part of the preceding calendar year.
            if not match.group(3) and today and month > today.month:
                year -= 1
            return date(year, month, int(match.group(1)))
        except ValueError:
            return None
    return None


def parse_amount(value: str) -> int | None:
    text = (value or "").replace("\xa0", " ").strip()
    if not text:
        return None
    # Sheet values such as "р.17 500,00" or "17 500" are both accepted.
    cleaned = re.sub(r"[^0-9,.-]", "", text.replace(" ", ""))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return int(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class SaleRow:
    row_number: int
    sale_date: date
    amount: int
    project_name: str

    @property
    def source_key(self) -> str:
        # No ID column is required.  Equal sale day + amount + project is one
        # customer check-in, which is safer than sending twice.
        material = "|".join((self.sale_date.isoformat(), str(self.amount), checkin_project_key(self.project_name)))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CheckinSettings:
    sales_csv_url: str = ""
    sales_csv_path: str = ""
    sender_account_title: str = ""
    date_column: str = "B"
    amount_column: str = "E"
    project_column: str = "D"
    days_after_sale: int = 14
    due_grace_days: int = 1
    sheet_year: int = 0
    start_date: str = ""
    excluded_usernames: tuple[str, ...] = ()
    excluded_names: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "CheckinSettings":
        return cls(
            sales_csv_url=os.getenv("CHECKIN_SHEET_CSV_URL", "").strip(),
            sales_csv_path=os.getenv("CHECKIN_SHEET_CSV_PATH", "").strip(),
            sender_account_title=os.getenv("CHECKIN_ACCOUNT_TITLE", "").strip(),
            date_column=os.getenv("CHECKIN_SHEET_DATE_COLUMN", "B"),
            amount_column=os.getenv("CHECKIN_SHEET_AMOUNT_COLUMN", "E"),
            project_column=os.getenv("CHECKIN_SHEET_PROJECT_COLUMN", "D"),
            days_after_sale=int(os.getenv("CHECKIN_DAYS_AFTER_SALE", "14")),
            due_grace_days=max(0, int(os.getenv("CHECKIN_DUE_GRACE_DAYS", "1"))),
            sheet_year=int(os.getenv("CHECKIN_SHEET_YEAR", "0")),
            start_date=os.getenv("CHECKIN_START_DATE", "").strip(),
            excluded_usernames=tuple(
                item.strip().lstrip("@").casefold()
                for item in os.getenv("CHECKIN_EXCLUDED_USERNAMES", "").split(",")
                if item.strip()
            ),
            excluded_names=tuple(
                " ".join(item.casefold().split())
                for item in os.getenv("CHECKIN_EXCLUDED_NAMES", "").split(",")
                if item.strip()
            ),
        )


class CustomerCheckinService:
    CHANNEL_MARKERS = {
        "вк": {"вк", "vk", "vkontakte", "вконтакте"},
        "авито": {"авито", "avito"},
    }

    def __init__(self, store: Store, telegram: FollowupService, settings: CheckinSettings):
        self.store = store
        self.telegram = telegram
        self.settings = settings

    def configured(self) -> tuple[bool, str]:
        if not self.settings.sender_account_title:
            return False, "Не задан CHECKIN_ACCOUNT_TITLE"
        if not (self.settings.sales_csv_url or self.settings.sales_csv_path):
            return False, "Не задана ссылка или путь к выгрузке таблицы"
        if not self._start_date():
            return False, "Не задана или некорректна CHECKIN_START_DATE"
        return True, ""

    def _start_date(self) -> date | None:
        try:
            return date.fromisoformat(self.settings.start_date)
        except ValueError:
            return None

    def _csv_text(self) -> str:
        if self.settings.sales_csv_path:
            return Path(self.settings.sales_csv_path).read_text(encoding="utf-8-sig")
        with urlopen(self.settings.sales_csv_url, timeout=30) as response:  # nosec B310: configured by the administrator
            return response.read().decode("utf-8-sig")

    def read_sales(self, today: date | None = None) -> list[SaleRow]:
        date_col = column_index(self.settings.date_column)
        amount_col = column_index(self.settings.amount_column)
        project_col = column_index(self.settings.project_column)
        highest = max(date_col, amount_col, project_col)
        timezone_name = self.store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        today = today or datetime.now(ZoneInfo(timezone_name)).date()
        year = self.settings.sheet_year or today.year
        result: list[SaleRow] = []
        for row_number, row in enumerate(csv.reader(self._csv_text().splitlines()), start=1):
            if len(row) <= highest:
                continue
            sale_date = parse_sheet_date(row[date_col], year, today)
            amount = parse_amount(row[amount_col])
            project_name = " ".join((row[project_col] or "").split())
            if sale_date and amount is not None and project_name:
                result.append(SaleRow(row_number, sale_date, amount, project_name))
        return result

    def import_due_sales(self, today: date | None = None, due_on_or_after: date | None = None) -> dict[str, int]:
        """Read eligible sales without changing the source sheet."""
        timezone_name = self.store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        today = today or datetime.now(ZoneInfo(timezone_name)).date()
        threshold = max(0, int(self.store.setting("checkin_min_amount", "0") or "0"))
        start_date = self._start_date()
        if not start_date:
            raise ValueError("Не задана CHECKIN_START_DATE")
        imported = existing = skipped = 0
        minimum_due_date = due_on_or_after or (today - timedelta(days=self.settings.due_grace_days))
        for sale in self.read_sales(today):
            due_date = sale.sale_date + timedelta(days=self.settings.days_after_sale)
            if sale.sale_date < start_date:
                skipped += 1
                continue
            if (threshold and sale.amount < threshold) or not self._is_approved_traffic_start(sale.project_name):
                skipped += 1
                continue
            if not minimum_due_date <= due_date <= today:
                continue
            _, created = self.store.upsert_customer_checkin(
                source_key=sale.source_key,
                source_row=sale.row_number,
                sale_date=sale.sale_date.isoformat(),
                due_date=due_date.isoformat(),
                amount=sale.amount,
                project_name=sale.project_name,
            )
            if created:
                imported += 1
            else:
                existing += 1
        self.store.audit("checkin_sales_import", f"Новые: {imported}; уже были: {existing}; отсеяны: {skipped}")
        return {"imported": imported, "existing": existing, "skipped": skipped}

    @staticmethod
    def _is_approved_traffic_start(project_name: str) -> bool:
        """Return whether a sale belongs in the 14-day customer workflow.

        The campaign intentionally treats every sale as eligible unless it is
        clearly a test, a top-up/second instalment, or a repeat payment.  The
        sales description is not a product catalogue: channel names and other
        service words must not be used as a hidden whitelist.
        """
        name = " ".join((project_name or "").casefold().split())
        return not any(marker in name for marker in (
            "тест",
            "доплат",
            "вторая часть",
            "вторую часть",
            "2 часть",
            "2-я часть",
            "2я часть",
            "повторная оплат",
            "повторные оплат",
            "повторный оплат",
        ))

    @staticmethod
    def _person(entity: User) -> tuple[int, int | None, str | None, str | None]:
        return (
            int(entity.id),
            getattr(entity, "access_hash", None),
            getattr(entity, "username", None),
            " ".join(part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part) or None,
        )

    def is_excluded_username(self, username: str | None) -> bool:
        return (username or "").strip().lstrip("@").casefold() in self.settings.excluded_usernames

    def is_excluded_contact(self, username: str | None, name: str | None) -> bool:
        return self.is_excluded_username(username) or " ".join((name or "").casefold().split()) in self.settings.excluded_names

    @staticmethod
    def _is_active_group(group: Chat | Channel) -> bool:
        return not any(getattr(group, flag, False) for flag in ("deactivated", "left", "kicked"))

    async def _direct_dialogs(self, client) -> dict[int, tuple[int, int | None, str | None, str | None]]:
        direct: dict[int, tuple[int, int | None, str | None, str | None]] = {}
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, User) and not getattr(entity, "bot", False):
                person = self._person(entity)
                if not self.is_excluded_contact(person[2], person[3]):
                    direct[int(entity.id)] = person
        return direct

    @classmethod
    def _source_channel(cls, project_name: str | None) -> str | None:
        words = set(re.findall(r"[a-zа-яё]+", normalized(project_name or "")))
        found = [channel for channel, markers in cls.CHANNEL_MARKERS.items() if words & markers]
        return found[0] if len(found) == 1 else None

    @classmethod
    def _matches_channel(cls, group: Chat | Channel, channel: str | None) -> bool:
        markers = cls.CHANNEL_MARKERS.get(channel or "")
        if not markers:
            return False
        text = " ".join(str(value or "") for value in (getattr(group, "title", None), getattr(group, "username", None)))
        return bool(set(re.findall(r"[a-zа-яё]+", normalized(text))) & markers)

    async def _history_contacts(self, client, group: Chat | Channel, direct: dict[int, tuple[int, int | None, str | None, str | None]]):
        contacts = []
        seen = set()
        try:
            async for message in client.iter_messages(group, limit=500):
                peer_id = getattr(message, "sender_id", None)
                if peer_id is None or int(peer_id) in seen or int(peer_id) not in direct:
                    continue
                contacts.append(direct[int(peer_id)])
                seen.add(int(peer_id))
        except (TimeoutError, OSError, RPCError):
            return []
        return contacts

    @staticmethod
    def _unique_most_active_contact(contacts, activity):
        ranked = sorted(((activity.get(person[0], 0), person) for person in contacts), reverse=True, key=lambda item: item[0])
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            return None
        return ranked[0]

    async def _longest_contact(self, client, contacts):
        counts = {}
        for person in contacts:
            result = await client.get_messages(person[0], limit=0)
            counts[person[0]] = int(result.total)
        selected = self._unique_most_active_contact(contacts, counts)
        if selected or not contacts:
            return selected
        largest = max(counts.values())
        ranked = []
        for person in contacts:
            if counts[person[0]] == largest:
                recent = await client.get_messages(person[0], limit=1)
                date = recent[0].date if recent else None
                ranked.append((date.timestamp() if date else 0, person[0], person))
        return largest, max(ranked)[2]

    async def resolve_checkin(self, checkin_id: int) -> str:
        row = self.store.checkin(checkin_id)
        retrying_unavailable_sender = bool(
            row
            and row["status"] == "needs_review"
            and (row["reason"] or "").startswith("Аккаунт отправителя недоступен:")
        )
        if not row or (row["status"] != "new" and not retrying_unavailable_sender):
            return "unchanged"
        account = self.store.account_by_title(self.settings.sender_account_title)
        if not account:
            self.store.mark_checkin_review(checkin_id, f"Не найден один аккаунт отправителя: {self.settings.sender_account_title}", preserve_reported=retrying_unavailable_sender)
            return "review"
        checked = await self.telegram.check_account(account["id"])
        if checked != "authorized":
            # The original operator card remains valid while Telegram is still
            # unavailable.  Do not reset its reported marker and create a new
            # duplicate card every scan.
            if retrying_unavailable_sender:
                return "unchanged"
            self.store.mark_checkin_review(checkin_id, f"Аккаунт отправителя недоступен: {checked}", account_id=account["id"])
            return "review"
        client = self.telegram.client_for_account(account["id"])
        if client is None:
            self.store.mark_checkin_review(checkin_id, "Не удалось открыть аккаунт отправителя", account_id=account["id"], preserve_reported=retrying_unavailable_sender)
            return "review"
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.mark_checkin_review(checkin_id, "Сессия аккаунта отправителя не авторизована", account_id=account["id"], preserve_reported=retrying_unavailable_sender)
                return "review"
            group_matches: list[tuple[int, int, Chat | Channel]] = []
            direct = {}
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, User) and not getattr(entity, "bot", False):
                    person = self._person(entity)
                    if not self.is_excluded_contact(person[2], person[3]):
                        direct[int(entity.id)] = person
                elif isinstance(entity, (Chat, Channel)) and self._is_active_group(entity):
                    score = project_chat_match_score(row["project_name"], getattr(entity, "title", ""))
                    if score:
                        last_date = getattr(getattr(dialog, "message", None), "date", None)
                        group_matches.append((score, int(last_date.timestamp()) if last_date else 0, entity))
            groups = [group for _, _, group in group_matches]
            if not groups:
                self.store.mark_checkin_review(checkin_id, "Чат магазина не найден", account_id=account["id"], preserve_reported=retrying_unavailable_sender)
                return "review"
            source_channel = self._source_channel(row["project_name"])
            eligible = group_matches
            if len(group_matches) > 1 and source_channel:
                channel_groups = [item for item in group_matches if self._matches_channel(item[2], source_channel)]
                if channel_groups:
                    eligible = channel_groups
                elif any(self._matches_channel(item[2], channel) for item in group_matches for channel in self.CHANNEL_MARKERS):
                    eligible = []
            if not eligible:
                self.store.mark_checkin_review(
                    checkin_id, f"Чат для канала «{source_channel}» не найден",
                    group_candidates=[(int(group.id), getattr(group, "title", None), getattr(group, "username", None)) for group in groups],
                    account_id=account["id"], preserve_reported=retrying_unavailable_sender,
                )
                return "review"
            best_score = max(score for score, _, _ in eligible)
            best_groups = [(activity, group) for score, activity, group in eligible if score == best_score]
            if len(best_groups) > 1 and len({project_chat_key(getattr(group, "title", "")) for _, group in best_groups}) == 1:
                newest_activity = max(activity for activity, _ in best_groups)
                newest = [group for activity, group in best_groups if activity == newest_activity]
                best_groups = [(newest_activity, group) for group in newest]
            if len(best_groups) != 1:
                self.store.mark_checkin_review(
                    checkin_id,
                    f"Найдено чатов с таким названием: {len(groups)}",
                    group_candidates=[(int(group.id), getattr(group, "title", None), getattr(group, "username", None)) for group in groups],
                    account_id=account["id"],
                    preserve_reported=retrying_unavailable_sender,
                )
                return "review"
            group = best_groups[0][1]
            contacts: list[tuple[int, int | None, str | None, str | None]] = []
            async for participant in client.iter_participants(group):
                if isinstance(participant, User) and int(participant.id) in direct and not getattr(participant, "bot", False):
                    contacts.append(direct[int(participant.id)])
            if not contacts:
                contacts = await self._history_contacts(client, group, direct)
            selected_contact = contacts[0] if len(contacts) == 1 else None
            if selected_contact is None and contacts:
                best = await self._longest_contact(client, contacts)
                if best:
                    _, selected_contact = best
            if selected_contact is None:
                detail = "Нет участника с личной перепиской" if not contacts else f"Подходящих участников с личной перепиской: {len(contacts)}"
                self.store.mark_checkin_review(
                    checkin_id,
                    detail,
                    contacts,
                    account_id=account["id"],
                    group_peer_id=int(group.id),
                    group_title=getattr(group, "title", None),
                    group_username=getattr(group, "username", None),
                    preserve_reported=retrying_unavailable_sender,
                )
                return "review"
            peer_id, access_hash, username, name = selected_contact
            self.store.mark_checkin_ready(
                checkin_id,
                account_id=account["id"],
                group_peer_id=int(group.id),
                group_title=getattr(group, "title", None),
                group_username=getattr(group, "username", None),
                peer_id=peer_id,
                access_hash=access_hash,
                username=username,
                name=name,
            )
            self.store.audit("checkin_contact_found", f"Контакт для {row['project_name']}: {name or username or peer_id}", account["id"], peer_id)
            return "ready"
        except FloodWaitError as exc:
            self.store.mark_checkin_error(checkin_id, f"Telegram попросил подождать {exc.seconds} секунд")
            return "error"
        except (TimeoutError, OSError, RPCError) as exc:
            self.store.mark_checkin_error(checkin_id, f"Ошибка поиска в Telegram: {exc.__class__.__name__}")
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def resolve_new_checkins(self, limit: int = 20) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in self.store.checkins_to_resolve(limit):
            result = await self.resolve_checkin(row["id"])
            totals[result] = totals.get(result, 0) + 1
        return totals

    async def choose_candidate(self, checkin_id: int, peer_id: int) -> bool:
        row = self.store.checkin(checkin_id)
        candidate = next((item for item in self.store.checkin_candidates(checkin_id) if int(item["peer_id"]) == peer_id), None)
        if not row or row["status"] != "needs_review" or not candidate or not row["account_id"] or self.is_excluded_contact(candidate["username"], candidate["name"]):
            return False
        self.store.mark_checkin_ready(
            checkin_id,
            account_id=int(row["account_id"]),
            group_peer_id=row["group_peer_id"],
            group_title=row["group_title"],
            group_username=row["group_username"],
            peer_id=int(candidate["peer_id"]),
            access_hash=candidate["peer_access_hash"],
            username=candidate["username"],
            name=candidate["name"],
            reason="Контакт выбран оператором",
        )
        return True

    async def choose_group(self, checkin_id: int, group_peer_id: int) -> str:
        row = self.store.checkin(checkin_id)
        candidate = next((item for item in self.store.checkin_group_candidates(checkin_id) if int(item["peer_id"]) == group_peer_id), None)
        if not row or row["status"] != "needs_review" or not candidate or not row["account_id"]:
            return "missing"
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            return "missing"
        try:
            await client.connect()
            selected = None
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, (Chat, Channel)) and self._is_active_group(entity) and int(entity.id) == group_peer_id:
                    selected = entity
                    break
            if selected is None:
                self.store.mark_checkin_error(checkin_id, "Выбранный чат больше не найден")
                return "error"
            direct = await self._direct_dialogs(client)
            contacts = []
            async for participant in client.iter_participants(selected):
                if isinstance(participant, User) and int(participant.id) in direct and not getattr(participant, "bot", False):
                    contacts.append(direct[int(participant.id)])
            if len(contacts) != 1:
                detail = "Нет участника с личной перепиской" if not contacts else f"Подходящих участников с личной перепиской: {len(contacts)}"
                self.store.mark_checkin_review(
                    checkin_id, detail, contacts,
                    account_id=int(row["account_id"]), group_peer_id=int(selected.id),
                    group_title=getattr(selected, "title", None), group_username=getattr(selected, "username", None),
                )
                return "review"
            peer_id, access_hash, username, name = contacts[0]
            self.store.mark_checkin_ready(
                checkin_id, account_id=int(row["account_id"]), group_peer_id=int(selected.id),
                group_title=getattr(selected, "title", None), group_username=getattr(selected, "username", None),
                peer_id=peer_id, access_hash=access_hash, username=username, name=name,
                reason="Чат и контакт выбраны оператором",
            )
            return "ready"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def choose_username(self, checkin_id: int, username: str) -> str:
        row = self.store.checkin(checkin_id)
        if not row or not row["account_id"]:
            return "missing"
        wanted = username.strip().lstrip("@").casefold()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", wanted):
            return "invalid"
        if self.is_excluded_username(wanted):
            return "excluded"
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            return "missing"
        try:
            await client.connect()
            matches = [person async for person in self._matching_direct_username(client, wanted)]
            if len(matches) != 1:
                return "not_found" if not matches else "ambiguous"
            peer_id, access_hash, actual_username, name = matches[0]
            self.store.mark_checkin_ready(
                checkin_id,
                account_id=int(row["account_id"]),
                group_peer_id=row["group_peer_id"],
                group_title=row["group_title"],
                group_username=row["group_username"],
                peer_id=peer_id,
                access_hash=access_hash,
                username=actual_username,
                name=name,
                reason="Контакт указан оператором",
            )
            return "ready"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _matching_direct_username(self, client, wanted: str):
        direct = await self._direct_dialogs(client)
        for person in direct.values():
            if (person[2] or "").casefold() == wanted:
                yield person

    async def deliver_one(self) -> tuple[str, int | None]:
        async with self.telegram.customer_delivery_lock:
            recent = [sent for sent in (self.store.last_renewal_sent_at(), self.store.last_checkin_sent_at()) if sent]
            if recent and datetime.now(timezone.utc) - max(recent).astimezone(timezone.utc) < timedelta(minutes=30):
                return "waiting", None
            return await self._deliver_one_locked()

    async def _deliver_one_locked(self) -> tuple[str, int | None]:
        if self.store.setting("global_paused") == "1" or self.store.setting("checkin_delivery_enabled") != "1":
            return "blocked", None
        shared_slot = self.store.setting("local_traffic_next_at")
        if shared_slot:
            try:
                due = datetime.fromisoformat(shared_slot)
                if due.tzinfo is None:
                    return "invalid_shared_slot", None
                if datetime.now(timezone.utc) < due.astimezone(timezone.utc):
                    return "waiting", None
            except ValueError:
                return "invalid_shared_slot", None
        template = self.store.next_checkin_template()
        if not template:
            return "missing_template", None
        timezone_name = self.store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        local_now = datetime.now(ZoneInfo(timezone_name))
        work_start = int(self.store.setting("checkin_work_start_hour", "8") or "8")
        work_end = int(self.store.setting("checkin_work_end_hour", "18") or "18")
        if not 0 <= work_start < work_end <= 24:
            return "outside_hours", None
        if not work_start <= local_now.hour < work_end:
            return "outside_hours", None
        # An explicit launch time makes it safe to prepare the service before
        # its first working day.  A malformed value blocks delivery instead of
        # accidentally starting it early.
        launch_at = os.getenv("CHECKIN_DELIVERY_START_AT", "").strip()
        if launch_at:
            try:
                launch_time = datetime.fromisoformat(launch_at)
                if launch_time.tzinfo is None:
                    launch_time = launch_time.replace(tzinfo=ZoneInfo(timezone_name))
                else:
                    launch_time = launch_time.astimezone(ZoneInfo(timezone_name))
            except ValueError:
                return "scheduled", None
            if local_now < launch_time:
                return "scheduled", None
        daily_limit = int(self.store.setting("checkin_max_per_day", "5") or "5")
        if daily_limit > 0 and self.store.checkin_sent_today(timezone_name) >= daily_limit:
            return "daily_limit", None
        last_sent = self.store.last_checkin_sent_at()
        minimum_delay = int(self.store.setting("checkin_delay_seconds", "2400") or "2400")
        maximum_delay = max(minimum_delay, int(self.store.setting("checkin_max_delay_seconds", str(minimum_delay)) or minimum_delay))
        next_allowed_raw = self.store.setting("checkin_next_delivery_at")
        try:
            next_allowed = datetime.fromisoformat(next_allowed_raw) if next_allowed_raw else None
        except ValueError:
            next_allowed = None
        if next_allowed and datetime.now(timezone.utc) < next_allowed.astimezone(timezone.utc):
            return "waiting", None
        if last_sent and not next_allowed and datetime.now(timezone.utc) - last_sent.astimezone(timezone.utc) < timedelta(seconds=minimum_delay):
            return "waiting", None
        rows = self.store.pending_checkin_delivery(local_now.date().isoformat(), limit=1)
        if not rows:
            return "empty", None
        row = rows[0]
        checkin_id = int(row["id"])
        peer_id = int(row["target_peer_id"])
        if self.store.is_blacklisted(peer_id):
            self.store.mark_checkin_skipped(checkin_id, "Получатель находится в blacklist")
            return "skipped", checkin_id
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            self.store.mark_checkin_error(checkin_id, "Не удалось открыть аккаунт отправителя")
            return "error", checkin_id
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.mark_checkin_error(checkin_id, "Сессия аккаунта отправителя не авторизована")
                return "error", checkin_id
            direct = await self._direct_dialogs(client)
            person = direct.get(peer_id)
            if not person:
                self.store.mark_checkin_review(checkin_id, "Личная переписка с выбранным человеком больше не найдена", account_id=int(row["account_id"]))
                return "review", checkin_id
            _, _, username, name = person
            # Explicit permanent opt-outs apply even if they are older than
            # the recent conversation window; ordinary stop phrases remain
            # limited to recent context to avoid historical false positives.
            stop_words = [normalized(item) for item in self.store.active_stop_words()]
            permanent_opt_outs = [normalized(item) for item in BLACKLIST_WORDS]
            index = 0
            async for message in client.iter_messages(peer_id, limit=None):
                if not message.out:
                    text = normalized(message.message or "")
                    phrases = permanent_opt_outs if index >= 100 else stop_words
                    phrase = next((word for word in phrases if word and word in text), None)
                    if phrase:
                        self.store.add_blacklist(peer_id, f"Чек-ин: клиент написал «{phrase}»")
                        self.store.mark_checkin_skipped(checkin_id, f"Клиент попросил не писать: «{phrase}»")
                        return "skipped", checkin_id
                index += 1
            sent = await client.send_message(peer_id, template)
            self.store.mark_checkin_sent(checkin_id, int(sent.id), template)
            wait_seconds = random.randint(minimum_delay, maximum_delay)
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)).isoformat()
            self.store.set_setting("checkin_next_delivery_at", next_at)
            self.store.set_setting("local_traffic_next_at", next_at)
            self.store.audit("customer_checkin_sent", f"Проект: {row['project_name']}; получатель: {name or username or peer_id}", int(row["account_id"]), peer_id)
            return "sent", checkin_id
        except FloodWaitError as exc:
            self.store.mark_checkin_error(checkin_id, f"Telegram попросил подождать {exc.seconds} секунд")
            return "error", checkin_id
        except (TimeoutError, OSError, RPCError) as exc:
            self.store.mark_checkin_error(checkin_id, f"Ошибка отправки: {exc.__class__.__name__}")
            return "error", checkin_id
        finally:
            if client.is_connected():
                await client.disconnect()
