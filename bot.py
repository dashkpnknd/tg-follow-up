from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from html import escape
import logging
import os
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon.errors import FloodWaitError

from customer_checkins import CheckinSettings, CustomerCheckinService
from renewal_followups import RenewalFollowupService
from unified_lost_campaign import UnifiedLostCampaign
from active_customer_campaign import ActiveCustomerCampaign
from store import Store, project_chat_match_score, project_chat_tokens
from telegram_service import FollowupService, TelegramSettings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("followup")
ADMINS = {int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()}
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.getenv("ADMIN_CHAT_ID") else None
ACCESS_MODE = os.getenv("ACCESS_MODE", "group_admins").strip().casefold()
store = Store(os.getenv("DATABASE_PATH", "data/followup.sqlite3"))
service = FollowupService(store, TelegramSettings(int(os.environ["API_ID"]), os.environ["API_HASH"], int(os.getenv("HISTORY_LIMIT", "100")), float(os.getenv("SCAN_HISTORY_PAUSE_SECONDS", "10"))))
checkin_service = CustomerCheckinService(store, service, CheckinSettings.from_environment())
renewal_service = RenewalFollowupService.from_environment(store, service)
unified_lost_service = UnifiedLostCampaign(store, service, renewal_service)
active_customer_service = ActiveCustomerCampaign(
    store, service, source_url=checkin_service.settings.sales_csv_url,
    sender_title=checkin_service.settings.sender_account_title)
dp = Dispatcher()
report_bot: Bot | None = None
# Both customer campaigns use the same Local Traffic Telegram session.  Keep
# their dialog/history work serial: concurrent GetDialogs requests cause
# Telegram flood waits and made restarts look like missed scans.
campaign_telegram_lock = asyncio.Lock()
REPORT_CARD_INTERVAL_SECONDS = float(os.getenv("REPORT_CARD_INTERVAL_SECONDS", "3.2"))


class Flow(StatesGroup):
    stop_word = State()
    remove_stop_word = State()
    template = State()
    task_name = State()
    task_sample = State()
    limit = State()
    delay = State()
    hours = State()
    blacklist = State()
    checkin_template = State()
    checkin_username = State()
    checkin_limit = State()
    checkin_account_phone = State()
    checkin_account_code = State()
    checkin_account_password = State()
    renewal_red_template = State()
    renewal_white_template = State()
    renewal_username = State()
    renewal_white_day = State()
    active_customer_chat_text = State()
    active_customer_private_text = State()
    active_customer_map_row = State()


def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows])


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Аккаунты", callback_data="accounts"), InlineKeyboardButton(text="💬 Диалоги", callback_data="dialogs")],
        [InlineKeyboardButton(text="🎯 Кандидаты", callback_data="candidates"), InlineKeyboardButton(text="📬 Очередь и задачи", callback_data="queue")],
        [InlineKeyboardButton(text="⛔ Исключения", callback_data="exceptions"), InlineKeyboardButton(text="🛑 Стоп-слова", callback_data="stop_words")],
        [InlineKeyboardButton(text="✉️ Шаблон", callback_data="template"), InlineKeyboardButton(text="📊 Статистика", callback_data="statistics")],
        [InlineKeyboardButton(text="📅 Клиенты через 14 дней", callback_data="checkins")],
        [InlineKeyboardButton(text="💬 Активные клиенты · все месяцы", callback_data="active_customers")],
        [InlineKeyboardButton(text="🔄 Продления", callback_data="renewals")],
        [InlineKeyboardButton(text="📣 Отчёты", callback_data="reports"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="🧾 Логи", callback_data="logs")],
        [InlineKeyboardButton(text="🚨 ОСТАНОВИТЬ ВСЁ", callback_data="emergency_stop")],
    ])


async def is_allowed(event: Message | CallbackQuery) -> bool:
    if ACCESS_MODE == "public":
        return True
    user = event.from_user
    if not user:
        return False
    if user.id in ADMINS:
        return True
    message = event.message if isinstance(event, CallbackQuery) else event
    event_chat_id = getattr(getattr(message, "chat", None), "id", None)
    operator_chat_ids = {
        int(chat_id) for chat_id in (
            store.setting("checkin_errors_chat_id"),
            store.setting("renewal_errors_chat_id"),
        ) if chat_id
    }
    # A card's own work chat grants its administrators access to that card.
    # The main admin group continues to grant access throughout the bot.
    chat_ids = [ADMIN_CHAT_ID] if ADMIN_CHAT_ID else []
    if event_chat_id in operator_chat_ids and event_chat_id not in chat_ids:
        chat_ids.append(event_chat_id)
    for chat_id in chat_ids:
        try:
            member = await event.bot.get_chat_member(chat_id, user.id)
            if member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
                return True
        except Exception:
            log.warning("Could not verify group admin %s in %s", user.id, chat_id, exc_info=True)
    log.info("Access denied: user=%s event_chat=%s checked_chats=%s", user.id, event_chat_id, chat_ids)
    return False


async def reject_if_needed(event: Message | CallbackQuery) -> bool:
    if await is_allowed(event):
        return False
    text = "Нет доступа для этого Telegram-аккаунта. Нужны права администратора группы карточки или управляющей группы бота."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return True


async def send_menu(message: Message, text: str = "Панель управления дожимами") -> None:
    await message.answer(text, reply_markup=main_menu())


async def send_report(text: str) -> None:
    """Post operational reports only to the explicitly configured group."""
    chat_id = store.setting("report_chat_id")
    if not chat_id or report_bot is None:
        return
    try:
        await report_bot.send_message(int(chat_id), text)
    except Exception:
        log.exception("Could not deliver report to chat %s", chat_id)


