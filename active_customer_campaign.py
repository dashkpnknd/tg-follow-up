"""One chat and one private check-in for recently active historical customers.

The sales sheet and the reviewed client-chat map are read-only inputs. Sending
is disabled by default; an uncertain Telegram result is never retried.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from telethon import functions, types
from telethon.errors import FloodWaitError, RPCError

from analyzer import BLACKLIST_WORDS, normalized
from chat_identity import norm as title_norm
from customer_checkins import CustomerCheckinService
from unified_offer_candidates import project_identity, sheet_identity

UTC = timezone.utc
PARTITION = Path('/opt/tg-activity-sender-campaign11')
sys.path.insert(0, str(PARTITION))
from campaign11 import folder_ids, norm as group_norm  # reviewed folder implementation


@dataclass(frozen=True)
class SourceOverview:
    projects: dict[str, list[int]]
    unmatched: tuple[tuple[int, str], ...]
    skipped: int


def map_sales_rows(rows: list[list[str]], projects: list[dict],
                   overrides: dict[int, tuple[str, str]] | None = None) -> SourceOverview:
    """Map a sale only when brand and city identify one reviewed project."""
    identities = {item['key']: project_identity(item) for item in projects}
    matched: dict[str, list[int]] = {}
    unmatched = []
    skipped = 0
    overrides = overrides or {}
    for number, row in enumerate(rows, 1):
        name = ' '.join((row[3] if len(row) > 3 else '').split())
        if not name or name.casefold() == 'описание':
            continue
        if not CustomerCheckinService._is_approved_traffic_start(name):
            skipped += 1
            continue
        override = overrides.get(number)
        if override and override[0] == name and override[1] in identities:
            matched.setdefault(override[1], []).append(number)
            continue
        identity = sheet_identity(name)
        if identity is None:
            unmatched.append((number, name))
            continue
        brand, cities = identity
        candidates = [key for key, (known_brand, known_cities) in identities.items()
                      if brand == known_brand and cities and cities <= known_cities]
        if len(candidates) != 1:
            unmatched.append((number, name))
            continue
        matched.setdefault(candidates[0], []).append(number)
    return SourceOverview(matched, tuple(unmatched), skipped)


def recent_client_message(message, team_ids: set[int], cutoff: datetime) -> bool:
    """Only an incoming, non-team message can prove client activity."""
    date = getattr(message, 'date', None)
    sender = getattr(message, 'sender_id', None)
    return bool(date and date.tzinfo and date >= cutoff and sender and
                int(sender) not in team_ids and not getattr(message, 'out', False)
                and not getattr(message, 'action', None))


class ActiveCustomerCampaign:
    def __init__(self, store, telegram, *, source_url: str, sender_title: str,
                 partition: Path = PARTITION):
        self.store = store
        self.telegram = telegram
        self.source_url = source_url
        self.sender_title = sender_title
        self.partition = partition
        self.excluded_usernames = {value.strip().lstrip('@').casefold()
                                   for value in os.getenv('CHECKIN_EXCLUDED_USERNAMES', '').split(',') if value.strip()}
        self.excluded_names = {' '.join(value.casefold().split())
                               for value in os.getenv('CHECKIN_EXCLUDED_NAMES', '').split(',') if value.strip()}
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS active_customer_deliveries (
                    project_key TEXT PRIMARY KEY, group_id INTEGER NOT NULL,
                    contact_id INTEGER NOT NULL, source_rows TEXT NOT NULL,
                    last_client_at TEXT NOT NULL, group_text TEXT NOT NULL,
                    private_text TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active_customer_messages (
                    project_key TEXT NOT NULL, kind TEXT NOT NULL,
                    destination_id INTEGER NOT NULL, random_id INTEGER NOT NULL,
                    status TEXT NOT NULL, message_id INTEGER, sent_at TEXT,
                    PRIMARY KEY(project_key,kind),
                    FOREIGN KEY(project_key) REFERENCES active_customer_deliveries(project_key));
                CREATE UNIQUE INDEX IF NOT EXISTS active_customer_one_contact
                    ON active_customer_deliveries(contact_id);
                CREATE UNIQUE INDEX IF NOT EXISTS active_customer_one_group
                    ON active_customer_deliveries(group_id);
                CREATE TABLE IF NOT EXISTS active_customer_row_overrides (
                    source_row INTEGER PRIMARY KEY, source_name TEXT NOT NULL,
                    project_key TEXT NOT NULL, reviewed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active_customer_skipped (
                    project_key TEXT PRIMARY KEY, reason TEXT NOT NULL,
                    skipped_at TEXT NOT NULL);
            ''')
            for key, value in {
                'active_customer_enabled': '0',
                'active_customer_chat_text': '',
                'active_customer_private_text': '',
                'active_customer_activity_days': '10',
                'active_customer_work_start_hour': '8',
                'active_customer_work_end_hour': '18',
                'active_customer_delay_min_seconds': '2400',
                'active_customer_delay_max_seconds': '3600',
                'active_customer_max_per_day': '5',
                'active_customer_cursor': '0',
                'active_customer_source_keys': '',
            }.items():
                db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, value))

    def _manifest(self) -> dict:
        data = json.loads((self.partition / 'partition.json').read_text())
        if data.get('schema_version') != 2 or data.get('reviewed') is not True:
            raise RuntimeError('Нет проверенной карты клиентских чатов')
        keys = [p['key'] for p in data['projects']]
        chat_ids = [cid for p in data['projects'] for cid in p['chat_ids']]
        if len(keys) != len(set(keys)) or len(chat_ids) != len(set(chat_ids)):
            raise RuntimeError('В проверенной карте есть повтор проекта или чата')
        return data

    def source_overview(self, manifest: dict | None = None) -> SourceOverview:
        if not self.source_url:
            raise RuntimeError('Не задан CHECKIN_SHEET_CSV_URL')
        data = manifest or self._manifest()
        with urlopen(self.source_url, timeout=30) as response:
            content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise RuntimeError('Таблица продаж слишком велика')
        rows = list(csv.reader(content.decode('utf-8-sig').splitlines()))
        with self.store.connect() as db:
            overrides = {row['source_row']: (row['source_name'], row['project_key'])
                         for row in db.execute('SELECT * FROM active_customer_row_overrides')}
        return map_sales_rows(rows, data['projects'], overrides)

    @staticmethod
    def row_digest(name: str) -> str:
        return hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]

    def save_row_override(self, row_number: int, project_index: int,
                          expected_digest: str) -> str:
        """An operator-confirmed match survives only while that sheet row is unchanged."""
        manifest = self._manifest()
        if not 0 <= project_index < len(manifest['projects']):
            raise ValueError('Неизвестный проект')
        overview = self.source_overview(manifest)
        name = next((value for number, value in overview.unmatched if number == row_number), None)
        if not name or self.row_digest(name) != expected_digest:
            raise ValueError('Строка таблицы изменилась; обновите список')
        project = manifest['projects'][project_index]
        with self.store.connect() as db:
            db.execute('''INSERT INTO active_customer_row_overrides VALUES(?,?,?,?)
                ON CONFLICT(source_row) DO UPDATE SET source_name=excluded.source_name,
                project_key=excluded.project_key,reviewed_at=excluded.reviewed_at''',
                (row_number, name, project['key'], datetime.now(UTC).isoformat()))
        return project['key']

    def status(self) -> dict:
        with self.store.connect() as db:
            counts = {row['status']: row['n'] for row in db.execute(
                'SELECT status,COUNT(*) n FROM active_customer_deliveries GROUP BY status')}
            counts['skipped'] = db.execute('SELECT COUNT(*) FROM active_customer_skipped').fetchone()[0]
        return counts

    def _clock(self) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        zone = ZoneInfo(self.store.setting('timezone', 'Europe/Moscow'))
        return now, now.astimezone(zone)

    def _can_send(self) -> str | None:
        if self.store.setting('active_customer_enabled') != '1':
            return 'disabled'
        if self.store.setting('global_paused') == '1':
            return 'global_paused'
        if not self.store.setting('active_customer_chat_text', '').strip():
            return 'missing_chat_text'
        if not self.store.setting('active_customer_private_text', '').strip():
            return 'missing_private_text'
        if not self.store.setting('active_customer_source_keys', ''):
            return 'missing_source_snapshot'
        now, local = self._clock()
        try:
            start = int(self.store.setting('active_customer_work_start_hour', '8'))
            end = int(self.store.setting('active_customer_work_end_hour', '18'))
            minimum = int(self.store.setting('active_customer_delay_min_seconds', '2400'))
            maximum = int(self.store.setting('active_customer_delay_max_seconds', '3600'))
            limit = int(self.store.setting('active_customer_max_per_day', '5'))
            days = int(self.store.setting('active_customer_activity_days', '10'))
        except (TypeError, ValueError):
            return 'invalid_settings'
        if not (0 <= start < end <= 24 and 60 <= minimum <= maximum and
                1 <= days <= 30 and limit >= 0):
            return 'invalid_settings'
        if not start <= local.hour < end:
            return 'outside_hours'
        for key in ('local_traffic_next_at', 'checkin_next_delivery_at',
                    'renewal_next_delivery_at', 'unified_lost_next_at'):
            raw = self.store.setting(key, '')
            if not raw:
                continue
            try:
                due = datetime.fromisoformat(raw)
                if due.tzinfo is None:
                    return 'invalid_shared_slot'
                if due.astimezone(UTC) > now:
                    return 'waiting'
            except ValueError:
                return 'invalid_shared_slot'
        if limit:
            with self.store.connect() as db:
                since = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat()
                sent = db.execute('''SELECT COUNT(*) FROM active_customer_messages
                    WHERE status='sent' AND sent_at>=?''', (since,)).fetchone()[0]
            if sent >= limit:
                return 'daily_limit'
        return None

    def _excluded_user(self, user, team_ids: set[int], team_names: set[str]) -> bool:
        if user is None or getattr(user, 'bot', False) or getattr(user, 'deleted', False):
            return True
        name = ' '.join(part for part in (getattr(user, 'first_name', None),
                                          getattr(user, 'last_name', None)) if part)
        username = (getattr(user, 'username', None) or '').lstrip('@').casefold()
        return (int(user.id) in team_ids or group_norm(username) in team_names or
                group_norm(name) in team_names or username in self.excluded_usernames or
                ' '.join(name.casefold().split()) in self.excluded_names)

    async def _client_activity(self, client, dialog, team_ids: set[int],
                               team_names: set[str], cutoff: datetime):
        """Return the latest real client message; 500 recent messages is a hard scan bound."""
        scanned = 0
        async for message in client.iter_messages(dialog.input_entity, limit=500):
            scanned += 1
            when = getattr(message, 'date', None)
            if when and when.tzinfo and when < cutoff:
                return None
            if recent_client_message(message, team_ids, cutoff):
                sender = await message.get_sender()
                if not self._excluded_user(sender, team_ids, team_names):
                    return message
        if scanned == 500:
            raise RuntimeError('Не удалось проверить всю активность за заданное окно')
        return None

    async def _candidate(self, client, project: dict, manifest: dict,
                         dialogs: dict[int, object], direct: dict[int, object],
                         team_ids: set[int], team_names: set[str], cutoff: datetime,
                         current_folder: set[int]):
        if project.get('ambiguous_ids') or not project['chat_ids']:
            return None, 'ambiguous_project'
        reviewed = {item['id']: item for item in manifest['inventory']}
        folder = set(manifest['folder_ids'])
        options = []
        for cid in project['chat_ids']:
            dialog = dialogs.get(cid)
            old = reviewed.get(cid)
            if (cid not in folder or cid not in current_folder or not dialog or not old
                    or title_norm(dialog.title) != title_norm(old['title'])):
                return None, 'chat_changed_since_review'
            message = await self._client_activity(client, dialog, team_ids, team_names, cutoff)
            if message:
                options.append((message.date, cid, dialog, message))
        if not options:
            return None, 'no_recent_client_message'
        options.sort(key=lambda item: item[0], reverse=True)
        _, cid, dialog, message = options[0]
        external_direct = []
        async for user in client.iter_participants(dialog.entity):
            if user.id in direct and not self._excluded_user(user, team_ids, team_names):
                external_direct.append(user)
        author = direct.get(int(message.sender_id))
        if author and not self._excluded_user(author, team_ids, team_names):
            contact = author
        elif len(external_direct) == 1:
            contact = external_direct[0]
        else:
            return None, 'contact_not_unique'
        if contact.id not in {user.id for user in external_direct}:
            return None, 'contact_not_in_group'
        if self.store.is_blacklisted(int(contact.id)):
            return None, 'contact_blacklisted'
        return (cid, int(contact.id), message.date, dialog), None

    def _already_contacted(self, db: sqlite3.Connection, key: str, cid: int, contact: int) -> bool:
        if db.execute('''SELECT 1 FROM active_customer_deliveries
                       WHERE project_key=? OR group_id=? OR contact_id=? LIMIT 1''',
                      (key, cid, contact)).fetchone():
            return True
        if db.execute("SELECT 1 FROM customer_checkins WHERE target_peer_id=? AND status='sent' LIMIT 1",
                      (contact,)).fetchone():
            return True
        if db.execute("SELECT 1 FROM renewal_followups WHERE target_peer_id=? AND status='sent' LIMIT 1",
                      (contact,)).fetchone():
            return True
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_lost_deliveries'").fetchone():
            if db.execute("SELECT 1 FROM unified_lost_deliveries WHERE contact_id=? AND status IN ('sent','reserved','uncertain','held') LIMIT 1",
                          (contact,)).fetchone():
                return True
        return False

    def _reserve(self, key: str, cid: int, contact: int, rows: list[int],
                 active_at: datetime) -> bool:
        now = datetime.now(UTC).isoformat()
        chat_text = self.store.setting('active_customer_chat_text')
        private_text = self.store.setting('active_customer_private_text')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._already_contacted(db, key, cid, contact):
                return False
            db.execute('''INSERT INTO active_customer_deliveries VALUES
                       (?,?,?,?,?,?,?,'reserved',NULL,?,?)''',
                       (key, cid, contact, json.dumps(rows), active_at.isoformat(),
                        chat_text, private_text, now, now))
            for kind, destination in (('chat', cid), ('private', contact)):
                db.execute('''INSERT INTO active_customer_messages
                           (project_key,kind,destination_id,random_id,status)
                           VALUES(?,?,?,?,'reserved')''',
                           (key, kind, destination, random.randrange(1, 2**63)))
        return True

    async def _private_allowed(self, client, contact: int) -> bool:
        if self.store.is_blacklisted(contact):
            return False
        stop = [normalized(value) for value in self.store.active_stop_words()]
        permanent = [normalized(value) for value in BLACKLIST_WORDS]
        index = 0
        async for message in client.iter_messages(contact, limit=None):
            if not message.out:
                text = normalized(message.message or '')
                phrases = permanent if index >= 100 else stop
                if any(phrase and phrase in text for phrase in phrases):
                    return False
            index += 1
        return True

    async def _dialog_state(self, client, manifest: dict):
        all_dialogs = [dialog async for dialog in client.iter_dialogs()]
        groups = {dialog.id: dialog for dialog in all_dialogs if dialog.is_group and
                  not getattr(dialog.entity, 'left', False) and
                  not getattr(dialog.entity, 'deactivated', False) and
                  not getattr(dialog.entity, 'migrated_to', None)}
        direct = {dialog.entity.id: dialog.entity for dialog in all_dialogs
                  if isinstance(dialog.entity, types.User) and not dialog.entity.bot}
        config = json.loads((self.partition / 'config.json').read_text())
        filters = await client(functions.messages.GetDialogFiltersRequest())
        folder = next((item for item in getattr(filters, 'filters', filters)
                       if getattr(item, 'id', None) == config['folder_id']), None)
        title = getattr(folder, 'title', '') if folder else ''
        if group_norm(getattr(title, 'text', title)) != group_norm(config['folder_name']):
            raise RuntimeError('Папка клиентов переименована или удалена')
        current = await folder_ids(client, config['folder_id'], config['folder_name'],
                                   [{'id': cid, 'title': dialog.title} for cid, dialog in groups.items()])
        current = set(current) & set(groups)
        return (groups, direct, {int(value) for value in config['team_ids']},
                {group_norm(value) for value in config.get('team_names', [])}, current)

    async def preview_live(self) -> dict:
        """Read-only eligibility report, including live client activity."""
        manifest = self._manifest()
        overview = self.source_overview(manifest)
        account = self.store.account_by_title(self.sender_title)
        if not account or account['auth_status'] != 'authorized':
            raise RuntimeError('Аккаунт отправителя недоступен')
        client = self.telegram.client_for_account(int(account['id']))
        if client is None:
            raise RuntimeError('Не удалось открыть аккаунт отправителя')
        result = {'source_projects': len(overview.projects), 'unmatched_rows': len(overview.unmatched),
                  'skipped_rows': overview.skipped, 'recent': 0, 'ready': 0, 'already_contacted': 0,
                  'held': {}, 'samples': [], 'ready_keys': []}
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError('Аккаунт отправителя не авторизован')
            groups, direct, team_ids, team_names, current_folder = await self._dialog_state(client, manifest)
            projects = {p['key']: p for p in manifest['projects']}
            cutoff = datetime.now(UTC) - timedelta(days=int(self.store.setting('active_customer_activity_days', '10')))
            for key in sorted(overview.projects):
                try:
                    candidate, reason = await self._candidate(client, projects[key], manifest,
                        groups, direct, team_ids, team_names, cutoff, current_folder)
                except (TimeoutError, OSError, RPCError, FloodWaitError, RuntimeError):
                    reason, candidate = 'telegram_scan_error', None
                if not candidate:
                    result['held'][reason] = result['held'].get(reason, 0) + 1
                    continue
                result['recent'] += 1
                cid, contact, _, dialog = candidate
                with self.store.connect() as db:
                    contacted = self._already_contacted(db, key, cid, contact)
                if contacted:
                    result['already_contacted'] += 1
                    continue
                result['ready'] += 1
                result['ready_keys'].append(key)
                if len(result['samples']) < 12:
                    result['samples'].append((dialog.title, len(overview.projects[key])))
        finally:
            await client.disconnect()
        return result

    async def _send_message(self, client, key: str, kind: str, destination: int,
                            random_id: int, text: str) -> str:
        with self.store.connect() as db:
            db.execute('''UPDATE active_customer_messages SET status='attempted'
                          WHERE project_key=? AND kind=? AND status='reserved' ''', (key, kind))
        try:
            result = await client(functions.messages.SendMessageRequest(
                peer=await client.get_input_entity(destination), message=text,
                random_id=random_id, no_webpage=True))
            message_id = getattr(result, 'id', None)
            for update in getattr(result, 'updates', []):
                if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                    message_id = update.id
            if message_id is None:
                raise RuntimeError('Telegram не вернул ID сообщения')
        except BaseException as exc:
            with self.store.connect() as db:
                db.execute('''UPDATE active_customer_deliveries SET status='uncertain',
                              reason=?,updated_at=? WHERE project_key=?''',
                           (f'{type(exc).__name__}: {exc}'[:300], datetime.now(UTC).isoformat(), key))
            raise
        with self.store.connect() as db:
            db.execute('''UPDATE active_customer_messages SET status='sent',message_id=?,sent_at=?
                          WHERE project_key=? AND kind=?''',
                       (message_id, datetime.now(UTC).isoformat(), key, kind))
            pending = db.execute('''SELECT COUNT(*) FROM active_customer_messages
                                    WHERE project_key=? AND status!='sent' ''', (key,)).fetchone()[0]
            db.execute('''UPDATE active_customer_deliveries SET status=?,updated_at=?
                          WHERE project_key=?''',
                       ('sent' if not pending else 'partial', datetime.now(UTC).isoformat(), key))
        return 'sent' if not pending else 'partial'

    async def run_once(self) -> str:
        blocked = self._can_send()
        if blocked:
            return blocked
        manifest = self._manifest()
        overview = self.source_overview(manifest)
        try:
            snapshot = set(json.loads(self.store.setting('active_customer_source_keys', '')))
        except (TypeError, ValueError):
            return 'invalid_source_snapshot'
        if not snapshot or not all(isinstance(key, str) for key in snapshot):
            return 'invalid_source_snapshot'
        with self.store.connect() as db:
            handled = {row[0] for row in db.execute('SELECT project_key FROM active_customer_deliveries')}
            handled.update(row[0] for row in db.execute('SELECT project_key FROM active_customer_skipped'))
            partial = db.execute('''SELECT project_key FROM active_customer_deliveries
                WHERE status IN ('reserved','partial') ORDER BY created_at LIMIT 1''').fetchone()
        if not partial and snapshot <= handled:
            return 'complete'
        account = self.store.account_by_title(self.sender_title)
        if not account or account['auth_status'] != 'authorized' or account['send_status'] == 'unavailable':
            return 'sender_unavailable'
        client = self.telegram.client_for_account(int(account['id']))
        if client is None:
            return 'sender_unavailable'
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return 'sender_unauthorized'
            groups, direct, team_ids, team_names, current_folder = await self._dialog_state(client, manifest)
            days = int(self.store.setting('active_customer_activity_days', '10'))
            cutoff = datetime.now(UTC) - timedelta(days=days)
            projects = {p['key']: p for p in manifest['projects']}
            keys = [partial['project_key']] if partial else sorted(snapshot)
            if not partial:
                cursor = int(self.store.setting('active_customer_cursor', '0')) % len(keys)
                keys = keys[cursor:] + keys[:cursor]
            for key in keys:
                if key not in projects or key not in overview.projects:
                    if partial:
                        self._hold(key, 'project_no_longer_in_source')
                        return 'held'
                    self._skip(key, 'project_no_longer_in_source')
                    continue
                if not partial:
                    self.store.set_setting('active_customer_cursor', str((sorted(snapshot).index(key) + 1) % len(keys)))
                    if key in handled:
                        continue
                try:
                    candidate, reason = await self._candidate(client, projects[key], manifest,
                                                               groups, direct, team_ids, team_names, cutoff,
                                                               current_folder)
                except (TimeoutError, OSError, RPCError, FloodWaitError, RuntimeError):
                    return 'telegram_scan_error'
                if candidate is None:
                    if partial:
                        self._hold(key, reason)
                        return 'held'
                    self._skip(key, reason)
                    continue
                cid, contact, active_at, dialog = candidate
                with self.store.connect() as db:
                    existing = db.execute('SELECT * FROM active_customer_deliveries WHERE project_key=?', (key,)).fetchone()
                if existing:
                    cid, contact = int(existing['group_id']), int(existing['contact_id'])
                    if cid not in current_folder or cid not in groups or contact not in direct:
                        self._hold(key, 'reserved_recipient_unavailable')
                        return 'held'
                    reviewed = {item['id']: item['title'] for item in manifest['inventory']}
                    if title_norm(groups[cid].title) != title_norm(reviewed.get(cid, '')):
                        self._hold(key, 'reserved_group_changed')
                        return 'held'
                    members = {user.id async for user in client.iter_participants(groups[cid].entity)}
                    if contact not in members:
                        self._hold(key, 'reserved_contact_left_group')
                        return 'held'
                else:
                    if not self._reserve(key, cid, contact, overview.projects[key], active_at):
                        self._skip(key, 'already_contacted')
                        continue
                with self.store.connect() as db:
                    message = db.execute('''SELECT * FROM active_customer_messages
                        WHERE project_key=? AND status!='sent' ORDER BY CASE kind WHEN 'chat' THEN 0 ELSE 1 END
                        LIMIT 1''', (key,)).fetchone()
                    record = db.execute('SELECT * FROM active_customer_deliveries WHERE project_key=?', (key,)).fetchone()
                if not message:
                    return 'already_complete'
                if message['status'] != 'reserved':
                    self._hold(key, 'uncertain_prior_attempt')
                    return 'held'
                if not await self._private_allowed(client, contact):
                    self._hold(key, 'private_opt_out_or_blacklist')
                    return 'held'
                if message['kind'] == 'private' and contact not in direct:
                    self._hold(key, 'direct_dialog_disappeared')
                    return 'held'
                fresh_gate = self._can_send()
                if fresh_gate:
                    return fresh_gate
                text = record['group_text'] if message['kind'] == 'chat' else record['private_text']
                delay = random.randint(int(self.store.setting('active_customer_delay_min_seconds')),
                                       int(self.store.setting('active_customer_delay_max_seconds')))
                next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                self.store.set_setting('local_traffic_next_at', next_at)
                outcome = await self._send_message(client, key, message['kind'],
                                                   int(message['destination_id']),
                                                   int(message['random_id']), text)
                return outcome
            return 'complete'
        finally:
            await client.disconnect()

    def _hold(self, key: str, reason: str) -> None:
        with self.store.connect() as db:
            db.execute('''UPDATE active_customer_deliveries SET status='held',reason=?,updated_at=?
                          WHERE project_key=?''', (reason, datetime.now(UTC).isoformat(), key))

    def _skip(self, key: str, reason: str) -> None:
        with self.store.connect() as db:
            db.execute('''INSERT OR IGNORE INTO active_customer_skipped VALUES(?,?,?)''',
                       (key, reason, datetime.now(UTC).isoformat()))
