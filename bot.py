from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from store import Store
from telegram_service import FollowupService, TelegramSettings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("followup")
ADMINS = {int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()}
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.getenv("ADMIN_CHAT_ID") else None
ACCESS_MODE = os.getenv("ACCESS_MODE", "group_admins").strip().casefold()
store = Store(os.getenv("DATABASE_PATH", "data/followup.sqlite3"))
service = FollowupService(store, TelegramSettings(int(os.environ["API_ID"]), os.environ["API_HASH"], int(os.getenv("HISTORY_LIMIT", "100")), float(os.getenv("SCAN_HISTORY_PAUSE_SECONDS", "10"))))
dp = Dispatcher()
report_bot: Bot | None = None


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


def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows])


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Аккаунты", callback_data="accounts"), InlineKeyboardButton(text="💬 Диалоги", callback_data="dialogs")],
        [InlineKeyboardButton(text="🎯 Кандидаты", callback_data="candidates"), InlineKeyboardButton(text="📬 Очередь и задачи", callback_data="queue")],
        [InlineKeyboardButton(text="⛔ Исключения", callback_data="exceptions"), InlineKeyboardButton(text="🛑 Стоп-слова", callback_data="stop_words")],
        [InlineKeyboardButton(text="✉️ Шаблон", callback_data="template"), InlineKeyboardButton(text="📊 Статистика", callback_data="statistics")],
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
    if ADMIN_CHAT_ID:
        try:
            member = await event.bot.get_chat_member(ADMIN_CHAT_ID, user.id)
            return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        except Exception:
            log.warning("Could not verify group admin %s", user.id, exc_info=True)
            return False
    return bool(ADMINS) and user.id in ADMINS


async def reject_if_needed(event: Message | CallbackQuery) -> bool:
    if await is_allowed(event):
        return False
    text = "Нет доступа. Настройте ADMIN_CHAT_ID группы: доступ получат все её администраторы."
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
    event = next((row for row in store.recent_logs(5) if row["kind"] == "followup_sent"), None)
    if not event:
        return
    account = store.account(event["account_id"])
    account_title = account["title"] if account else "неизвестный аккаунт"
    per_account = store.sent_today(event["account_id"])
    total_sent = store.queue_summary().get("sent", 0)
    await send_report(
        "✉️ Дожим отправлен\n"
        "Текст: «Фиксирую отказ?»\n"
        f"Аккаунт: {account_title}\n"
        f"№ {per_account} с этого аккаунта сегодня · всего отправлено: {total_sent}"
    )


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


@dp.callback_query(F.data == "accounts")
async def accounts(query: CallbackQuery):
    if await reject_if_needed(query): return
    rows = store.accounts()
    labels = [f"• {row['title'] or row['session_name']} — {row['auth_status']}, {'включён' if row['enabled'] else 'выключен'}" for row in rows]
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
    await query.message.answer(f"✉️ Текущий шаблон:\n{store.setting('followup_template')}", reply_markup=kb(("✏️ Изменить", "template_edit"), ("◀️ Меню", "menu")))
    await query.answer()


@dp.callback_query(F.data == "template_edit")
async def template_edit(query: CallbackQuery, state: FSMContext):
    if await reject_if_needed(query): return
    await state.set_state(Flow.template)
    await query.message.answer("Пришлите новый текст шаблона.")
    await query.answer()


@dp.message(Flow.template)
async def template_save(message: Message, state: FSMContext):
    if await reject_if_needed(message): return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не может быть пустым.")
        return
    store.set_setting("followup_template", text)
    store.audit("template_changed", text)
    await state.clear()
    await message.answer("✅ Шаблон сохранён. Он будет применён только к новым задачам.")


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


async def main():
    if ACCESS_MODE not in {"public", "group_admins"}:
        raise RuntimeError("ACCESS_MODE must be public or group_admins")
    global report_bot
    bot = Bot(os.environ["BOT_TOKEN"])
    report_bot = bot
    asyncio.create_task(delivery_loop())
    asyncio.create_task(scan_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