async def delete_operator_card(chat_id: int, message_id: int, bot: Bot) -> None:
    """Remove an acted-on review card without failing the operator action."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # The operator can delete cards manually too.  In that case the
        # selection is still valid and must not be rolled back.
        log.info("Operator card %s/%s was already unavailable for deletion", chat_id, message_id)


def scan_report(totals: dict[str, int]) -> str:
    statuses = store.candidate_summary()
    queue_data = store.queue_summary()
    lines = [
        "🔎 Отчёт анализа диалогов",
        f"За цикл обработано: {sum(totals.values())}",
        f"Новых/подходящих кандидатов: {totals.get('candidate', 0)}",
        f"Всего кандидатов в очереди: {queue_data.get('pending', 0)}",
        f"Диалогов с ответом: {statuses.get('replied', 0)}",
        f"Заявок исключено: {statuses.get('application', 0)}",
        f"Ожидают 48 часов: {statuses.get('too_fresh', 0)}",
    ]
    if totals.get("history_error") or totals.get("error"):
        lines.append(f"Ошибок чтения: {totals.get('history_error', 0) + totals.get('error', 0)}")
    return "\n".join(lines)


async def send_delivery_report() -> None:
    last_sent = store.latest_sent()
    if not last_sent:
        return
    account_title = last_sent["title"] or "неизвестный аккаунт"
    total_sent = store.sent_today_total(store.setting("timezone", "Europe/Moscow") or "Europe/Moscow")
    await send_report(
        f"✉️ Отправлен дожим: «{last_sent['sent_template']}»\n"
        f"Аккаунт: {account_title}\n"
        f"Сегодня отправлено со всех аккаунтов: {total_sent}"
    )


def card_group_url(username: str | None, group_id: int | None, links: dict[int, str]) -> str | None:
    username = (username or "").lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}"
    return links.get(int(group_id)) if group_id else None


async def card_group_links(account_id: int | None, groups: list[tuple[int | None, str | None]]) -> dict[int, str]:
    """Use an existing message for private Telegram links; never emit t.me/c/<id> alone."""
    links: dict[int, str] = {}
    private_ids = {
        int(group_id) for group_id, username in groups
        if group_id and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", (username or "").lstrip("@"))
    }
    if not private_ids or not account_id:
        return links
    client = service.client_for_account(int(account_id))
    if client is None:
        return links
    async with campaign_telegram_lock:
        try:
            await client.connect()
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                group_id = int(getattr(entity, "id", 0))
                if group_id not in private_ids:
                    continue
                messages = await client.get_messages(entity, limit=1)
                if messages:
                    links[group_id] = f"https://t.me/c/{group_id}/{int(messages[0].id)}"
                if len(links) == len(private_ids):
                    break
        except Exception:
            log.exception("Could not build private group links for account %s", account_id)
        finally:
            if client.is_connected():
                await client.disconnect()
    return links


def checkin_card(row, group_links: dict[int, str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """One actionable operator card for an ambiguous customer check-in."""
    candidates = [
        candidate for candidate in store.checkin_candidates(int(row["id"]))
        if not checkin_service.is_excluded_contact(candidate["username"], candidate["name"])
    ]
    group_candidates = store.checkin_group_candidates(int(row["id"]))
    group_links = group_links or {}
    group_title = row["group_title"]
    group_url = card_group_url(row["group_username"], row["group_peer_id"], group_links)
    lines = [
        "<b>⚠️ НУЖНА ПОМОЩЬ · 14 ДНЕЙ ПОСЛЕ ПРОДАЖИ</b>",
        "",
        f"<b>Проект:</b> {escape(row['project_name'])}",
        f"<b>Продажа:</b> {escape(row['sale_date'])}; сумма: {row['amount']}",
        f"<b>Причина:</b> {escape(row['reason'])}",
    ]
    if group_title:
        safe_title = escape(group_title)
        lines.append(f"<b>Чат:</b> <a href=\"{group_url}\">{safe_title}</a>" if group_url else f"<b>Чат:</b> {safe_title}")
    elif group_candidates:
        lines.append("<b>Подходящие чаты:</b>")
        for index, group in enumerate(group_candidates[:8], start=1):
            title = escape(group["title"] or "Чат без названия")
            username = (group["username"] or "").lstrip("@")
            url = card_group_url(username, group["peer_id"], group_links)
            lines.append(f"{index}. <a href=\"{url}\">{title}</a>" if url else f"{index}. {title}")
    if candidates:
        lines.append("<b>Люди с личным диалогом Local Traffic:</b>")
        for candidate in candidates[:8]:
            username = (candidate["username"] or "").lstrip("@")
            name = candidate["name"] or "—"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                lines.append(f"• <a href=\"https://t.me/{username}\">@{escape(username)}</a> — {escape(name)}")
            else:
                lines.append(f"• {escape(name)}")
    buttons: list[list[InlineKeyboardButton]] = []
    if group_url:
        buttons.append([InlineKeyboardButton(text="Перейти в чат магазина", url=group_url)])
    for index, group in enumerate(group_candidates[:8], start=1):
        username = (group["username"] or "").lstrip("@")
        url = card_group_url(username, group["peer_id"], group_links)
        if url:
            buttons.append([InlineKeyboardButton(text=f"Открыть чат {index}", url=url)])
        buttons.append([InlineKeyboardButton(text=f"Выбрать чат {index}", callback_data=f"checkin_choose_group:{row['id']}:{group['peer_id']}")])
    for candidate in candidates[:8]:
        username = (candidate["username"] or "").lstrip("@")
        label = f"@{username}" if username else (candidate["name"] or str(candidate["peer_id"]))
        buttons.append([InlineKeyboardButton(text=f"Выбрать юзера: {label}"[:64], callback_data=f"checkin_choose:{row['id']}:{candidate['peer_id']}")])
    buttons.extend([
        [InlineKeyboardButton(text="Ввести @username", callback_data=f"checkin_username:{row['id']}")],
        [InlineKeyboardButton(text="Пропустить проект", callback_data=f"checkin_skip:{row['id']}")],
        [InlineKeyboardButton(text="Больше не писать этому проекту", callback_data=f"checkin_block:{row['id']}")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def checkin_preview_cards() -> list[tuple[str, InlineKeyboardMarkup]]:
    """Inert examples for checking the operator-card layout.

    Preview callbacks are deliberately separate from real check-in callbacks:
    they never read or modify the sales/check-in ledger.
    """
    person_card = (
        "<b>ТЕСТ — карточка не влияет на рассылку</b>\n"
        "<b>ПРОВЕРКА КЛИЕНТА</b>\n"
        "<b>Проект:</b> Тестовый магазин ВК\n"
        "<b>Продажа:</b> 20 октября 2026; сумма: 25 000 ₽\n"
        "<b>Причина:</b> Подходящих участников с личной перепиской: 2\n"
        "<b>Чат:</b> Тестовый магазин | ВК\n"
        "<b>Люди с личным диалогом Local Traffic:</b>\n"
        "• @test_alex — Алексей\n"
        "• @test_maria — Мария",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть чат магазина — тест", callback_data="checkin_preview:open_chat")],
            [InlineKeyboardButton(text="Выбрать: @test_alex", callback_data="checkin_preview:choose_person")],
            [InlineKeyboardButton(text="Выбрать: @test_maria", callback_data="checkin_preview:choose_person")],
            [InlineKeyboardButton(text="Ввести @username", callback_data="checkin_preview:username")],
            [InlineKeyboardButton(text="Пропустить проект", callback_data="checkin_preview:skip")],
            [InlineKeyboardButton(text="Больше не писать этому проекту", callback_data="checkin_preview:block")],
        ]),
    )
    group_card = (
        "<b>ТЕСТ — карточка не влияет на рассылку</b>\n"
        "<b>Найдено несколько чатов</b>\n"
        "<b>Проект:</b> 9:41 Новосибирск ВК\n"
        "<b>Причина:</b> Найдено чатов с таким названием: 2\n\n"
        "1. 9:41 | Новосибирск | ВК\n"
        "2. 9:41 | Новосибирск | ВК",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть чат 1 — тест", callback_data="checkin_preview:open_group")],
            [InlineKeyboardButton(text="Выбрать чат 1", callback_data="checkin_preview:choose_group")],
            [InlineKeyboardButton(text="Открыть чат 2 — тест", callback_data="checkin_preview:open_group")],
            [InlineKeyboardButton(text="Выбрать чат 2", callback_data="checkin_preview:choose_group")],
            [InlineKeyboardButton(text="Пропустить проект", callback_data="checkin_preview:skip")],
            [InlineKeyboardButton(text="Больше не писать этому проекту", callback_data="checkin_preview:block")],
        ]),
    )
    report_card = (
        "<b>ТЕСТ — отчёт не означает отправку</b>\n"
        "<b>СООБЩЕНИЕ ОТПРАВЛЕНО</b>\n"
        "<b>Проект:</b> Тестовый магазин ВК\n"
        "<b>С продажи прошло:</b> 14 дн.\n"
        "<b>Чат:</b> Тестовый магазин | ВК\n"
        "<b>Кому:</b> @test_alex — Алексей\n"
        "<b>Текст:</b> Приветствую) Решил лично написать узнать, как продвигается работа по трафику?",
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    return [person_card, group_card, report_card]


async def send_checkin_exceptions() -> int:
    """Deliver unresolved cases only once, and only to the chosen work chat."""
    if store.setting("operator_cards_deferred", "0") == "1":
        return 0
    chat_id = store.setting("checkin_errors_chat_id")
    if not chat_id or report_bot is None:
        return 0
    sent = 0
    for row in store.checkin_exceptions_to_report():
        group_candidates = store.checkin_group_candidates(int(row["id"]))
        group_specs = [(row["group_peer_id"], row["group_username"])] + [
            (group["peer_id"], group["username"]) for group in group_candidates
        ]
        group_links = await card_group_links(row["account_id"], group_specs)
        text, markup = checkin_card(row, group_links)
        try:
            await report_bot.send_message(int(chat_id), text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
            store.mark_checkin_reported(int(row["id"]))
            sent += 1
            # Telegram limits messages in a group.  Spacing cards keeps a
            # large batch from being cut off after the first twenty items.
            await asyncio.sleep(REPORT_CARD_INTERVAL_SECONDS)
        except Exception:
            log.exception("Could not deliver customer check-in exception to %s", chat_id)
            break
    return sent


def checkin_sent_report(row) -> str:
    """A compact, self-contained report for an actual customer message."""
    timezone_name = store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
    try:
        days_since_sale = max(0, (datetime.now(ZoneInfo(timezone_name)).date() - date.fromisoformat(row["sale_date"])).days)
    except (ValueError, TypeError):
        days_since_sale = "—"
    recipient = f"@{row['target_username']}" if row["target_username"] else (row["target_name"] or str(row["target_peer_id"]))
    template = row["sent_template"] or "—"
    return (
        "<b>СООБЩЕНИЕ ОТПРАВЛЕНО</b>\n"
        f"<b>Проект:</b> {escape(row['project_name'])}\n"
        f"<b>С продажи прошло:</b> {days_since_sale} дн.\n"
        f"<b>Кому:</b> {escape(recipient)}\n"
        f"<b>Текст:</b> {escape(template)}"
    )


async def send_checkin_sent_report(row) -> None:
    """Post actual 14-day delivery reports to the same operator chat."""
    chat_id = store.setting("checkin_errors_chat_id")
    if not chat_id or report_bot is None:
        return
    try:
        await report_bot.send_message(
            int(chat_id), checkin_sent_report(row), parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Could not deliver customer check-in sent report to %s", chat_id)


async def send_renewal_sent_report(row) -> None:
    """Post actual renewal delivery reports to the renewal operator chat."""
    chat_id = store.setting("renewal_errors_chat_id")
    if not chat_id or report_bot is None:
        return
    recipient = f"@{row['target_username']}" if row["target_username"] else (row["target_name"] or str(row["target_peer_id"]))
    template = row["sent_template"] or "—"
    text = (
        "<b>СООБЩЕНИЕ ОТПРАВЛЕНО</b>\n"
        f"<b>Проект:</b> {escape(row['project_name'])}\n"
        f"<b>Кому:</b> {escape(recipient)}\n"
        f"<b>Текст:</b> {escape(template)}"
    )
    try:
        await report_bot.send_message(
            int(chat_id), text, parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Could not deliver renewal sent report to %s", chat_id)


async def run_checkin_scan() -> dict[str, int]:
    configured, _ = checkin_service.configured()
    if not configured:
        return {"not_configured": 1}
    timezone_name = store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
    today = datetime.now(ZoneInfo(timezone_name)).date()
    previous = store.setting("checkin_last_successful_scan_on", "") or ""
    try:
        previous_date = date.fromisoformat(previous)
    except ValueError:
        previous_date = None
    # On an established installation, fill every due day after the last
    # successful scan.  The first ever run preserves the cautious one-day
    # grace window rather than unexpectedly importing historical sales.
    due_on_or_after = (
        previous_date + timedelta(days=1)
        if previous_date and previous_date < today
        else today - timedelta(days=checkin_service.settings.due_grace_days)
    )
    imported = checkin_service.import_due_sales(today, due_on_or_after)
    async with campaign_telegram_lock:
        resolved = await checkin_service.resolve_new_checkins()
    await send_checkin_exceptions()
    store.set_setting("checkin_last_successful_scan_on", today.isoformat())
    return {**imported, **{f"resolve_{key}": value for key, value in resolved.items()}}


def renewal_card(row, group_links: dict[int, str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    candidates = [
        candidate for candidate in store.renewal_candidates(int(row["id"]))
        if not renewal_service.is_excluded_contact(candidate["username"], candidate["name"])
    ]
    group_candidates = store.renewal_group_candidates(int(row["id"]))
    unified = row["source_key"].startswith("unified:")
    color = "красная" if row["trigger_kind"] == "red" else "белая"
    lines = [
        "<b>⚠️ НУЖНА ПОМОЩЬ · СЛИВШИЕСЯ</b>" if unified else "<b>⚠️ НУЖНА ПОМОЩЬ · ПРОДЛЕНИЕ</b>",
        "",
        f"<b>Проект:</b> {escape(row['project_name'])}",
        f"<b>Причина:</b> {escape(row['reason'])}",
    ]
    if unified:
        lines.insert(3, "<b>Основание:</b> 10 дней тишины или актуальная красная строка")
    else:
        lines.insert(3, f"<b>Таблица:</b> {escape(row['sheet_name'])}, строка {row['row_number']} · {color} строка")
        lines.insert(4, f"<b>Канал:</b> {escape(row['channel'] or 'не указан')}")
    if row["reason"] == "Нет участника с личной перепиской":
        lines.append("Среди участников группы нет клиента с личным диалогом Local Traffic. Укажите @username, если такой диалог есть; без него отправка невозможна.")
    group_links = group_links or {}
    group_url = card_group_url(row["group_username"], row["group_peer_id"], group_links)
    if row["group_title"]:
        title = escape(row["group_title"])
        lines.append(f"Чат: <a href=\"{group_url}\">{title}</a>" if group_url else f"Чат: {title}")
    elif group_candidates:
        lines.append("Подходящие чаты:")
        for index, group in enumerate(group_candidates[:8], start=1):
            username = (group["username"] or "").lstrip("@")
            url = card_group_url(username, group["peer_id"], group_links)
            title = escape(group["title"] or "Чат без названия")
            lines.append(f"{index}. <a href=\"{url}\">{title}</a>" if url else f"{index}. {title}")
    if candidates:
        lines.append("Люди с личным диалогом Local Traffic:")
        for candidate in candidates[:8]:
            username = (candidate["username"] or "").lstrip("@")
            name = candidate["name"] or "—"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                lines.append(f"• <a href=\"https://t.me/{username}\">@{escape(username)}</a> — {escape(name)}")
            else:
                lines.append(f"• {escape(name)}")
    buttons: list[list[InlineKeyboardButton]] = []
    if group_url:
        buttons.append([InlineKeyboardButton(text="Перейти в чат магазина", url=group_url)])
    for index, group in enumerate(group_candidates[:8], start=1):
        username = (group["username"] or "").lstrip("@")
        url = card_group_url(username, group["peer_id"], group_links)
        if url:
            buttons.append([InlineKeyboardButton(text=f"Открыть чат {index}", url=url)])
        buttons.append([InlineKeyboardButton(text=f"Выбрать чат {index}", callback_data=f"renewal_choose_group:{row['id']}:{group['peer_id']}")])
    for candidate in candidates[:8]:
        username = (candidate["username"] or "").lstrip("@")
        label = f"@{username}" if username else (candidate["name"] or str(candidate["peer_id"]))
        buttons.append([InlineKeyboardButton(text=f"Выбрать юзера: {label}"[:64], callback_data=f"renewal_choose:{row['id']}:{candidate['peer_id']}")])
    buttons.extend([
        [InlineKeyboardButton(text="Ввести @username", callback_data=f"renewal_username:{row['id']}")],
        [InlineKeyboardButton(text="Пропустить проект", callback_data=f"renewal_skip:{row['id']}")],
        [InlineKeyboardButton(text="Не писать больше", callback_data=f"renewal_block:{row['id']}")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_renewal_exceptions() -> int:
    if store.setting("operator_cards_deferred", "0") == "1":
        return 0
    chat_id = store.setting("renewal_errors_chat_id")
    if not chat_id or report_bot is None:
        return 0
    sent = 0
    for row in store.renewal_exceptions_to_report():
        group_candidates = store.renewal_group_candidates(int(row["id"]))
        group_specs = [(row["group_peer_id"], row["group_username"])] + [
            (group["peer_id"], group["username"]) for group in group_candidates
        ]
        group_links = await card_group_links(row["account_id"], group_specs)
        text, markup = renewal_card(row, group_links)
        try:
            await report_bot.send_message(int(chat_id), text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
            store.mark_renewal_reported(int(row["id"]))
            sent += 1
            await asyncio.sleep(REPORT_CARD_INTERVAL_SECONDS)
        except Exception:
            log.exception("Could not deliver renewal exception to %s", chat_id)
            break
    return sent


async def run_renewal_scan() -> dict[str, int]:
    configured, _ = renewal_service.configured()
    if not configured:
        return {"not_configured": 1}
    scanned = renewal_service.scan_sheet()
    async with campaign_telegram_lock:
        resolved = await renewal_service.resolve_new_followups()
    await send_renewal_exceptions()
    return {**scanned, **{f"resolve_{key}": value for key, value in resolved.items()}}


@dp.message(CommandStart())
async def start(message: Message):
    if ACCESS_MODE != "public" and not ADMIN_CHAT_ID and not ADMINS:
        await message.answer("Бот запущен, но доступ к управлению ещё не настроен. Добавьте бота администратором в управляющую группу и передайте её числовой ID — тогда доступ автоматически получат все администраторы группы.")
        return
    if await reject_if_needed(message): return
    await send_menu(message, "Бот готов. Отправка выключена и поставлена на глобальную паузу.")


@dp.message(F.text.startswith("/reports_here"))
@dp.channel_post(F.text.startswith("/reports_here"))
async def set_reports_here(message: Message):
    if await reject_if_needed(message): return
    if str(message.chat.type) not in {"group", "supergroup", "channel"}:
        await message.answer("Добавьте меня в отдельную группу или канал для отчётов и отправьте там команду /reports_here.")
        return
    store.set_setting("report_chat_id", str(message.chat.id))
    store.audit("report_chat_configured", f"Чат отчётов настроен: {message.chat.id}")
    await message.answer("✅ Эта группа назначена для отчётов по анализу и каждой отправке.")
    await send_report(scan_report({}))


@dp.message(F.text.startswith("/reports_off"))
@dp.channel_post(F.text.startswith("/reports_off"))
async def disable_reports(message: Message):
    if await reject_if_needed(message): return
    if store.setting("report_chat_id") == str(message.chat.id):
        store.set_setting("report_chat_id", "")
        store.audit("report_chat_disabled", "Отчёты отключены")
        await message.answer("Отчёты в эту группу отключены.")
    else:
        await message.answer("Эта группа не назначена для отчётов.")


@dp.message(F.text.startswith("/checkin_errors_here"))
@dp.channel_post(F.text.startswith("/checkin_errors_here"))
async def set_checkin_errors_here(message: Message):
    if await reject_if_needed(message): return
    if str(message.chat.type) not in {"group", "supergroup", "channel"}:
        await message.answer("Добавьте бота в отдельную группу для ошибок и отправьте там команду /checkin_errors_here.")
        return
    store.set_setting("checkin_errors_chat_id", str(message.chat.id))
    store.audit("checkin_errors_chat_configured", f"Чат ошибок: {message.chat.id}")
    count = await send_checkin_exceptions()
    await message.answer(f"Группа назначена для ошибок по клиентам через 14 дней. Отправлено ожидающих карточек: {count}.")


@dp.callback_query(F.data.startswith("checkin_preview:"))
async def checkin_preview_action(query: CallbackQuery):
    """Acknowledge preview clicks without treating them as operator decisions."""
    if await reject_if_needed(query): return
    await query.answer("Это тестовая карточка: выбор не сохранён, рассылка не изменена.", show_alert=True)


@dp.message(F.text.startswith("/renewal_errors_here"))
@dp.channel_post(F.text.startswith("/renewal_errors_here"))
async def set_renewal_errors_here(message: Message):
    if await reject_if_needed(message): return
    if str(message.chat.type) not in {"group", "supergroup", "channel"}:
        await message.answer("Добавьте бота в отдельную группу для ошибок продлений и отправьте там команду /renewal_errors_here.")
        return
    store.set_setting("renewal_errors_chat_id", str(message.chat.id))
    store.audit("renewal_errors_chat_configured", f"Чат ошибок: {message.chat.id}")
    count = await send_renewal_exceptions()
    await message.answer(f"Группа назначена для ошибок продлений. Отправлено ожидающих карточек: {count}.")


@dp.callback_query(F.data == "accounts")
async def accounts(query: CallbackQuery):
    if await reject_if_needed(query): return
    rows = store.accounts()
    labels = [f"• {row['title'] or row['session_name']} — {'доступен для отправки' if row['send_status'] != 'unavailable' else 'исключён после ошибки'}, проверка сессии: {row['auth_status']}" for row in rows]
    text = f"👥 Аккаунты Даниила: {len(rows)}\n" + ("\n".join(labels[:30]) if labels else "Ещё не импортированы.")
    await query.message.answer(text, reply_markup=kb(("🔄 Обновить из DialogHub", "import"), ("🔎 Анализировать все", "scan"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data == "import")
async def import_accounts(query: CallbackQuery):
    if await reject_if_needed(query): return
    try:
        count = service.import_dialoghub_accounts(os.environ["DIALOGHUB_DB_PATH"], os.environ["ACCOUNT_SESSIONS_DIR"], os.getenv("ACCOUNT_TITLE_PREFIX", "Даниил"))
        await query.message.answer(f"✅ Добавлено/обновлено аккаунтов Даниила: {count}. Импорт не открывает и не использует Telegram-сессии.")
    except Exception:
        log.exception("DialogHub import failed")
        await query.message.answer("⚠️ Не удалось импортировать аккаунты. Проверьте пути DialogHub.")
    await query.answer()


@dp.callback_query(F.data == "scan")
async def scan(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.answer("Анализ запущен")
    await query.message.answer("🔎 Анализирую все диалоги. Отправка остаётся выключенной.")

    async def run():
        totals = await service.scan_all(os.getenv("DIALOGHUB_DB_PATH"))
        text = "\n".join(f"{status}: {count}" for status, count in sorted(totals.items())) or "Нет доступных диалогов"
        await query.message.answer(f"✅ Анализ завершён:\n{text}")
        await send_report(scan_report(totals))
    asyncio.create_task(run())


@dp.callback_query(F.data.in_({"dialogs", "candidates"}))
async def candidates(query: CallbackQuery):
    if await reject_if_needed(query): return
    summary = store.candidate_summary()
    names = {"candidate": "Можно дожимать", "application": "Есть заявка", "refusal": "Явный отказ", "replied": "Клиент ответил", "followup_sent": "Дожим уже был", "too_fresh": "Меньше 48 часов", "blacklisted": "Blacklist", "no_outbound": "Нет исходящего", "undetermined": "Неопределённые"}
    lines = [f"{names.get(key, key)}: {value}" for key, value in sorted(summary.items())]
    title = "💬 Статусы диалогов" if query.data == "dialogs" else "🎯 Предпросмотр кандидатов"
    await query.message.answer(title + ":\n" + ("\n".join(lines) if lines else "Сначала запустите анализ."), reply_markup=kb(("➕ Создать задачу из кандидатов", "task_new"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data == "queue")
async def queue(query: CallbackQuery):
    if await reject_if_needed(query): return
    summary, tasks = store.queue_summary(), store.tasks()
    lines = [f"{status}: {count}" for status, count in sorted(summary.items())] or ["Очередь пуста"]
    lines += ["", *[f"#{row['id']} {row['name']} — {'включена' if row['enabled'] else 'черновик'}, в очереди: {row['queued'] or 0}, отправлено: {row['sent'] or 0}; автодобавление после 48 ч: {'да' if row['auto_include_new'] else 'нет'}" for row in tasks[:15]]]
    buttons = [("➕ Новая задача", "task_new"), *((f"▶️ Запустить #{row['id']}", f"task_enable:{row['id']}") for row in tasks if not row["enabled"]), ("◀️ Меню", "menu")]
    await query.message.answer("📬 Очередь и задачи:\n" + "\n".join(lines), reply_markup=kb(*buttons))
    await query.answer()


@dp.callback_query(F.data == "task_new")
async def task_new(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.task_name)
    await query.message.answer("Введите название задачи. Затем бот попросит размер тестовой выборки.")
    await query.answer()


@dp.message(Flow.task_name)
async def task_name(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не должно быть пустым.")
        return
    await state.update_data(task_name=name)
    await state.set_state(Flow.task_sample)
    await message.answer("Введите общее число кандидатов для задачи или `все` для полной текущей базы.", parse_mode="Markdown")


@dp.message(Flow.task_sample)
async def task_sample(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    raw = (message.text or "").strip().casefold()
    try:
        sample_limit = 0 if raw in {"все", "all"} else int(raw)
        if not 0 <= sample_limit <= 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число или слово «все».")
        return
    data = await state.get_data()
    task_id, selected = store.create_task(data["task_name"], sample_limit)
    scope = "полной базы" if sample_limit == 0 else f"теста: до {sample_limit} всего"
    store.audit("task_created", f"Создана задача #{task_id}: {data['task_name']}; {scope}; выбрано {selected}")
    await state.clear()
    await message.answer(f"✅ Задача #{task_id} создана как черновик. Выбрано кандидатов: {selected} ({scope}). После включения новые подходящие диалоги будут автоматически добавляться в неё, когда пройдёт 48 часов. Проверьте её в разделе «Очередь и задачи».", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("task_enable:"))
async def task_enable(query: CallbackQuery):
    if await reject_if_needed(query): return
    task_id = int(query.data.split(":", 1)[1])
    if store.setting("global_paused") == "1" or store.setting("delivery_enabled") != "1":
        await query.message.answer("Задача не включена: сначала в «Настройках» нужно отдельно подтвердить доступ к отправке и снять глобальную паузу.")
    elif store.set_task_enabled(task_id, True):
        store.audit("task_enabled", f"Задача #{task_id} включена")
        await query.message.answer(f"▶️ Задача #{task_id} включена. Перед каждым сообщением диалог будет проверен повторно; новые молчуны будут добавляться автоматически после 48 часов.")
    else:
        await query.message.answer("Задача не найдена.")
    await query.answer()


@dp.callback_query(F.data == "exceptions")
async def exceptions(query: CallbackQuery):
    if await reject_if_needed(query): return
    result = store.exception_summary()
    text = "⛔ Исключения:\n" + ("\n".join(f"{key}: {value}" for key, value in result.items()) if result else "Исключений пока нет.")
    await query.message.answer(text, reply_markup=kb(("Добавить в blacklist", "blacklist"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data == "blacklist")
async def blacklist(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.blacklist)
    await query.message.answer("Введите Telegram ID и причину через пробел. Пример: `123456789 не писать`", parse_mode="Markdown")
    await query.answer()


@dp.message(Flow.blacklist)
async def blacklist_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        peer_id_text, reason = (message.text or "").split(maxsplit=1)
        store.add_blacklist(int(peer_id_text), reason, message.from_user.id)
        store.audit("blacklist_added", reason, peer_id=int(peer_id_text))
        await message.answer("✅ Пользователь добавлен в общий blacklist.")
    except (ValueError, IndexError):
        await message.answer("Нужны ID и причина, например: `123456789 не писать`", parse_mode="Markdown")
        return
    await state.clear()


@dp.callback_query(F.data == "stop_words")
async def stop_words(query: CallbackQuery):
    if await reject_if_needed(query): return
    words = store.stop_words()
    lines = [f"• {row['phrase']} {'✅' if row['enabled'] else '⏸️'}" for row in words[:35]] or ["Список пуст"]
    await query.message.answer("🛑 Стоп-слова / отказы:\n" + "\n".join(lines), reply_markup=kb(("➕ Добавить", "stop_word_add"), ("🗑 Удалить", "stop_word_remove"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data.in_({"stop_word_add", "stop_word_remove"}))
async def stop_word_input(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    removing = query.data == "stop_word_remove"
    await state.set_state(Flow.remove_stop_word if removing else Flow.stop_word)
    await query.message.answer("Введите фразу для удаления." if removing else "Введите новую фразу отказа.")
    await query.answer()


@dp.message(Flow.stop_word)
async def stop_word_add(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        store.add_stop_word(message.text or "")
        store.audit("stop_word_added", message.text or "")
        await message.answer("✅ Стоп-фраза сохранена.")
        await state.clear()
    except ValueError:
        await message.answer("Фраза не должна быть пустой.")


@dp.message(Flow.remove_stop_word)
async def stop_word_remove(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    removed = store.remove_stop_word(message.text or "")
    await state.clear()
    await message.answer("✅ Стоп-фраза удалена." if removed else "Такая фраза не найдена.")


@dp.callback_query(F.data == "template")
async def template(query: CallbackQuery):
    if await reject_if_needed(query): return
    templates = "\n".join(f"• {text}" for text in store.followup_templates())
    await query.message.answer(f"✉️ Тексты дожима (выбираются случайно):\n{templates}", reply_markup=kb(("✏️ Изменить", "template_edit"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data == "template_edit")
async def template_edit(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.template)
    await query.message.answer("Пришлите один или несколько текстов: каждый вариант с новой строки. Они будут выбираться случайно для новых задач.")
    await query.answer()


@dp.message(Flow.template)
async def template_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не может быть пустым.")
        return
    templates = [line for line in text.splitlines() if line.strip()]
    try:
        store.set_followup_templates(templates)
    except ValueError:
        await message.answer("Нужен хотя бы один непустой текст.")
        return
    store.audit("template_changed", "Обновлено вариантов: " + str(len(templates)))
    await state.clear()
    await message.answer("✅ Тексты сохранены. Они будут применены случайно только к новым задачам.")


@dp.callback_query(F.data == "statistics")
async def statistics(query: CallbackQuery):
    if await reject_if_needed(query): return
    candidates, queue_data = store.candidate_summary(), store.queue_summary()
    await query.message.answer(f"📊 Статистика:\nОтправлено дожимов: {queue_data.get('sent', 0)}\nОтветов в обработанных диалогах: {candidates.get('replied', 0)}\nВ очереди: {queue_data.get('pending', 0)}\nОшибок: {queue_data.get('error', 0)}")
    await query.answer()


@dp.callback_query(F.data == "reports")
async def reports(query: CallbackQuery):
    if await reject_if_needed(query): return
    configured = bool(store.setting("report_chat_id"))
    text = (
        "📣 Отчёты\n"
        f"Отчётная группа: {'настроена' if configured else 'не настроена'}\n\n"
        "Создайте отдельную группу или канал, добавьте туда бота и отправьте в нём команду /reports_here. "
        "Туда будут поступать: итог каждого цикла анализа и отчёт о каждой отправке. "
        "Команда /reports_off в той же группе отключит отчёты."
    )
    await query.message.answer(text)
    await query.answer()


@dp.callback_query(F.data == "checkins")
async def checkins(query: CallbackQuery):
    if await reject_if_needed(query): return
    configured, reason = checkin_service.configured()
    summary = store.checkin_summary()
    status_text = ", ".join(f"{key}: {value}" for key, value in summary.items()) or "пока нет"
    minimum = int(store.setting("checkin_min_amount", "0") or "0")
    text = (
        "Клиенты через 14 дней\n"
        f"Настройка источника: {'готова' if configured else reason}\n"
        f"Аккаунт: {checkin_service.settings.sender_account_title or 'не задан'}\n"
        f"Минимальная сумма: {minimum if minimum else 'не задана'}\n"
        f"Вариантов текста: {len(store.checkin_templates())} (чередуются по порядку)\n"
        f"Отправка: {'включена' if store.setting('checkin_delivery_enabled') == '1' else 'выключена'}\n"
        f"Чат ошибок: {'настроен' if store.setting('checkin_errors_chat_id') else 'не настроен'}\n"
        f"Статусы: {status_text}"
    )
    await query.message.answer(text, reply_markup=kb(
        ("Подключить Local Traffic", "checkin_connect_account"),
        ("Проверить таблицу сейчас", "checkin_scan"),
        ("Тексты сообщений", "checkin_template"),
        ("Минимальная сумма (необязательно)", "checkin_limit"),
        ("Включить отправку", "checkin_delivery_confirm"),
        ("Как настроить чат ошибок", "checkin_errors_help"),
        ("Меню", "menu"),
    ))
    await query.answer()


@dp.callback_query(F.data == "checkin_connect_account")
async def checkin_connect_account(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.message.answer("Подключение Local Traffic делается только в личном чате с ботом, чтобы номер и код не попали в группу.")
    else:
        await state.set_state(Flow.checkin_account_phone)
        await query.message.answer("Пришлите номер Local Traffic в международном формате, например: +79991234567. Telegram отправит на него код. Номер не сохраняется после подключения.")
    await query.answer()


async def remove_sensitive_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


@dp.message(Flow.checkin_account_phone)
async def checkin_account_phone(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Номер можно прислать только в личный чат с ботом.")
        return
    result = await service.begin_manual_sender_login(message.text or "")
    await remove_sensitive_message(message)
    if result == "code_sent":
        await state.set_state(Flow.checkin_account_code)
        await message.answer("Код отправлен в Telegram. Пришлите его сюда; сообщение с кодом будет удалено после проверки.")
    elif result == "already_authorized":
        await state.clear()
        await message.answer("Local Traffic уже подключён и авторизован.")
    elif result == "invalid_phone":
        await message.answer("Не получилось распознать номер. Нужен формат +79991234567.")
    else:
        await state.clear()
        await message.answer("Не удалось запросить код Telegram. Попробуйте ещё раз позже.")


@dp.message(Flow.checkin_account_code)
async def checkin_account_code(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Код можно прислать только в личный чат с ботом.")
        return
    result = await service.finish_manual_sender_login(message.text or "")
    await remove_sensitive_message(message)
    if result == "authorized":
        await state.clear()
        await message.answer("Local Traffic подключён. Теперь можно сделать проверку таблицы без отправки.")
    elif result == "password_needed":
        await state.set_state(Flow.checkin_account_password)
        await message.answer("На аккаунте включён пароль 2FA. Пришлите его сюда; сообщение будет удалено после проверки.")
    elif result == "invalid_code":
        await message.answer("Код не подошёл. Пришлите актуальный код ещё раз.")
    elif result == "expired_code":
        await state.clear()
        await message.answer("Код истёк. Нажмите «Подключить Local Traffic» и запросите новый.")
    else:
        await state.clear()
        await message.answer("Не удалось завершить подключение. Запросите код заново.")


@dp.message(Flow.checkin_account_password)
async def checkin_account_password(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Пароль можно прислать только в личный чат с ботом.")
        return
    result = await service.finish_manual_sender_password(message.text or "")
    await remove_sensitive_message(message)
    if result == "authorized":
        await state.clear()
        await message.answer("Local Traffic подключён. Теперь можно сделать проверку таблицы без отправки.")
    elif result == "invalid_password":
        await message.answer("Пароль не подошёл. Пришлите его ещё раз.")
    else:
        await state.clear()
        await message.answer("Не удалось завершить подключение. Запросите код заново.")


@dp.callback_query(F.data == "checkin_scan")
async def checkin_scan(query: CallbackQuery):
    if await reject_if_needed(query): return
    configured, reason = checkin_service.configured()
    if not configured:
        await query.message.answer(f"Пока не запускаю: {reason}.")
        await query.answer()
        return
    await query.answer("Проверка запущена")
    await query.message.answer("Проверяю таблицу и ищу контакты. Отправка сообщений не включается этой кнопкой.")

    async def run():
        try:
            totals = await run_checkin_scan()
            result = ", ".join(f"{key}: {value}" for key, value in totals.items()) or "подходящих строк нет"
            await query.message.answer(f"Проверка завершена: {result}")
        except Exception:
            log.exception("Customer check-in scan failed")
            await query.message.answer("Не удалось проверить таблицу. Проверьте ссылку на CSV-выгрузку и настройки.")
    asyncio.create_task(run())


@dp.callback_query(F.data == "checkin_template")
async def checkin_template(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.checkin_template)
    current = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(store.checkin_templates()))
    await query.message.answer("Пришлите варианты сообщений: один вариант в каждой строке. Бот будет чередовать их по порядку.\n\nТекущие варианты:\n" + current + "\n\nОтправка не включится этой настройкой.")
    await query.answer()


@dp.message(Flow.checkin_template)
async def checkin_template_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    templates = [" ".join(line.split()) for line in (message.text or "").splitlines() if line.strip()]
    if not templates:
        await message.answer("Нужен хотя бы один непустой текст.")
        return
    try:
        store.set_checkin_templates(templates)
    except ValueError:
        await message.answer("Нужен хотя бы один непустой текст.")
        return
    store.audit("checkin_templates_changed", f"Вариантов: {len(templates)}; изменил {message.from_user.id}")
    await state.clear()
    await message.answer(f"Сохранено вариантов: {len(templates)}. Они будут чередоваться по порядку. Отправка по-прежнему выключена.")


@dp.callback_query(F.data == "checkin_limit")
async def checkin_limit(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.checkin_limit)
    await query.message.answer("Введите минимальную сумму продажи в рублях или 0, чтобы пока не фильтровать по сумме.")
    await query.answer()


@dp.message(Flow.checkin_limit)
async def checkin_limit_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        value = int((message.text or "").replace(" ", ""))
        if not 0 <= value <= 10_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введите сумму целым числом, например: 10000, или 0.")
        return
    store.set_setting("checkin_min_amount", str(value))
    store.audit("checkin_min_amount_changed", f"{value}; изменил {message.from_user.id}")
    await state.clear()
    await message.answer("Фильтр по сумме отключён." if value == 0 else f"Минимальная сумма сохранена: {value} руб.")


@dp.callback_query(F.data == "checkin_errors_help")
async def checkin_errors_help(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.message.answer("Создайте отдельную группу с Рустамом, добавьте туда бота и отправьте в ней команду /checkin_errors_here. В эту группу придут только карточки, где бот не смог однозначно выбрать человека.")
    await query.answer()


@dp.callback_query(F.data == "checkin_delivery_confirm")
async def checkin_delivery_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    if not store.checkin_templates():
        await query.message.answer("Сначала сохраните хотя бы один текст сообщения.")
    elif store.setting("global_paused") == "1":
        await query.message.answer("Сначала снимите общую паузу в разделе «Настройки». Она является общим аварийным стопом.")
    else:
        await query.message.answer("Включить автоматическую отправку только для клиентов через 14 дней? Подходящие контакты получат сообщение в рамках дневного лимита.", reply_markup=kb(("Включить", "checkin_delivery_enable"), ("Отмена", "checkins")))
    await query.answer()


@dp.callback_query(F.data == "checkin_delivery_enable")
async def checkin_delivery_enable(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("checkin_delivery_enabled", "1")
    store.audit("checkin_delivery_enabled", f"Подтвердил {query.from_user.id}")
    await query.message.answer("Отправка для клиентов через 14 дней включена. Общая аварийная пауза продолжает работать.")
    await query.answer()


@dp.callback_query(F.data.startswith("checkin_choose:"))
async def checkin_choose(query: CallbackQuery):
    if await reject_if_needed(query): return
    _, checkin_id, peer_id = query.data.split(":")
    if await checkin_service.choose_candidate(int(checkin_id), int(peer_id)):
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Контакт выбран и добавлен в очередь.")
    else:
        await query.answer("Карточка устарела или уже обработана.", show_alert=True)


@dp.callback_query(F.data.startswith("checkin_choose_group:"))
async def checkin_choose_group(query: CallbackQuery):
    if await reject_if_needed(query): return
    _, checkin_id, group_peer_id = query.data.split(":")
    result = await checkin_service.choose_group(int(checkin_id), int(group_peer_id))
    if result == "ready":
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Чат и контакт выбраны. Проект добавлен в очередь.")
    elif result == "review":
        await send_checkin_exceptions()
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Чат выбран. Отправлена карточка выбора человека.")
    else:
        await query.answer("Не удалось обработать чат. Карточка оставлена для проверки.", show_alert=True)


@dp.callback_query(F.data.startswith("checkin_username:"))
async def checkin_username(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    checkin_id = int(query.data.split(":", 1)[1])
    await state.update_data(
        checkin_id=checkin_id,
        card_chat_id=query.message.chat.id,
        card_message_id=query.message.message_id,
    )
    await state.set_state(Flow.checkin_username)
    await query.message.answer("Пришлите @username человека. Бот выберет его только если у аккаунта Local Traffic уже есть с ним личная переписка.")
    await query.answer()


@dp.message(Flow.checkin_username)
async def checkin_username_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    data = await state.get_data()
    result = await checkin_service.choose_username(int(data["checkin_id"]), message.text or "")
    if result == "ready":
        await delete_operator_card(int(data["card_chat_id"]), int(data["card_message_id"]), message.bot)
        await state.clear()
        await message.answer("Контакт сохранён и добавлен в очередь.")
    elif result == "invalid":
        await message.answer("Пришлите корректный @username.")
    elif result == "excluded":
        await message.answer("Этот @username относится к вашей команде и исключён из выбора.")
    elif result in {"not_found", "ambiguous"}:
        await message.answer("Такой личный диалог не найден или найден неоднозначно. Проверьте @username.")
    else:
        await message.answer("Не удалось проверить username. Попробуйте ещё раз позже.")


@dp.callback_query(F.data.startswith("checkin_skip:"))
async def checkin_skip(query: CallbackQuery):
    if await reject_if_needed(query): return
    checkin_id = int(query.data.split(":", 1)[1])
    store.mark_checkin_skipped(checkin_id)
    store.audit("checkin_skipped", f"Пропустил {query.from_user.id}")
    await query.message.answer("Проект пропущен. Сообщение по этой продаже не отправится.")
    await query.answer()


@dp.callback_query(F.data.startswith("checkin_block:"))
async def checkin_block(query: CallbackQuery):
    if await reject_if_needed(query): return
    checkin_id = int(query.data.split(":", 1)[1])
    store.block_checkin_project(checkin_id, query.from_user.id)
    store.audit("checkin_project_blocked", f"Не писать больше; подтвердил {query.from_user.id}")
    await query.message.answer("Проект исключён. При повторном появлении такого же названия бот ему не напишет.")
    await query.answer()


@dp.callback_query(F.data == "active_customers")
async def active_customers(query: CallbackQuery):
    if await reject_if_needed(query): return
    counts = active_customer_service.status()
    status_text = ', '.join(f'{key}: {value}' for key, value in counts.items()) or 'пока нет'
    text = (
        'Активные клиенты · все месяцы\n'
        'Источник: таблица продаж Рустама, все месяцы; отправитель Local Traffic.\n'
        f"Активность: сообщение клиента за {store.setting('active_customer_activity_days', '10')} дней. "
        'Сообщения команды и рассылок не считаются.\n'
        'Один проект: один чат и одно ЛС. Повторы по человеку и проекту исключены.\n'
        f"Текст в чат: {'задан' if store.setting('active_customer_chat_text', '').strip() else 'не задан'}; "
        f"текст в ЛС: {'задан' if store.setting('active_customer_private_text', '').strip() else 'не задан'}\n"
        f"Отправка: {'включена' if store.setting('active_customer_enabled') == '1' else 'выключена'}\n"
        f"Снимок источника: {len(json.loads(store.setting('active_customer_source_keys', '') or '[]'))} проектов\n"
        f'Статусы: {status_text}'
    )
    await query.message.answer(text, reply_markup=kb(
        ('Проверить строки таблицы', 'active_source'),
        ('Сопоставить строку вручную', 'active_map_start'),
        ('Предпросмотр по живым чатам', 'active_preview'),
        ('Текст в чат', 'active_chat_text'),
        ('Текст в ЛС', 'active_private_text'),
        ('Окно активности: 5 или 10 дней', 'active_window'),
        ('Включить отправку', 'active_enable_confirm'),
        ('Выключить отправку', 'active_disable'),
        ('Меню', 'menu'),
    ))
    await query.answer()


@dp.callback_query(F.data == 'active_source')
async def active_source(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.answer('Читаю таблицу')
    try:
        result = await asyncio.to_thread(active_customer_service.source_overview)
        names = [f'• строка {number}: {escape(name[:90])}'
                 for number, name in result.unmatched]
        await query.message.answer(
            f'Однозначно сопоставлено проектов: {len(result.projects)}\n'
            f'Строк без надёжного совпадения: {len(result.unmatched)}\n'
            f'Исключено тестов и повторных платежей: {result.skipped}\n\n'
            'Непривязанные строки не отправляются автоматически. Полный список ниже.',
            parse_mode='HTML')
        for start in range(0, len(names), 20):
            await query.message.answer('\n'.join(names[start:start + 20]), parse_mode='HTML')
    except Exception as exc:
        log.exception('Active customer source preview failed')
        await query.message.answer(f'Не удалось проверить таблицу: {type(exc).__name__}')


@dp.callback_query(F.data == 'active_map_start')
async def active_map_start(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.active_customer_map_row)
    await query.message.answer('Пришлите номер непривязанной строки из списка таблицы. '
                               'Бот покажет похожие проекты для ручного подтверждения.')
    await query.answer()


@dp.message(Flow.active_customer_map_row)
async def active_map_row(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        row_number = int((message.text or '').strip())
        manifest = active_customer_service._manifest()
        overview = await asyncio.to_thread(active_customer_service.source_overview, manifest)
        name = next(value for number, value in overview.unmatched if number == row_number)
    except (ValueError, StopIteration, RuntimeError):
        await message.answer('Нет такой непривязанной строки. Проверьте номер в разделе «Проверить строки таблицы».')
        return
    reviewed = {item['id']: item['title'] for item in manifest['inventory']}
    cleaned = re.sub(r'\([^)]*\)', ' ', name)
    source_tokens = set(project_chat_tokens(cleaned))
    candidates = []
    for index, project in enumerate(manifest['projects']):
        titles = [reviewed.get(cid, '') for cid in project['chat_ids']]
        score = max((project_chat_match_score(cleaned, title) for title in titles), default=0)
        overlap = max((len(source_tokens & set(project_chat_tokens(title))) for title in titles), default=0)
        if score or overlap:
            candidates.append((score, overlap, index, titles[0] if titles else project['key']))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[3]))
    if not candidates:
        await message.answer('Похожих проектов в проверенной карте нет. Строка останется без отправки.')
        await state.clear()
        return
    digest = active_customer_service.row_digest(name)
    buttons = [[InlineKeyboardButton(text=title[:55],
                  callback_data=f'active_map:{row_number}:{index}:{digest}')]
               for _, _, index, title in candidates[:8]]
    await message.answer(
        f'Строка {row_number}: {escape(name)}\nВыберите проект только при точном совпадении магазина и города:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode='HTML')
    await state.clear()


@dp.callback_query(F.data.startswith('active_map:'))
async def active_map_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    try:
        _, row, index, digest = query.data.split(':')
        key = await asyncio.to_thread(active_customer_service.save_row_override,
                                      int(row), int(index), digest)
    except (ValueError, RuntimeError):
        await query.answer('Строка изменилась или не найдена. Обновите список.', show_alert=True)
        return
    store.set_setting('active_customer_last_preview_at', '')
    store.audit('active_customer_row_mapped', f'Строка {row} -> {key}; подтвердил {query.from_user.id}')
    await query.message.answer(f'Строка {row} привязана к проекту {escape(key)}. '
                               'Выполните новый предпросмотр.', parse_mode='HTML')
    await query.answer()


@dp.callback_query(F.data == 'active_preview')
async def active_preview(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.answer('Начал проверку; пришлю итог')
    async def run():
        try:
            async with campaign_telegram_lock:
                result = await active_customer_service.preview_live()
            store.set_setting('active_customer_last_preview_at', datetime.now(ZoneInfo('Europe/Moscow')).isoformat())
            store.set_setting('active_customer_last_preview_ready', str(result['ready']))
            store.set_setting('active_customer_last_preview_keys', json.dumps(result['ready_keys']))
            held = ', '.join(f'{key}: {value}' for key, value in sorted(result['held'].items())) or 'нет'
            sample = '\n'.join(f'• {escape(title)}' for title, _ in result['samples'])
            await query.message.answer(
                f"Проверка завершена. Проектов из таблицы: {result['source_projects']}; "
                f"с сообщением клиента: {result['recent']}; готовы: {result['ready']}; "
                f"ранее написали: {result['already_contacted']}.\n"
                f"Без совпадения в таблице: {result['unmatched_rows']}; "
                f"исключены тесты и повторные платежи: {result['skipped_rows']}.\n"
                f'Удержаны: {escape(held)}\n\nПервые готовые:\n{sample}', parse_mode='HTML')
        except Exception as exc:
            log.exception('Active customer live preview failed')
            await query.message.answer(f'Предпросмотр не завершён: {type(exc).__name__}. Подробности в логе сервера.')
    asyncio.create_task(run())


@dp.callback_query(F.data.in_({'active_chat_text', 'active_private_text'}))
async def active_text_start(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    kind = 'чат' if query.data == 'active_chat_text' else 'ЛС'
    await state.set_state(Flow.active_customer_chat_text if kind == 'чат' else Flow.active_customer_private_text)
    current = store.setting('active_customer_chat_text' if kind == 'чат' else 'active_customer_private_text', '')
    await query.message.answer(f'Пришлите согласованный текст для {kind} одним сообщением.' +
                               (f'\n\nТекущий текст:\n{current}' if current else ''))
    await query.answer()


@dp.message(Flow.active_customer_chat_text)
@dp.message(Flow.active_customer_private_text)
async def active_text_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    value = (message.text or '').strip()
    if not value or len(value.encode('utf-16-le')) // 2 > 4096:
        await message.answer('Нужен непустой текст не длиннее 4096 символов Telegram.')
        return
    kind = await state.get_state()
    setting = ('active_customer_chat_text' if kind == Flow.active_customer_chat_text.state
               else 'active_customer_private_text')
    store.set_setting(setting, value)
    store.audit('active_customer_text_saved', f'{setting}; изменил {message.from_user.id}')
    await state.clear()
    await message.answer('Текст сохранён. Уже зарезервированные отправки сохраняют прежний текст.')


@dp.callback_query(F.data == 'active_window')
async def active_window(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.message.answer('Считать активным чат, если клиент писал за:',
                               reply_markup=kb(('5 дней', 'active_window:5'),
                                               ('10 дней', 'active_window:10')))
    await query.answer()


@dp.callback_query(F.data.startswith('active_window:'))
async def active_window_save(query: CallbackQuery):
    if await reject_if_needed(query): return
    days = int(query.data.split(':')[-1])
    if days not in {5, 10}:
        await query.answer('Недопустимое окно', show_alert=True)
        return
    store.set_setting('active_customer_activity_days', str(days))
    store.set_setting('active_customer_last_preview_at', '')
    store.audit('active_customer_window_saved', f'{days}; изменил {query.from_user.id}')
    await query.message.answer(f'Окно активности: {days} дней. Нужен новый предпросмотр.')
    await query.answer()


@dp.callback_query(F.data == 'active_enable_confirm')
async def active_enable_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    reason = active_customer_service._can_send()
    if reason not in {'disabled', 'outside_hours', 'waiting', 'daily_limit', 'missing_source_snapshot'}:
        await query.message.answer(f'Включение пока недоступно: {reason}.')
    elif not store.setting('active_customer_chat_text', '').strip() or not store.setting('active_customer_private_text', '').strip():
        await query.message.answer('Сначала сохраните оба согласованных текста.')
    elif store.setting('global_paused') == '1':
        await query.message.answer('Сначала снимите общую аварийную паузу.')
    else:
        raw = store.setting('active_customer_last_preview_at', '')
        try:
            preview_at = datetime.fromisoformat(raw)
            recent = datetime.now(ZoneInfo('Europe/Moscow')) - preview_at < timedelta(hours=24)
        except (ValueError, TypeError):
            recent = False
        if not recent:
            await query.message.answer('Сначала выполните предпросмотр по живым чатам (не старше 24 часов).')
        else:
            await query.message.answer(
                f"Включить рассылку? По последнему предпросмотру готовы: {store.setting('active_customer_last_preview_ready', '0')}. "
                'Бот отправит одно сообщение в один клиентский чат и одно ЛС, с интервалами и проверкой перед каждым шагом.',
                reply_markup=kb(('Включить', 'active_enable'), ('Отмена', 'active_customers')))
    await query.answer()


@dp.callback_query(F.data == 'active_enable')
async def active_enable(query: CallbackQuery):
    if await reject_if_needed(query): return
    if not store.setting('active_customer_chat_text', '').strip() or not store.setting('active_customer_private_text', '').strip():
        await query.answer('Сначала сохраните оба текста', show_alert=True)
        return
    raw = store.setting('active_customer_last_preview_at', '')
    try:
        valid = datetime.now(ZoneInfo('Europe/Moscow')) - datetime.fromisoformat(raw) < timedelta(hours=24)
    except (ValueError, TypeError):
        valid = False
    if not valid or store.setting('global_paused') == '1':
        await query.answer('Нужен свежий предпросмотр и снятая аварийная пауза', show_alert=True)
        return
    if not store.setting('active_customer_source_keys', ''):
        try:
            source = await asyncio.to_thread(active_customer_service.source_overview)
            candidates = set(json.loads(store.setting('active_customer_last_preview_keys', '[]')))
        except Exception:
            log.exception('Active customer source snapshot failed')
            await query.answer('Не удалось повторно проверить источник', show_alert=True)
            return
        snapshot = sorted(candidates & set(source.projects))
        if not snapshot:
            await query.answer('Нет готовых клиентов в свежем предпросмотре', show_alert=True)
            return
        store.set_setting('active_customer_source_keys', json.dumps(snapshot))
    store.set_setting('active_customer_enabled', '1')
    store.audit('active_customer_enabled', f'Подтвердил {query.from_user.id}')
    await query.message.answer('Рассылка активным клиентам включена.')
    await query.answer()


@dp.callback_query(F.data == 'active_disable')
async def active_disable(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting('active_customer_enabled', '0')
    store.audit('active_customer_disabled', f'Выключил {query.from_user.id}')
    await query.message.answer('Рассылка активным клиентам выключена.')
    await query.answer()


@dp.callback_query(F.data == "renewals")
async def renewals(query: CallbackQuery):
    if await reject_if_needed(query): return
    configured, reason = renewal_service.configured()
    summary = store.renewal_summary()
    statuses = ", ".join(f"{key}: {value}" for key, value in summary.items()) or "пока нет"
    text = (
        "Продления\n"
        f"Настройка источника: {'готова' if configured else reason}\n"
        f"Аккаунт: {renewal_service.sender_account_title or 'не задан'}\n"
        f"Красных текстов: {len(store.renewal_templates('red'))}\n"
        f"Белых текстов: {len(store.renewal_templates('white'))}\n"
        f"Дата белых: {store.setting('renewal_white_day')} число следующего месяца\n"
        f"Отправка: {'включена' if store.setting('renewal_delivery_enabled') == '1' else 'выключена'}\n"
        f"Статусы: {statuses}"
    )
    await query.message.answer(text, reply_markup=kb(
        ("Импортировать Local Traffic", "renewal_import_account"),
        ("Проверить таблицу сейчас", "renewal_scan"),
        ("Тексты для красных", "renewal_red_template"),
        ("Тексты для белых", "renewal_white_template"),
        ("Дата белых", "renewal_white_day"),
        ("Чат ошибок Рустама", "renewal_errors_help"),
        ("Включить отправку", "renewal_delivery_confirm"),
        ("Меню", "menu"),
    ))
    await query.answer()


@dp.callback_query(F.data == "renewal_import_account")
async def renewal_import_account(query: CallbackQuery):
    if await reject_if_needed(query): return
    title = renewal_service.sender_account_title
    if not title:
        await query.message.answer("Сначала задайте RENEWAL_ACCOUNT_TITLE в настройках сервера.")
    else:
        try:
            count = service.import_dialoghub_account_by_title(os.environ["DIALOGHUB_DB_PATH"], os.environ["ACCOUNT_SESSIONS_DIR"], title)
            await query.message.answer("Аккаунт импортирован." if count else f"Не удалось однозначно найти аккаунт «{title}» в DialogHub.")
        except Exception:
            log.exception("Renewal account import failed")
            await query.message.answer("Не удалось импортировать аккаунт. Проверьте настройки DialogHub.")
    await query.answer()


@dp.callback_query(F.data == "renewal_scan")
async def renewal_scan(query: CallbackQuery):
    if await reject_if_needed(query): return
    configured, reason = renewal_service.configured()
    if not configured:
        await query.message.answer(f"Пока не запускаю: {reason}.")
        await query.answer()
        return
    await query.answer("Проверка запущена")
    await query.message.answer("Проверяю все вкладки продлений. Эта кнопка не включает отправку сообщений.")

    async def run():
        try:
            totals = await run_renewal_scan()
            await query.message.answer("Проверка завершена: " + (", ".join(f"{key}: {value}" for key, value in totals.items()) or "строк нет"))
        except Exception:
            log.exception("Renewal scan failed")
            await query.message.answer("Не удалось прочитать таблицу продлений. Проверьте XLSX-ссылку и доступ к ней.")
    asyncio.create_task(run())


@dp.callback_query(F.data.in_({"renewal_red_template", "renewal_white_template"}))
async def renewal_templates_edit(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    kind = "red" if query.data == "renewal_red_template" else "white"
    await state.set_state(Flow.renewal_red_template if kind == "red" else Flow.renewal_white_template)
    existing = "\n\n---\n\n".join(store.renewal_templates(kind)) or "пока не задано"
    title = "красных" if kind == "red" else "белых"
    await query.message.answer(f"Пришлите варианты для {title} строк. Один вариант отделяйте от следующего строкой из трёх дефисов: ---. Бот сохранит абзацы и будет чередовать варианты по порядку.\n\nТекущие варианты:\n{existing}")
    await query.answer()


async def save_renewal_templates(message: Message, state: FSMContext, kind: str):
    if await reject_if_needed(message): return
    values = [block.strip() for block in re.split(r"\n\s*---\s*\n", message.text or "") if block.strip()]
    if not values:
        await message.answer("Нужен хотя бы один непустой текст.")
        return
    store.set_renewal_templates(kind, values)
    store.audit("renewal_templates_changed", f"{kind}: {len(values)}; изменил {message.from_user.id}")
    await state.clear()
    await message.answer(f"Сохранено вариантов: {len(values)}. Они будут чередоваться по порядку.")


@dp.message(Flow.renewal_red_template)
async def renewal_red_template_save(message: Message, state: FSMContext):
    await save_renewal_templates(message, state, "red")


@dp.message(Flow.renewal_white_template)
async def renewal_white_template_save(message: Message, state: FSMContext):
    await save_renewal_templates(message, state, "white")


@dp.callback_query(F.data == "renewal_white_day")
async def renewal_white_day(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.renewal_white_day)
    await query.message.answer(f"Введите число следующего месяца для белых строк. Сейчас: {store.setting('renewal_white_day')}. Например: 5")
    await query.answer()


@dp.message(Flow.renewal_white_day)
async def renewal_white_day_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        day = int(message.text or "")
        if not 1 <= day <= 28:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 28.")
        return
    store.set_setting("renewal_white_day", str(day))
    store.audit("renewal_white_day_changed", f"{day}; изменил {message.from_user.id}")
    await state.clear()
    await message.answer(f"Белые строки будут проверяться {day} числа следующего месяца.")


@dp.callback_query(F.data == "renewal_errors_help")
async def renewal_errors_help(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.message.answer("Создайте отдельную группу с Рустамом, добавьте туда бота и отправьте команду /renewal_errors_here. Туда придут только случаи, где бот не смог однозначно выбрать человека.")
    await query.answer()


@dp.callback_query(F.data == "renewal_delivery_confirm")
async def renewal_delivery_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    if not store.renewal_templates("red"):
        await query.message.answer("Сначала сохраните текст для красных строк.")
    elif store.setting("global_paused") == "1":
        await query.message.answer("Сначала снимите общую паузу в разделе «Настройки».")
    else:
        await query.message.answer("Включить отправку по продлениям? Красные строки получат сообщение сразу после проверки. Белые — только после заданной даты и только когда для них сохранён текст.", reply_markup=kb(("Включить", "renewal_delivery_enable"), ("Отмена", "renewals")))
    await query.answer()


@dp.callback_query(F.data == "renewal_delivery_enable")
async def renewal_delivery_enable(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("renewal_delivery_enabled", "1")
    store.audit("renewal_delivery_enabled", f"Подтвердил {query.from_user.id}")
    await query.message.answer("Отправка по продлениям включена. Общая аварийная пауза действует всегда.")
    await query.answer()


@dp.callback_query(F.data.startswith("renewal_choose:"))
async def renewal_choose(query: CallbackQuery):
    if await reject_if_needed(query): return
    _, followup_id, peer_id = query.data.split(":")
    if await renewal_service.choose_candidate(int(followup_id), int(peer_id)):
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Контакт выбран и добавлен в очередь.")
    else:
        await query.answer("Карточка устарела или уже обработана.", show_alert=True)


@dp.callback_query(F.data.startswith("renewal_choose_group:"))
async def renewal_choose_group(query: CallbackQuery):
    if await reject_if_needed(query): return
    _, followup_id, group_peer_id = query.data.split(":")
    result = await renewal_service.choose_group(int(followup_id), int(group_peer_id))
    if result == "ready":
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Чат и контакт выбраны. Проект добавлен в очередь.")
    elif result == "review":
        await send_renewal_exceptions()
        await delete_operator_card(query.message.chat.id, query.message.message_id, query.bot)
        await query.answer("Чат выбран. Отправлена карточка выбора человека.")
    else:
        await query.answer("Не удалось обработать чат. Карточка оставлена для проверки.", show_alert=True)


@dp.callback_query(F.data.startswith("renewal_username:"))
async def renewal_username(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.update_data(
        renewal_followup_id=int(query.data.split(":", 1)[1]),
        card_chat_id=query.message.chat.id,
        card_message_id=query.message.message_id,
    )
    await state.set_state(Flow.renewal_username)
    await query.message.answer("Пришлите @username. Бот выберет его только если у Local Traffic уже есть с ним личный диалог.")
    await query.answer()


@dp.message(Flow.renewal_username)
async def renewal_username_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    data = await state.get_data()
    result = await renewal_service.choose_username(int(data["renewal_followup_id"]), message.text or "")
    if result == "ready":
        await delete_operator_card(int(data["card_chat_id"]), int(data["card_message_id"]), message.bot)
        await state.clear()
        await message.answer("Контакт сохранён и добавлен в очередь.")
    elif result == "invalid":
        await message.answer("Пришлите корректный @username.")
    elif result == "excluded":
        await message.answer("Этот @username относится к вашей команде и исключён из выбора.")
    else:
        await message.answer("Не удалось найти однозначный личный диалог. Проверьте username или попробуйте позже.")


@dp.callback_query(F.data.startswith("renewal_skip:"))
async def renewal_skip(query: CallbackQuery):
    if await reject_if_needed(query): return
    followup_id = int(query.data.split(":", 1)[1])
    store.mark_renewal_skipped(followup_id)
    store.audit("renewal_skipped", f"Пропустил {query.from_user.id}")
    await query.message.answer("Проект пропущен. Сообщение по нему не отправится.")
    await query.answer()


@dp.callback_query(F.data.startswith("renewal_block:"))
async def renewal_block(query: CallbackQuery):
    if await reject_if_needed(query): return
    followup_id = int(query.data.split(":", 1)[1])
    store.block_renewal_project(followup_id, query.from_user.id)
    store.audit("renewal_project_blocked", f"Не писать больше; подтвердил {query.from_user.id}")
    await query.message.answer("Проект исключён из будущих сообщений по продлениям.")
    await query.answer()


@dp.callback_query(F.data == "logs")
async def logs(query: CallbackQuery):
    if await reject_if_needed(query): return
    rows = store.recent_logs()
    lines = [f"{row['at'][:19]} · {row['kind']} · {row['details'][:140]}" for row in rows]
    await query.message.answer("🧾 Последние события:\n" + ("\n".join(lines) if lines else "Лог пуст."))
    await query.answer()


@dp.callback_query(F.data == "settings")
async def settings(query: CallbackQuery):
    if await reject_if_needed(query): return
    text = ("⚙️ Настройки:\n"
            f"Пауза: {'да' if store.setting('global_paused') == '1' else 'нет'}\n"
            f"Доставка разрешена: {'да' if store.setting('delivery_enabled') == '1' else 'нет'}\n"
            f"Лимит на аккаунт/день: {store.setting('task_max_per_account_per_day')}\n"
            f"Пауза с одного аккаунта: {int(store.setting('task_delay_seconds') or '600') // 60}–{int(store.setting('task_max_delay_seconds') or '900') // 60} мин.\n"
            f"Рабочие часы: {store.setting('task_work_start_hour')}–{store.setting('task_work_end_hour')} ({store.setting('timezone')})")
    await query.message.answer(text, reply_markup=kb(("Лимит", "setting_limit"), ("Интервал", "setting_delay"), ("Рабочие часы", "setting_hours"), ("Разрешить доставку", "delivery_confirm"), ("Снять паузу", "resume_confirm"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data.in_({"setting_limit", "setting_delay", "setting_hours"}))
async def setting_input(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    mapping = {"setting_limit": (Flow.limit, "Введите целый дневной лимит на один аккаунт."), "setting_delay": (Flow.delay, "Введите минимальную и максимальную паузу в минутах через пробел, например: 10 15."), "setting_hours": (Flow.hours, "Введите начало и конец рабочих часов через пробел, например: 10 20")}
    target, text = mapping[query.data]
    await state.set_state(target)
    await query.message.answer(text)
    await query.answer()


@dp.message(Flow.limit)
async def setting_limit(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        value = int(message.text or "")
        if not 1 <= value <= 100: raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 100."); return
    store.set_setting("task_max_per_account_per_day", str(value)); await state.clear(); await message.answer("✅ Лимит сохранён для новых задач.")


@dp.message(Flow.delay)
async def setting_delay(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        minimum, maximum = map(int, (message.text or "").split())
        if not 1 <= minimum <= maximum <= 24 * 60: raise ValueError
    except ValueError:
        await message.answer("Введите две величины в минутах, например: 10 15."); return
    store.set_setting("task_delay_seconds", str(minimum * 60)); store.set_setting("task_max_delay_seconds", str(maximum * 60)); await state.clear(); await message.answer("✅ Пауза сохранена для новых задач.")


@dp.message(Flow.hours)
async def setting_hours(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    try:
        start, end = map(int, (message.text or "").split())
        if not 0 <= start < end <= 24: raise ValueError
    except ValueError:
        await message.answer("Пример корректного формата: 10 20"); return
    store.set_setting("task_work_start_hour", str(start)); store.set_setting("task_work_end_hour", str(end)); await state.clear(); await message.answer("✅ Рабочие часы сохранены для новых задач.")


@dp.callback_query(F.data == "delivery_confirm")
async def delivery_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.message.answer("Это только первый предохранитель. Подтвердите ещё раз: после включения доставка всё равно не начнётся без включённой задачи.", reply_markup=kb(("Подтвердить доступ к доставке", "delivery_enable"), ("Отмена", "menu")))
    await query.answer()


@dp.callback_query(F.data == "delivery_enable")
async def delivery_enable(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("delivery_enabled", "1"); store.audit("delivery_enabled", f"Подтвердил {query.from_user.id}")
    await query.message.answer("✅ Доступ к доставке подтверждён. Глобальная пауза пока сохранена.")
    await query.answer()


@dp.callback_query(F.data == "resume_confirm")
async def resume_confirm(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.message.answer("Снять глобальную паузу? Отправка начнётся лишь для отдельно включённых задач.", reply_markup=kb(("Да, снять паузу", "resume"), ("Отмена", "menu")))
    await query.answer()


@dp.callback_query(F.data == "resume")
async def resume(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("global_paused", "0"); store.audit("global_resumed", f"Подтвердил {query.from_user.id}")
    await query.message.answer("✅ Глобальная пауза снята. Ни одна задача не включается автоматически.")
    await query.answer()


@dp.callback_query(F.data == "emergency_stop")
async def emergency_stop(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("global_paused", "1"); store.audit("emergency_stop", f"Экстренная остановка: {query.from_user.id}")
    await query.message.answer("🚨 Все новые отправки остановлены. Очередь и история сохранены.")
    await query.answer()


@dp.callback_query(F.data == "menu")
async def menu(query: CallbackQuery):
    if await reject_if_needed(query): return
    await send_menu(query.message)
    await query.answer()


async def delivery_loop():
    while True:
        try:
            sent = await service.run_delivery_tick()
            if sent:
                await send_delivery_report()
        except Exception:
            log.exception("Delivery tick failed")
        await asyncio.sleep(30)


async def scan_loop():
    """Keep new dialogs visible; scanning does not turn on delivery or a task."""
    await asyncio.sleep(60)
    while True:
        try:
            if store.setting("auto_scan_enabled") == "1":
                totals = await service.scan_all(os.getenv("DIALOGHUB_DB_PATH"))
                await send_report(scan_report(totals))
        except Exception:
            log.exception("Automatic dialog scan failed")
        await asyncio.sleep(int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))


async def revalidation_loop():
    """Safely clean old candidates before delivery resumes."""
    while True:
        try:
            if store.setting("revalidation_active") == "1":
                result = await service.run_revalidation_tick()
                if result == "complete":
                    summary = store.revalidation_summary()
                    store.set_setting("revalidation_active", "0")
                    store.set_setting("auto_scan_enabled", store.setting("revalidation_resume_scan", "1") or "1")
                    if store.setting("revalidation_resume_delivery", "1") == "1":
                        store.set_setting("global_paused", "0")
                    store.audit("revalidation_finished", f"Полная проверка: безопасно {summary['safe']}, исключено {summary['excluded']}")
                    await send_report(f"✅ Полная перепроверка очереди завершена\nБезопасных чатов: {summary['safe']}\nИсключено: {summary['excluded']}")
                if result in {"waiting_for_account", "waiting_for_scan"}:
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(float(os.getenv("REVALIDATION_INTERVAL_SECONDS", "3")))
            else:
                await asyncio.sleep(5)
        except Exception:
            log.exception("Queue revalidation failed")
            await asyncio.sleep(10)


async def checkin_scan_loop():
    """Read the sales sheet at the configured Moscow hour every day."""
    timezone_name = store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
    try:
        scan_hour = int(os.getenv("CHECKIN_SCAN_HOUR", "11"))
        if not 0 <= scan_hour <= 23:
            raise ValueError
    except ValueError:
        scan_hour = 11
    # A restart after the scheduled hour must not wait until tomorrow.  The
    # persisted scan cursor in run_checkin_scan() makes this a safe catch-up.
    try:
        configured, _ = checkin_service.configured()
        timezone_name = store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
        if configured and store.setting("checkin_last_successful_scan_on") != datetime.now(ZoneInfo(timezone_name)).date().isoformat():
            await run_checkin_scan()
    except Exception:
        log.exception("Initial customer check-in catch-up failed")
    while True:
        now = datetime.now(ZoneInfo(timezone_name))
        scheduled = now.replace(hour=scan_hour, minute=0, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        await asyncio.sleep((scheduled - now).total_seconds())
        try:
            configured, _ = checkin_service.configured()
            if configured:
                await run_checkin_scan()
        except Exception:
            log.exception("Customer check-in scan loop failed")


async def checkin_delivery_loop():
    """Send at most one approved 14-day message per tick."""
    while True:
        try:
            async with campaign_telegram_lock:
                result, checkin_id = await checkin_service.deliver_one()
            if result == "sent" and checkin_id:
                row = store.checkin(checkin_id)
                await send_checkin_sent_report(row)
            if result in {"error", "review"}:
                await send_checkin_exceptions()
        except Exception:
            log.exception("Customer check-in delivery loop failed")
        await asyncio.sleep(30)


async def checkin_resolution_loop():
    """Retry a Local Traffic lookup that was temporarily unavailable."""
    while True:
        try:
            configured, _ = checkin_service.configured()
            if not configured:
                await asyncio.sleep(60)
                continue
            pending = store.checkins_to_resolve(limit=1)
            if not pending:
                await asyncio.sleep(60)
                continue
            async with campaign_telegram_lock:
                result = await checkin_service.resolve_checkin(int(pending[0]["id"]))
            if result in {"review", "error"}:
                await send_checkin_exceptions()
            # A sender that is still unavailable is checked again later,
            # rather than hammering one Telegram session every 30 seconds.
            await asyncio.sleep(300 if result == "unchanged" else 30)
        except Exception:
            log.exception("Customer check-in resolution loop failed")
            await asyncio.sleep(60)


async def renewal_scan_loop():
    """Read every renewal month tab at the configured Moscow hour."""
    timezone_name = store.setting("timezone", "Europe/Moscow") or "Europe/Moscow"
    try:
        scan_hour = int(os.getenv("RENEWAL_SCAN_HOUR", "11"))
        if not 0 <= scan_hour <= 23:
            raise ValueError
    except ValueError:
        scan_hour = 11
    try:
        configured, _ = renewal_service.configured()
        today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        if configured and store.setting("renewal_last_successful_scan_on") != today:
            await run_renewal_scan()
            store.set_setting("renewal_last_successful_scan_on", today)
    except Exception:
        log.exception("Initial renewal catch-up failed")
    while True:
        now = datetime.now(ZoneInfo(timezone_name))
        scheduled = now.replace(hour=scan_hour, minute=0, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        await asyncio.sleep((scheduled - now).total_seconds())
        try:
            configured, _ = renewal_service.configured()
            if configured:
                await run_renewal_scan()
                store.set_setting("renewal_last_successful_scan_on", datetime.now(ZoneInfo(timezone_name)).date().isoformat())
        except Exception:
            log.exception("Renewal scan loop failed")


async def renewal_delivery_loop():
    while True:
        try:
            async with campaign_telegram_lock:
                result, followup_id = await renewal_service.deliver_one()
            if result == "sent" and followup_id:
                row = store.renewal_followup(followup_id)
                await send_renewal_sent_report(row)
            if result in {"error", "review"}:
                await send_renewal_exceptions()
        except Exception:
            log.exception("Renewal delivery loop failed")
        await asyncio.sleep(30)


async def unified_lost_loop():
    while True:
        if store.setting('unified_lost_enabled','0')!='1':
            await asyncio.sleep(60)
            continue
        outcome = None
        try:
            async with campaign_telegram_lock:
                async with service.customer_delivery_lock:
                    outcome = await unified_lost_service.run_once()
            if outcome == 'sent':
                await send_report('✉️ Общий офер: отправлен комплект в чат(ы) и ЛС')
            elif outcome in {'reviewed_folder_drift','client_folder_renamed','video_finish_time_unknown',
                             'unlocated_new_or_changed_chat'}:
                log.warning('Unified lost campaign held: %s',outcome)
                await unified_lost_service._report_hold('Папка клиентов',outcome)
        except FloodWaitError as exc:
            log.warning('Unified lost campaign Telegram cooldown: %s seconds',exc.seconds)
            await asyncio.sleep(exc.seconds+5)
        except Exception:
            log.exception('Unified lost campaign failed; recipient remains reserved if send was uncertain')
            if store.setting('unified_lost_test_one_bundle','0')=='1' and store.setting('unified_lost_test_project_key',''):
                store.set_setting('unified_lost_enabled','0')
        if (store.setting('unified_lost_test_one_bundle','0')=='1'
                and store.setting('unified_lost_test_project_key','')
                and outcome not in {'partial','waiting_private'}):
            store.set_setting('unified_lost_enabled','0')
        if outcome=='partial' and store.setting('unified_lost_test_one_bundle','0')=='1':
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(10 if outcome in {'partial','waiting_private'} else 60)


async def active_customer_loop():
    """One durable chat or private message per turn, sharing the sender lock."""
    while True:
        if store.setting('active_customer_enabled', '0') != '1':
            await asyncio.sleep(60)
            continue
        try:
            async with campaign_telegram_lock:
                async with service.customer_delivery_lock:
                    outcome = await active_customer_service.run_once()
            if outcome in {'sent', 'partial'}:
                await send_report(f'Активные клиенты: шаг отправлен; итог {outcome}.')
            elif outcome == 'held':
                await send_report('Активные клиенты: отправка удержана для проверки. См. панель кампании.')
            elif outcome == 'complete':
                store.set_setting('active_customer_enabled', '0')
                await send_report('Активные клиенты: разовая кампания завершена. Отправка выключена.')
            await asyncio.sleep(30 if outcome in {'sent', 'partial'} else 300)
        except Exception:
            log.exception('Active customer campaign loop failed')
            await send_report('Активные клиенты: ошибка воркера; отправка остановлена до проверки лога.')
            store.set_setting('active_customer_enabled', '0')
            await asyncio.sleep(60)


async def renewal_resolution_loop():
    """Keep resolving newly detected red/white cases between daily sheet scans."""
    while True:
        try:
            configured, _ = renewal_service.configured()
            if not configured:
                await asyncio.sleep(60)
                continue
            pending = store.renewal_followups_to_resolve(limit=1)
            if not pending:
                await asyncio.sleep(60)
                continue
            async with campaign_telegram_lock:
                result = await renewal_service.resolve_followup(int(pending[0]["id"]))
            if result in {"review", "error"}:
                await send_renewal_exceptions()
            # Avoid bursting Telegram requests when several rows turn red at
            # the same time. FloodWait is still honoured in the service.
            await asyncio.sleep(300 if result == "unchanged" else 30)
        except Exception:
            log.exception("Renewal resolution loop failed")
            await asyncio.sleep(60)


async def exception_report_loop():
    """Retry operator cards that were deferred by Telegram rate limits."""
    while True:
        try:
            await send_checkin_exceptions()
            await send_renewal_exceptions()
        except Exception:
            log.exception("Exception report retry failed")
        await asyncio.sleep(60)


async def main():
    if ACCESS_MODE not in {"public", "group_admins"}:
        raise RuntimeError("ACCESS_MODE must be public or group_admins")
    global report_bot
    bot = Bot(os.environ["BOT_TOKEN"])
    report_bot = bot
    unified_lost_service.report_bot = bot
    asyncio.create_task(delivery_loop())
    asyncio.create_task(scan_loop())
    asyncio.create_task(revalidation_loop())
    asyncio.create_task(checkin_scan_loop())
    asyncio.create_task(checkin_delivery_loop())
    asyncio.create_task(checkin_resolution_loop())
    asyncio.create_task(renewal_scan_loop())
    asyncio.create_task(renewal_resolution_loop())
    asyncio.create_task(renewal_delivery_loop())
    asyncio.create_task(unified_lost_loop())
    asyncio.create_task(active_customer_loop())
    asyncio.create_task(exception_report_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
