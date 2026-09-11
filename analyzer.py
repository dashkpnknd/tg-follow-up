"""Pure, conservative conversation classification used before every send."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
from typing import Iterable

DEFAULT_FOLLOWUP = "Фиксирую отказ?"
MIN_FOLLOWUP_AGE = timedelta(hours=48)
APPLICATION_MARKERS = ("@gelikky", "@localtraffic")
DEFAULT_STOP_WORDS = (
    "неинтересно", "не интересно", "не актуально", "неактуально", "не нужно",
    "не надо", "нам не нужно", "нам не интересно", "не рассматриваем",
    "не рассматриваю", "не интересует", "спасибо, не нужно", "откажусь",
    "отказ", "уберите", "не пишите", "больше не пишите", "удалите номер",
    "отпишите", "стоп",
)
BLACKLIST_WORDS = ("не пишите", "больше не пишите", "удалите номер", "уберите", "отпишите", "стоп")


class DialogStatus(str, Enum):
    CANDIDATE = "candidate"
    APPLICATION = "application"
    REFUSAL = "refusal"
    REPLIED = "replied"
    FOLLOWUP_SENT = "followup_sent"
    NO_OUTBOUND = "no_outbound"
    TOO_FRESH = "too_fresh"
    BLACKLISTED = "blacklisted"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Message:
    id: int
    date: datetime
    outgoing: bool
    text: str = ""


@dataclass(frozen=True)
class Decision:
    status: DialogStatus
    reason: str
    relevant_outbound_at: datetime | None = None
    blacklisting_phrase: str | None = None


def normalized(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _matches(text: str, phrases: Iterable[str]) -> str | None:
    haystack = normalized(text)
    for phrase in phrases:
        needle = normalized(phrase)
        if needle and needle in haystack:
            return phrase
    return None


def classify(
    messages: Iterable[Message],
    *,
    stop_words: Iterable[str] = DEFAULT_STOP_WORDS,
    is_blacklisted: bool = False,
    followup_text: str = DEFAULT_FOLLOWUP,
    now: datetime | None = None,
    min_age: timedelta = MIN_FOLLOWUP_AGE,
) -> Decision:
    """Classify a dialog without inferring intent from silence.

    A candidate is strictly a private conversation with an outbound message, no
    later inbound message, no known application/refusal/follow-up, and 48 hours
    elapsed. This is intentionally more conservative than the older finisher.
    """
    history = sorted(messages, key=lambda item: (item.date, item.id))
    if not history:
        return Decision(DialogStatus.UNDETERMINED, "История диалога пуста")
    if is_blacklisted:
        return Decision(DialogStatus.BLACKLISTED, "Пользователь находится в общем blacklist")

    all_text = "\n".join(item.text for item in history)
    marker = _matches(all_text, APPLICATION_MARKERS)
    if marker:
        return Decision(DialogStatus.APPLICATION, f"Найден признак заявки: {marker}")

    if _matches(all_text, (followup_text,)):
        return Decision(DialogStatus.FOLLOWUP_SENT, "Дожим уже найден в истории Telegram")

    outgoing = [item for item in history if item.outgoing]
    if not outgoing:
        return Decision(DialogStatus.NO_OUTBOUND, "Нет нашего исходящего сообщения")
    relevant = outgoing[-1]

    incoming_after = [item for item in history if not item.outgoing and item.date >= relevant.date]
    if incoming_after:
        refusal = next((_matches(item.text, stop_words) for item in incoming_after if _matches(item.text, stop_words)), None)
        blacklist_phrase = next((_matches(item.text, BLACKLIST_WORDS) for item in incoming_after if _matches(item.text, BLACKLIST_WORDS)), None)
        if refusal:
            return Decision(DialogStatus.REFUSAL, f"Клиент написал: «{refusal}»", relevant.date, blacklist_phrase)
        return Decision(DialogStatus.REPLIED, "После исходящего сообщения есть ответ клиента", relevant.date)

    current = now or datetime.now(timezone.utc)
    date = relevant.date if relevant.date.tzinfo else relevant.date.replace(tzinfo=timezone.utc)
    if current.astimezone(timezone.utc) - date.astimezone(timezone.utc) < min_age:
        return Decision(DialogStatus.TOO_FRESH, "С последнего исходящего сообщения прошло менее 48 часов", relevant.date)
    return Decision(DialogStatus.CANDIDATE, "Нет ответа, заявки, отказа или прошлого дожима; прошло не менее 48 часов", relevant.date)
