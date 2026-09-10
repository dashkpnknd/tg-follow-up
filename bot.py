from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from store import Store
from telegram_service import FollowupService, TelegramSettings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("followup")

ADMINS = {int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()}
store = Store(os.getenv("DATABASE_PATH", "data/followup.sqlite3"))
service = FollowupService(store, TelegramSettings(int(os.environ["API_ID"]), os.environ["API_HASH"], int(os.getenv("HISTORY_LIMIT", "10000"))))
dp = Dispatcher()


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Подключить аккаунты DialogHub", callback_data="import")],
        [InlineKeyboardButton(text="🔎 Анализировать все диалоги", callback_data="scan")],
        [InlineKeyboardButton(text="📋 Предпросмотр кандидатов", callback_data="preview")],
        [InlineKeyboardButton(text="🚨 ОСТАНОВИТЬ ВСЕ ОТПРАВКИ", callback_data="stop")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="status")],
    ])


def allowed(user_id: int) -> bool:
    return bool(ADMINS) and user_id in ADMINS


async def reject_if_needed(message: Message | CallbackQuery) -> bool:
    user = message.from_user
    if not user or not allowed(user.id):
        if isinstance(message, CallbackQuery):
            await message.answer("Нет доступа", show_alert=True)
        else:
            await message.answer("Нет доступа. Добавьте ваш числовой Telegram ID в ADMIN_IDS на сервере.")
        return True
    return False


@dp.message(CommandStart())
async def start(message: Message):
    if await reject_if_needed(message):
        return
    await message.answer("Бот дожимов готов. Отправка по умолчанию отключена и поставлена на паузу.", reply_markup=menu())


@dp.callback_query(F.data == "import")
async def import_accounts(query: CallbackQuery):
    if await reject_if_needed(query): return
    try:
        count = service.import_dialoghub_accounts(os.environ["DIALOGHUB_DB_PATH"], os.environ["ACCOUNT_SESSIONS_DIR"], os.getenv("ACCOUNT_TITLE_PREFIX", ""))
        await query.message.answer(f"✅ Добавлено/обновлено аккаунтов DialogHub: {count}. Никакая отправка не запускалась.")
    except Exception:
        log.exception("DialogHub import failed")
        await query.message.answer("⚠️ Не удалось импортировать аккаунты. Проверьте пути DIALOGHUB_DB_PATH и ACCOUNT_SESSIONS_DIR.")
    await query.answer()


@dp.callback_query(F.data == "scan")
async def scan(query: CallbackQuery):
    if await reject_if_needed(query): return
    await query.answer("Анализ запущен")
    await query.message.answer("🔎 Анализирую диалоги. Отправки остаются выключенными.")
    async def run():
        totals = await service.scan_all()
        text = "\n".join(f"{key}: {value}" for key, value in sorted(totals.items())) or "Нет доступных диалогов"
        await query.message.answer(f"✅ Анализ завершён:\n{text}")
    asyncio.create_task(run())


@dp.callback_query(F.data == "preview")
async def preview(query: CallbackQuery):
    if await reject_if_needed(query): return
    summary = store.candidate_summary()
    names = {"candidate": "Подходят", "application": "Есть заявка", "refusal": "Явный отказ", "replied": "Ответили", "followup_sent": "Уже был дожим", "too_fresh": "Слишком свежие", "blacklisted": "Blacklist", "no_outbound": "Нет исходящего"}
    lines = [f"{names.get(key, key)}: {value}" for key, value in sorted(summary.items())]
    await query.message.answer("📋 Предпросмотр:\n" + ("\n".join(lines) if lines else "Сначала запустите анализ."))
    await query.answer()


@dp.callback_query(F.data == "stop")
async def stop(query: CallbackQuery):
    if await reject_if_needed(query): return
    store.set_setting("global_paused", "1")
    store.audit("emergency_stop", f"Экстренная остановка пользователем {query.from_user.id}")
    await query.message.answer("🚨 Все новые отправки остановлены. Очередь и история сохранены.")
    await query.answer()


@dp.callback_query(F.data == "status")
async def status(query: CallbackQuery):
    if await reject_if_needed(query): return
    accounts = store.accounts()
    await query.message.answer(f"Аккаунтов: {len(accounts)}\nПауза: {'да' if store.setting('global_paused') == '1' else 'нет'}\nОтправка разрешена: {'да' if store.setting('delivery_enabled') == '1' else 'нет'}")
    await query.answer()


@dp.message(F.text.startswith("/new_task"))
async def new_task(message: Message):
    if await reject_if_needed(message): return
    name = message.text.removeprefix("/new_task").strip()
    if not name:
        await message.answer("Использование: /new_task Название тестовой выборки")
        return
    task_id = store.create_task(name)
    store.audit("task_created", f"Создана задача: {name}")
    await message.answer(f"✅ Создана задача #{task_id} «{name}». В неё добавлены текущие кандидаты, но задача выключена.")


@dp.message(F.text == "/tasks")
async def tasks(message: Message):
    if await reject_if_needed(message): return
    rows = store.tasks()
    if not rows:
        await message.answer("Задач пока нет. Сначала проанализируйте диалоги, затем используйте /new_task.")
        return
    lines = [f"#{r['id']} {r['name']} — {'включена' if r['enabled'] else 'выключена'}, кандидатов: {r['queued'] or 0}, отправлено: {r['sent'] or 0}" for r in rows]
    await message.answer("\n".join(lines))


@dp.message(F.text.startswith("/enable_task"))
async def enable_task(message: Message):
    if await reject_if_needed(message): return
    try:
        task_id = int(message.text.rsplit(maxsplit=1)[1])
    except (ValueError, IndexError):
        await message.answer("Использование: /enable_task <номер>. Требуется отдельное снятие глобальной паузы на сервере.")
        return
    if store.setting("global_paused") == "1" or store.setting("delivery_enabled") != "1":
        await message.answer("Задача не включена: глобальная защита отправки активна. Сначала согласуйте тест и снимите защиту явным действием оператора.")
        return
    if store.set_task_enabled(task_id, True):
        store.audit("task_enabled", f"Задача {task_id} включена")
        await message.answer(f"Задача #{task_id} включена.")
    else:
        await message.answer("Задача не найдена.")


async def delivery_loop():
    while True:
        try:
            await service.run_delivery_tick()
        except Exception:
            log.exception("Delivery tick failed")
        await asyncio.sleep(30)


async def main():
    if not ADMINS:
        raise RuntimeError("ADMIN_IDS is required; bot will not accept control without it")
    bot = Bot(os.environ["BOT_TOKEN"])
    asyncio.create_task(delivery_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
