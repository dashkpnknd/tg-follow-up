"""Conservative identity and OR-trigger policy for the future lost-client campaign.

Pure policy: this module never reads Telegram, changes a database, or sends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import unicodedata


MONTHS = {name: i for i, name in enumerate((
    'январь','февраль','март','апрель','май','июнь','июль',
    'август','сентябрь','октябрь','ноябрь','декабрь'), 1)}
RED = {'FFFF0000','FFF4CCCC'}
GREEN = {'FFD9EAD3'}
WHITE = {'FFFFFFFF'}


def norm(value: str) -> str:
    value = unicodedata.normalize('NFKC', value or '').casefold().replace('ё','е')
    return ''.join(ch for ch in value if ch.isalnum())


def month_index(title: str, current_year: int, current_month: int) -> int | None:
    label=title.casefold().replace('ё','е')
    month=next((number for name,number in MONTHS.items() if re.search(r'(?<![а-я])'+name+r'(?![а-я])',label)),None)
    if month is None:return None
    explicit=re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)',label)
    year=int(explicit.group(1)) if explicit else current_year-int(month>current_month)
    return year*12+month


@dataclass(frozen=True)
class SheetRow:
    sheet: str
    channel: str
    project: str
    color: str
    row_number: int


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reasons: tuple[str,...]
    review_reason: str | None = None


def latest_sheet_state(rows: list[SheetRow], current_year: int, current_month: int) -> str | None:
    dated=[]
    for row in rows:
        index=month_index(row.sheet,current_year,current_month)
        if index is None:return None
        dated.append((index,row.color.upper()))
    if not dated:return 'absent'
    latest=max(index for index,_ in dated)
    colors={color for index,color in dated if index==latest}
    if colors & GREEN:return 'working'
    if colors & RED:return 'red'
    if colors & WHITE:return 'white'
    return 'other'


def decide(*,now:datetime,activity_dates:list[datetime|None],sheet_state:str,
           mapping_ambiguous:bool=False,previously_sent:bool=False) -> Decision:
    if previously_sent:return Decision(False,(),'already_sent')
    if mapping_ambiguous:return Decision(False,(),'ambiguous_project_mapping')
    if not activity_dates:return Decision(False,(),'no_reviewed_chats')
    if any(moment is None or moment.tzinfo is None for moment in activity_dates) and sheet_state!='red':
        return Decision(False,(),'unknown_chat_activity')
    silent=bool(activity_dates) and all(moment is not None and moment.tzinfo is not None
                                        and moment < now-timedelta(days=10) for moment in activity_dates)
    red=sheet_state=='red'
    if not silent and not red:return Decision(False,(),'neither_trigger')
    return Decision(True,tuple(reason for reason,enabled in (('ten_days_silent',silent),('red_sheet',red)) if enabled))
