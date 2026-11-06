"""Follow-ups driven by red and white rows in the renewal Google Sheet."""

from __future__ import annotations

import hashlib
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import Channel, Chat, User

from analyzer import BLACKLIST_WORDS, normalized
from store import Store, project_chat_key, project_chat_match_score
from telegram_service import FollowupService


MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
RED_COLORS = {"FFFF0000", "FFF4CCCC"}
WHITE = "FFFFFFFF"


class RenewalFollowupService:
    # The spreadsheet calls these channels "ВК" and "Авито", while group
    # titles are not completely uniform.  Match a whole word in either a
    # title or public username, never a substring of a shop name.
    CHANNEL_MARKERS = {
        "вк": {"вк", "vk", "vkontakte", "вконтакте"},
        "авито": {"авито", "avito"},
    }
    RETRYABLE_REVIEW_REASONS = (
        "Чат с таким названием не найден",
        "Найдено чатов с таким названием:",
        "Нет участника с личной перепиской",
        "Участников с личной перепиской:",
        "Подходящих участников с личной перепиской:",
    )

    def __init__(
        self,
        store: Store,
        telegram: FollowupService,
        *,
        sheet_xlsx_url: str,
        sender_account_title: str,
        excluded_usernames: tuple[str, ...] = (),
        excluded_names: tuple[str, ...] = (),
    ):
        self.store = store
        self.telegram = telegram
        self.sheet_xlsx_url = sheet_xlsx_url.strip()
        self.sender_account_title = sender_account_title.strip()
        self.excluded_usernames = {item.strip().lstrip("@").casefold() for item in excluded_usernames if item.strip()}
        self.excluded_names = {" ".join(item.casefold().split()) for item in excluded_names if item.strip()}

    @classmethod
    def from_environment(cls, store: Store, telegram: FollowupService) -> "RenewalFollowupService":
        return cls(
            store,
            telegram,
            sheet_xlsx_url=os.getenv("RENEWAL_SHEET_XLSX_URL", "").strip(),
            sender_account_title=os.getenv("RENEWAL_ACCOUNT_TITLE", "").strip(),
            excluded_usernames=tuple(os.getenv("CHECKIN_EXCLUDED_USERNAMES", "").split(",")),
            excluded_names=tuple(os.getenv("CHECKIN_EXCLUDED_NAMES", "").split(",")),
        )

    def is_excluded_contact(self, username: str | None, name: str | None) -> bool:
        return (
            bool(username and username.lstrip("@").casefold() in self.excluded_usernames)
            or bool(name and " ".join(name.casefold().split()) in self.excluded_names)
        )

    @staticmethod
    def _is_active_group(group: Chat | Channel) -> bool:
        return not any(getattr(group, flag, False) for flag in ("deactivated", "left", "kicked"))

    def configured(self) -> tuple[bool, str]:
        if not self.sheet_xlsx_url:
            return False, "Не задана XLSX-выгрузка таблицы продлений"
        if not self.sender_account_title:
            return False, "Не задан RENEWAL_ACCOUNT_TITLE"
        return True, ""

    def _workbook(self):
        with urlopen(self.sheet_xlsx_url, timeout=30) as response:  # nosec B310: URL is configured by the administrator.
            content = response.read()
        return load_workbook(BytesIO(content), data_only=True, read_only=False)

    def _renewal_due_date(self, sheet_name: str, today: date) -> date | None:
        title = (sheet_name or "").casefold().strip()
        match = re.search(r"(?<![а-яё])(" + "|".join(MONTHS) + r")(?![а-яё])", title)
        month = MONTHS.get(match.group(1)) if match else None
        if not month:
            return None
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        year = int(year_match.group(1)) if year_match else today.year - int(month > today.month)
        if month == 12:
            return date(year + 1, 1, int(self.store.setting("renewal_white_day", "5") or "5"))
        return date(year, month + 1, int(self.store.setting("renewal_white_day", "5") or "5"))

    @staticmethod
    def _fill_rgb(cell) -> str:
        color = cell.fill.fgColor
        return (color.rgb or "").upper() if color.type == "rgb" else ""

    def scan_sheet(self, today: date | None = None) -> dict[str, int]:
        """Record red and white renewal rows across all month tabs.

        Only actual customer rows in B:D qualify: channel B must be VK or
        Avito and column C must contain the project name. Empty spreadsheet
        rows are never considered white cases.
        """
        timezone_name = self.store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        today = today or datetime.now(ZoneInfo(timezone_name)).date()
        added = existing = red = white = ignored = 0
        workbook = self._workbook()
        current_white_keys, current_red_keys = set(), set()
        for sheet in workbook.worksheets:
            white_due = self._renewal_due_date(sheet.title, today)
            for row_number in range(1, sheet.max_row + 1):
                channel = " ".join(str(sheet.cell(row_number, 2).value or "").split())
                project = " ".join(str(sheet.cell(row_number, 3).value or "").split())
                if channel.casefold() not in {"вк", "авито"} or not project:
                    continue
                color = self._fill_rgb(sheet.cell(row_number, 3))
                if color in RED_COLORS:
                    kind, due_date = "red", today
                    red += 1
                elif color == WHITE and white_due:
                    kind, due_date = "white", white_due
                    white += 1
                else:
                    ignored += 1
                    continue
                raw_amount = sheet.cell(row_number, 4).value
                amount = int(raw_amount) if isinstance(raw_amount, (int, float)) else None
                stable = "|".join((sheet.title, channel.casefold(), project_chat_key(project), str(amount or "")))
                source_key = hashlib.sha256(stable.encode("utf-8")).hexdigest()
                if kind == "white":
                    current_white_keys.add(source_key)
                else:
                    current_red_keys.add(source_key)
                _, created = self.store.upsert_renewal_followup(
                    source_key=source_key,
                    sheet_name=sheet.title,
                    row_number=row_number,
                    channel=channel,
                    project_name=project,
                    amount=amount,
                    trigger_kind=kind,
                    due_date=due_date.isoformat(),
                    today=today.isoformat(),
                )
                if created:
                    added += 1
                else:
                    existing += 1
        self.store.reconcile_renewal_colors(today.isoformat(), current_red_keys, current_white_keys)
        activated = self.store.activate_due_white_renewals(today.isoformat(), current_white_keys)
        self.store.audit("renewal_sheet_scan", f"Новые: {added}; были: {existing}; красные: {red}; белые: {white}; активировано белых: {activated}")
        return {"added": added, "existing": existing, "red": red, "white": white, "activated": activated, "ignored": ignored}

    @staticmethod
    def _person(entity: User) -> tuple[int, int | None, str | None, str | None]:
        return (
            int(entity.id), getattr(entity, "access_hash", None), getattr(entity, "username", None),
            " ".join(item for item in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if item) or None,
        )

    async def _direct_dialogs(self, client) -> dict[int, tuple[int, int | None, str | None, str | None]]:
        dialogs = {}
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, User) and not getattr(entity, "bot", False):
                dialogs[int(entity.id)] = self._person(entity)
        return dialogs

    @classmethod
    def _matches_channel(cls, group: Chat | Channel, channel: str | None) -> bool:
        """Return whether a duplicate project chat is labelled for its channel."""
        markers = cls.CHANNEL_MARKERS.get(normalized(channel or ""))
        if not markers:
            return False
        text = " ".join(
            str(value or "") for value in (getattr(group, "title", None), getattr(group, "username", None))
        )
        words = set(re.findall(r"[a-zа-яё]+", normalized(text)))
        return bool(words & markers)

    @classmethod
    def _has_known_channel_marker(cls, group: Chat | Channel) -> bool:
        text = " ".join(
            str(value or "") for value in (getattr(group, "title", None), getattr(group, "username", None))
        )
        words = set(re.findall(r"[a-zа-яё]+", normalized(text)))
        return bool(words & set().union(*cls.CHANNEL_MARKERS.values()))

    async def _history_contacts(
        self,
        client,
        group: Chat | Channel,
        direct: dict[int, tuple[int, int | None, str | None, str | None]],
    ) -> list[tuple[int, int | None, str | None, str | None]]:
        """Recover possible contacts when Telegram hides the member list.

        Some channels return zero participants even though the Local Traffic
        account can read their messages. In that case only authors of recent
        messages are eligible; this never invents a group membership.
        """
        contacts = []
        seen = set()
        try:
            async for message in client.iter_messages(group, limit=500):
                peer_id = getattr(message, "sender_id", None)
                if peer_id is None or int(peer_id) in seen or int(peer_id) not in direct:
                    continue
                person = direct[int(peer_id)]
                if not self.is_excluded_contact(person[2], person[3]):
                    contacts.append(person)
                    seen.add(int(peer_id))
        except (TimeoutError, OSError, RPCError):
            return []
        return contacts

    @staticmethod
    def _unique_most_active_contact(
        contacts: list[tuple[int, int | None, str | None, str | None]],
        dialog_activity: dict[int, int],
    ) -> tuple[tuple[int, int | None, str | None, str | None], int] | None:
        """Pick the unique contact with the largest supplied message count."""
        counts = [(dialog_activity.get(contact[0], 0), contact) for contact in contacts]
        if not counts:
            return None
        counts.sort(key=lambda item: item[0], reverse=True)
        if len(counts) > 1 and counts[0][0] == counts[1][0]:
            return None
        return counts[0][1], counts[0][0]

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
        return max(ranked)[2], largest

    def _can_retry_review(self, row) -> bool:
        return bool(
            row
            and row["status"] == "needs_review"
            and (row["reason"] or "").startswith(self.RETRYABLE_REVIEW_REASONS)
        )

    async def resolve_followup(self, followup_id: int, *, retry_review: bool = False,
                               allowed_group_ids: set[int] | None = None,
                               excluded_peer_ids: set[int] | None = None,
                               excluded_usernames: set[str] | None = None) -> str:
        row = self.store.renewal_followup(followup_id)
        retrying_unavailable_sender = bool(
            row
            and row["status"] == "needs_review"
            and (row["reason"] or "").startswith("Аккаунт отправителя недоступен:")
        )
        retrying_match = retry_review and self._can_retry_review(row)
        if not row or (row["status"] != "new" and not retrying_unavailable_sender and not retrying_match):
            return "unchanged"
        account = self.store.account_by_title(self.sender_account_title)
        if not account:
            self.store.mark_renewal_review(followup_id, f"Не найден один аккаунт отправителя: {self.sender_account_title}", preserve_reported=retrying_unavailable_sender)
            return "review"
        checked = await self.telegram.check_account(account["id"])
        if checked != "authorized":
            if retrying_unavailable_sender:
                return "unchanged"
            self.store.mark_renewal_review(followup_id, f"Аккаунт отправителя недоступен: {checked}", account_id=account["id"])
            return "review"
        client = self.telegram.client_for_account(account["id"])
        if client is None:
            self.store.mark_renewal_review(followup_id, "Не удалось открыть аккаунт отправителя", account_id=account["id"], preserve_reported=retrying_unavailable_sender)
            return "review"
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.mark_renewal_review(followup_id, "Аккаунт отправителя не авторизован", account_id=account["id"], preserve_reported=retrying_unavailable_sender)
                return "review"
            direct: dict[int, tuple[int, int | None, str | None, str | None]] = {}
            group_matches: list[tuple[int, int, Chat | Channel]] = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if (isinstance(entity, User) and not getattr(entity, "bot", False)
                        and int(entity.id) not in (excluded_peer_ids or ())
                        and (entity.username or "").casefold() not in (excluded_usernames or ())):
                    direct[int(entity.id)] = self._person(entity)
                elif (isinstance(entity, (Chat, Channel)) and self._is_active_group(entity)
                      and (allowed_group_ids is None or int(entity.id) in allowed_group_ids)):
                    score = project_chat_match_score(row["project_name"], getattr(entity, "title", ""))
                    if score:
                        last_date = getattr(getattr(dialog, "message", None), "date", None)
                        group_matches.append((score, int(last_date.timestamp()) if last_date else 0, entity))
            groups = [group for _, _, group in group_matches]
            channel_matches = [(score, activity, group) for score, activity, group in group_matches if self._matches_channel(group, row["channel"])]
            has_channelled_group = any(self._has_known_channel_marker(group) for _, _, group in group_matches)
            # A single matching project chat is always safe. The spreadsheet
            # channel resolves only a genuine choice between several chats.
            eligible_groups = (
                group_matches if len(group_matches) == 1
                else channel_matches if has_channelled_group else group_matches
            )
            selected_group = None
            group_reason = "Чат найден автоматически"
            if eligible_groups:
                best_score = max(score for score, _, _ in eligible_groups)
                best_matches = [(activity, group) for score, activity, group in eligible_groups if score == best_score]
                if len(best_matches) == 1:
                    selected_group = best_matches[0][1]
                    group_reason = (
                        f"Чат выбран по каналу «{row['channel']}»"
                        if len(group_matches) > 1 and has_channelled_group else "Чат найден автоматически"
                    )
                elif len({project_chat_key(getattr(group, "title", "")) for _, group in best_matches}) == 1:
                    best_activity = max(activity for activity, _ in best_matches)
                    newest = [group for activity, group in best_matches if activity == best_activity]
                    if len(newest) == 1:
                        selected_group = newest[0]
                        group_reason = f"Чат выбран по каналу «{row['channel']}» и последней активности"
            if selected_group is None:
                message = (
                    "Чат с таким названием не найден" if not groups
                    else f"Чат для канала «{row['channel']}» не найден" if has_channelled_group and not channel_matches
                    else f"Найдено чатов с таким названием: {len(groups)}"
                )
                self.store.mark_renewal_review(
                    followup_id,
                    message,
                    account_id=account["id"],
                    group_candidates=[(int(group.id), getattr(group, "title", None), getattr(group, "username", None)) for group in groups],
                    preserve_reported=retrying_unavailable_sender,
                )
                return "review"
            matches = []
            async for participant in client.iter_participants(selected_group):
                if (
                    isinstance(participant, User)
                    and not getattr(participant, "bot", False)
                    and int(participant.id) in direct
                    and not self.is_excluded_contact(getattr(participant, "username", None), self._person(participant)[3])
                ):
                    matches.append(direct[int(participant.id)])
            if not matches:
                matches = await self._history_contacts(client, selected_group, direct)
            selected_contact = matches[0] if len(matches) == 1 else None
            contact_reason = "Контакт найден автоматически"
            if selected_contact is None and matches:
                best = await self._longest_contact(client, matches)
                if best:
                    selected_contact, count = best
                    contact_reason = f"Контакт выбран по количеству сообщений в личном диалоге: {count}"
            if selected_contact is None:
                message = "Нет участника с личной перепиской" if not matches else f"Участников с личной перепиской: {len(matches)}"
                self.store.mark_renewal_review(
                    followup_id, message, matches, account["id"],
                    group_peer_id=int(selected_group.id), group_title=getattr(selected_group, "title", None), group_username=getattr(selected_group, "username", None),
                    preserve_reported=retrying_unavailable_sender,
                )
                return "review"
            peer_id, access_hash, username, name = selected_contact
            self.store.mark_renewal_ready(
                followup_id, account_id=account["id"], peer_id=peer_id, access_hash=access_hash, username=username, name=name,
                reason=f"{group_reason}; {contact_reason}", group_peer_id=int(selected_group.id), group_title=getattr(selected_group, "title", None), group_username=getattr(selected_group, "username", None),
            )
            return "ready"
        except FloodWaitError as exc:
            self.store.mark_renewal_error(followup_id, f"Telegram попросил подождать {exc.seconds} секунд")
            return "error"
        except (TimeoutError, OSError, RPCError) as exc:
            self.store.mark_renewal_error(followup_id, f"Ошибка поиска в Telegram: {exc.__class__.__name__}")
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def resolve_new_followups(self, limit: int = 20) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in self.store.renewal_followups_to_resolve(limit):
            result = await self.resolve_followup(int(row["id"]))
            totals[result] = totals.get(result, 0) + 1
        return totals

    async def choose_candidate(self, followup_id: int, peer_id: int) -> bool:
        row = self.store.renewal_followup(followup_id)
        candidate = next((item for item in self.store.renewal_candidates(followup_id) if int(item["peer_id"]) == peer_id), None)
        if not row or not candidate or not row["account_id"] or self.is_excluded_contact(candidate["username"], candidate["name"]):
            return False
        self.store.mark_renewal_ready(
            followup_id, account_id=int(row["account_id"]), peer_id=int(candidate["peer_id"]), access_hash=candidate["peer_access_hash"],
            username=candidate["username"], name=candidate["name"], reason="Контакт выбран оператором",
        )
        return True

    async def choose_group(self, followup_id: int, group_peer_id: int) -> str:
        row = self.store.renewal_followup(followup_id)
        group_candidate = next((item for item in self.store.renewal_group_candidates(followup_id) if int(item["peer_id"]) == group_peer_id), None)
        if not row or row["status"] != "needs_review" or not group_candidate or not row["account_id"]:
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
                self.store.mark_renewal_error(followup_id, "Выбранный чат больше не найден")
                return "error"
            direct = {}
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, User) and not getattr(entity, "bot", False):
                    direct[int(entity.id)] = self._person(entity)
            contacts = []
            async for participant in client.iter_participants(selected):
                if isinstance(participant, User) and int(participant.id) in direct and not getattr(participant, "bot", False):
                    person = direct[int(participant.id)]
                    if not self.is_excluded_contact(person[2], person[3]):
                        contacts.append(person)
            if not contacts:
                contacts = await self._history_contacts(client, selected, direct)
            selected_contact = contacts[0] if len(contacts) == 1 else None
            contact_reason = "Чат и контакт выбраны оператором"
            if selected_contact is None and contacts:
                best = await self._longest_contact(client, contacts)
                if best:
                    selected_contact, count = best
                    contact_reason = f"Чат выбран оператором; контакт выбран по количеству сообщений в личном диалоге: {count}"
            if selected_contact is None:
                reason = "Нет участника с личной перепиской" if not contacts else f"Подходящих участников с личной перепиской: {len(contacts)}"
                self.store.mark_renewal_review(
                    followup_id, reason, contacts, int(row["account_id"]),
                    group_peer_id=int(selected.id), group_title=getattr(selected, "title", None), group_username=getattr(selected, "username", None),
                )
                return "review"
            peer_id, access_hash, username, name = selected_contact
            self.store.mark_renewal_ready(
                followup_id, account_id=int(row["account_id"]), peer_id=peer_id, access_hash=access_hash, username=username, name=name,
                reason=contact_reason, group_peer_id=int(selected.id), group_title=getattr(selected, "title", None), group_username=getattr(selected, "username", None),
            )
            return "ready"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def choose_username(self, followup_id: int, username: str) -> str:
        row = self.store.renewal_followup(followup_id)
        wanted = username.strip().lstrip("@").casefold()
        if not row or not row["account_id"]:
            return "missing"
        if not re.fullmatch(r"[a-z0-9_]{5,32}", wanted):
            return "invalid"
        if wanted in self.excluded_usernames:
            return "excluded"
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            return "missing"
        try:
            await client.connect()
            direct = await self._direct_dialogs(client)
            matches = [person for person in direct.values() if (person[2] or "").casefold() == wanted]
            if len(matches) != 1:
                return "not_found" if not matches else "ambiguous"
            peer_id, access_hash, actual_username, name = matches[0]
            if self.is_excluded_contact(actual_username, name):
                return "excluded"
            self.store.mark_renewal_ready(followup_id, account_id=int(row["account_id"]), peer_id=peer_id, access_hash=access_hash, username=actual_username, name=name, reason="Контакт указан оператором")
            return "ready"
        except (TimeoutError, OSError, RPCError):
            return "error"
        finally:
            if client.is_connected():
                await client.disconnect()

    async def deliver_one(self) -> tuple[str, int | None]:
        async with self.telegram.customer_delivery_lock:
            recent = [sent for sent in (self.store.last_renewal_sent_at(), self.store.last_checkin_sent_at()) if sent]
            if recent and datetime.now(timezone.utc) - max(recent).astimezone(timezone.utc) < timedelta(minutes=30):
                return "waiting", None
            return await self._deliver_one_locked()

    async def _check_private_recipient(self, client, row, *,
                                       excluded_peer_ids: set[int] | None = None,
                                       excluded_usernames: set[str] | None = None) -> str:
        """Existing direct-dialog, opt-out and blacklist checks shared by both offers."""
        peer_id = int(row["target_peer_id"])
        direct = await self._direct_dialogs(client)
        person = direct.get(peer_id)
        if not person:
            self.store.mark_renewal_review(int(row["id"]), "Личная переписка с выбранным человеком больше не найдена", account_id=int(row["account_id"]))
            return "review"
        if (peer_id in (excluded_peer_ids or ())
                or (person[2] or "").casefold() in (excluded_usernames or ())
                or self.is_excluded_contact(person[2], person[3])):
            self.store.mark_renewal_skipped(int(row["id"]), "Получатель относится к команде или исключённым контактам")
            return "skipped"
        if self.store.is_blacklisted(peer_id):
            self.store.mark_renewal_skipped(int(row["id"]), "Получатель находится в blacklist")
            return "skipped"
        stop_words = [normalized(item) for item in self.store.active_stop_words()]
        permanent_opt_outs = [normalized(item) for item in BLACKLIST_WORDS]
        index = 0
        async for message in client.iter_messages(peer_id, limit=None):
            if not message.out:
                text = normalized(message.message or "")
                phrases = permanent_opt_outs if index >= 100 else stop_words
                phrase = next((item for item in phrases if item and item in text), None)
                if phrase:
                    self.store.add_blacklist(peer_id, f"Продления: клиент написал «{phrase}»")
                    self.store.mark_renewal_skipped(int(row["id"]), f"Клиент попросил не писать: «{phrase}»")
                    return "skipped"
            index += 1
        return "ready"

    async def _send_verified_private(self, client, row, template: str,
                                     *, excluded_peer_ids: set[int] | None = None,
                                     excluded_usernames: set[str] | None = None) -> tuple[str, int | None]:
        checked = await self._check_private_recipient(
            client, row, excluded_peer_ids=excluded_peer_ids,
            excluded_usernames=excluded_usernames)
        if checked != "ready":
            return checked, None
        peer_id = int(row["target_peer_id"])
        sent = await client.send_message(peer_id, template)
        self.store.mark_renewal_sent(int(row["id"]), int(sent.id), template)
        return "sent", int(sent.id)

    async def deliver_selected_unified(self, followup_id: int, template: str,
                                       *, excluded_peer_ids: set[int] | None = None,
                                       excluded_usernames: set[str] | None = None) -> tuple[str, int | None]:
        """Send one coordinator-reserved offer through Activity Center's checks."""
        row = self.store.renewal_followup(followup_id)
        if (self.store.setting("unified_lost_enabled") != "1" or self.store.setting("global_paused") == "1" or not row
                or not row["source_key"].startswith("unified:") or row["status"] != "ready"
                or not row["account_id"] or not row["target_peer_id"]
                or int(row["target_peer_id"]) in (excluded_peer_ids or ())
                or (row["target_username"] or "").casefold() in (excluded_usernames or ())):
            return "blocked", None
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            return "error", None
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return "error", None
            return await self._send_verified_private(
                client,row,template,excluded_peer_ids=excluded_peer_ids,
                excluded_usernames=excluded_usernames)
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _deliver_one_locked(self) -> tuple[str, int | None]:
        if self.store.setting("global_paused") == "1" or self.store.setting("renewal_delivery_enabled") != "1":
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
        timezone_name = self.store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        local_now = datetime.now(ZoneInfo(timezone_name))
        work_start = int(self.store.setting("renewal_work_start_hour", "8") or "8")
        work_end = int(self.store.setting("renewal_work_end_hour", "18") or "18")
        if not 0 <= work_start < work_end <= 24:
            return "outside_hours", None
        if not work_start <= local_now.hour < work_end:
            return "outside_hours", None
        daily_limit = int(self.store.setting("renewal_max_per_day", "5") or "5")
        if daily_limit > 0 and self.store.renewal_sent_today(timezone_name) >= daily_limit:
            return "daily_limit", None
        last_sent = self.store.last_renewal_sent_at()
        minimum_delay = int(self.store.setting("renewal_delay_seconds", "2400") or "2400")
        maximum_delay = max(minimum_delay, int(self.store.setting("renewal_max_delay_seconds", str(minimum_delay)) or minimum_delay))
        next_allowed_raw = self.store.setting("renewal_next_delivery_at")
        try:
            next_allowed = datetime.fromisoformat(next_allowed_raw) if next_allowed_raw else None
        except ValueError:
            next_allowed = None
        if next_allowed and datetime.now(timezone.utc) < next_allowed.astimezone(timezone.utc):
            return "waiting", None
        if last_sent and not next_allowed and datetime.now(timezone.utc) - last_sent.astimezone(timezone.utc) < timedelta(seconds=minimum_delay):
            return "waiting", None
        rows = self.store.pending_renewal_delivery(1)
        if not rows:
            return "empty", None
        # Re-read the sheet immediately before delivery. If it is unavailable,
        # do not send using an old color or an out-of-date project row.
        try:
            self.scan_sheet()
        except Exception:
            return "sheet_unavailable", None
        rows = self.store.pending_renewal_delivery(1)
        if not rows:
            return "empty", None
        row = rows[0]
        template = self.store.next_renewal_template(row["trigger_kind"])
        if not template:
            return "missing_template", int(row["id"])
        client = self.telegram.client_for_account(int(row["account_id"]))
        if client is None:
            self.store.mark_renewal_error(int(row["id"]), "Не удалось открыть аккаунт отправителя")
            return "error", int(row["id"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.store.mark_renewal_error(int(row["id"]), "Аккаунт отправителя не авторизован")
                return "error", int(row["id"])
            outcome, _ = await self._send_verified_private(client, row, template)
            if outcome != "sent":
                return outcome, int(row["id"])
            wait_seconds = random.randint(minimum_delay, maximum_delay)
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)).isoformat()
            self.store.set_setting("renewal_next_delivery_at", next_at)
            self.store.set_setting("local_traffic_next_at", next_at)
            self.store.audit("renewal_followup_sent", f"{row['trigger_kind']}: {row['project_name']}", int(row["account_id"]), int(row["target_peer_id"]))
            return "sent", int(row["id"])
        except FloodWaitError as exc:
            self.store.mark_renewal_error(int(row["id"]), f"Telegram попросил подождать {exc.seconds} секунд")
            return "error", int(row["id"])
        except (TimeoutError, OSError, RPCError) as exc:
            self.store.mark_renewal_error(int(row["id"]), f"Ошибка отправки: {exc.__class__.__name__}")
            return "error", int(row["id"])
        finally:
            if client.is_connected():
                await client.disconnect()
