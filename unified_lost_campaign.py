"""Paused-by-default combined lost-client campaign for Activity Center.

The operator must enable this explicitly. Each project
gets at most one durable group-and-DM bundle, including after an uncertain
Telegram outcome. Read-only preview does not reserve or send anything.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timedelta,timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

from telethon import functions,types,utils
from telethon.errors import FloodWaitError,RPCError

from analyzer import BLACKLIST_WORDS,normalized
from unified_offer_candidates import (attach_rows,workbook_rows,project_identity,
                                      sheet_identity,drift_cities,drift_project_holds)
from unified_offer_policy import decide,latest_sheet_state,month_index,RED,norm
from store import project_chat_key
from chat_identity import parse as parse_chat_identity

PARTITION=Path('/opt/tg-activity-sender-campaign11')
IDENTITY_REVIEW=Path('/opt/tg-follow-up/unified_identity_exclusions.json')
CONFIRMED_TEAM_IDS={6900450082,674992718,5900412442,6230279745,393466939}
CONFIRMED_TEAM_USERNAMES={'gelikky','ilekseii','workseyl','nnelame','milli_foxy'}
OTHER_TEAM_USERNAMES={'gelik','geliktok','region_marketing','local_manager1'}
UTC=timezone.utc
sys.path.insert(0,str(PARTITION))
from campaign11 import eligible_members,current_blacklist,folder_ids,send_offer_group,norm as group_norm


class UnifiedLostCampaign:
    def __init__(self,store,telegram,renewal,report_bot=None):
        self.store=store;self.telegram=telegram;self.renewal=renewal;self.report_bot=report_bot
        self._schema()

    def _schema(self):
        with self.store.connect() as db:
            db.execute('''create table if not exists unified_lost_deliveries(
                project_key text primary key, reason text not null, status text not null,
                group_ids text not null, contact_id integer not null, template text not null,
                created_at text not null, finished_at text, detail text)''')
            db.execute('''create table if not exists unified_lost_messages(
                project_key text not null, destination_id integer not null, kind text not null,
                random_id integer not null, status text not null, message_id integer,
                primary key(project_key,destination_id,kind))''')
            db.execute('''create table if not exists unified_lost_mentions(
                project_key text not null, group_id integer not null, contact_id integer not null,
                primary key(project_key,group_id,contact_id))''')
            db.execute("create unique index if not exists unified_lost_one_contact on unified_lost_deliveries(contact_id) where contact_id>0")
            db.execute("create unique index if not exists unified_lost_one_group on unified_lost_messages(destination_id) where kind='group'")
            db.execute("create unique index if not exists unified_lost_one_private on unified_lost_messages(destination_id) where kind='private'")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_enabled','0')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_test_one_bundle','0')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_work_start_hour','8')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_work_end_hour','19')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_delay_min_seconds','300')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_delay_max_seconds','1200')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_private_delay_min_seconds','120')")
            db.execute("insert or ignore into settings(key,value) values('unified_lost_private_delay_max_seconds','180')")
            db.execute("insert or ignore into settings(key,value) values('local_traffic_next_at','')")

    def _manifest(self):
        manifest=json.loads((PARTITION/'partition.json').read_text())
        if manifest.get('schema_version')!=2 or not manifest.get('reviewed'):
            raise RuntimeError('Reviewed client mapping is unavailable')
        if IDENTITY_REVIEW.exists():
            review=json.loads(IDENTITY_REVIEW.read_text())
            if review.get('schema_version')!=1 or review.get('source_manifest_at')!=manifest['generated_at']:
                raise RuntimeError('Client identity review does not match the base map')
            inventory={row['id']:row for row in manifest['inventory']}
            project_by_key={project['key']:project for project in manifest['projects']}
            seen=set()
            for item in review['excluded_ambiguous_chats']:
                cid=item['chat_id']
                related={project['key'] for project in manifest['projects'] if cid in project['ambiguous_ids']}
                if (cid in seen or item['kind'] not in ('different_business','team_only','same_project')
                        or cid not in inventory or inventory[cid]['title']!=item['expected_title']
                        or related!=set(item['candidate_project_keys'])
                        or not item.get('participant_ids')
                        or (item['kind'] in ('different_business','same_project') and not
                            (item.get('evidence_message_id') or item.get('evidence_user_id')))):
                    raise RuntimeError('Invalid client identity exclusion')
                if item['kind']=='same_project' and (len(related)!=1 or
                        item.get('target_project_key') not in related or
                        item.get('reference_chat_id') not in
                        project_by_key[item['target_project_key']]['chat_ids'] or
                        not item.get('shared_client_ids')):
                    raise RuntimeError('Invalid related client chat')
                seen.add(cid)
                for key in related:
                    project=project_by_key[key]
                    project['ambiguous_ids'].remove(cid)
                    if item['kind']=='same_project':project['chat_ids'].append(cid)
                    project.setdefault('reviewed_exclusions',[]).append(item)
            for item in review.get('chat_updates',[]):
                cid=item['chat_id'];kind=item['kind']
                identity=parse_chat_identity(item['expected_title'])
                if cid in seen or not item.get('participant_ids') or identity['kind']!='named':
                    raise RuntimeError('Invalid reviewed chat update')
                seen.add(cid)
                if kind=='rename_project_chat':
                    project=project_by_key.get(item['target_project_key'])
                    row=inventory.get(cid)
                    if (project is None or row is None or cid not in project['chat_ids']
                            or row['title']!=item['previous_title']
                            or norm(identity['brand'])!=norm(project['brand'])
                            or not {norm(city) for city in project['cities']}<=
                            {norm(city) for city in identity['cities']}):
                        raise RuntimeError('Invalid reviewed chat rename')
                    row['title']=item['expected_title']
                    project['cities']=identity['cities']
                    project['city']=', '.join(identity['cities'])
                    project.setdefault('reviewed_updates',[]).append(item)
                elif kind=='add_related_chat':
                    project=project_by_key.get(item['target_project_key'])
                    if (project is None or cid in inventory or cid in manifest['folder_ids']
                            or item.get('reference_chat_id') not in project['chat_ids']
                            or norm(identity['brand'])!=norm(project['brand'])
                            or not ({norm(city) for city in identity['cities']}&
                                    {norm(city) for city in project['cities']})
                            or not item.get('shared_client_ids')
                            or not item.get('evidence_message_id')):
                        raise RuntimeError('Invalid reviewed related chat addition')
                    row={'id':cid,'title':item['expected_title'],'activity_known':False,
                         'work_at':None,'identity':identity,'match_evidence':'reviewed_new_named_chat'}
                    manifest['inventory'].append(row);inventory[cid]=row
                    manifest['folder_ids'].append(cid)
                    project['chat_ids'].append(cid)
                    project.setdefault('reviewed_updates',[]).append(item)
                elif kind=='new_named_project':
                    if (cid in inventory or cid in manifest['folder_ids']
                            or item['project_key'] in project_by_key
                            or item['project_key']!=identity['key']
                            or not set(item['ambiguous_ids'])<=set(inventory)):
                        raise RuntimeError('Invalid reviewed new project')
                    row={'id':cid,'title':item['expected_title'],'activity_known':False,
                         'work_at':None,'identity':identity,'match_evidence':'reviewed_new_named_chat'}
                    manifest['inventory'].append(row);inventory[cid]=row
                    manifest['folder_ids'].append(cid)
                    project={'key':item['project_key'],'brand':identity['brand'],
                             'city':', '.join(identity['cities']),'cities':identity['cities'],
                             'chat_ids':[cid],'ambiguous_ids':list(item['ambiguous_ids']),
                             'reviewed_updates':[item]}
                    manifest['projects'].append(project);project_by_key[project['key']]=project
                else:
                    raise RuntimeError('Unknown reviewed chat update')
        keys=[project['key'] for project in manifest['projects']]
        group_ids=[cid for project in manifest['projects'] for cid in project['chat_ids']]
        if len(keys)!=len(set(keys)) or len(group_ids)!=len(set(group_ids)):
            raise RuntimeError('Reviewed map repeats a project or chat')
        return manifest

    async def _verify_reviewed_exclusions(self,client,project,manifest,live_groups,current_folder):
        """A reviewed unrelated chat must still match its Telegram evidence."""
        if not project.get('reviewed_exclusions'):return None
        old={row['id']:row for row in manifest['inventory']}
        config=json.loads((PARTITION/'config.json').read_text())
        confirmed_team=set(config['team_ids'])|CONFIRMED_TEAM_IDS
        from campaign_partition import work_activity
        rules=json.loads((PARTITION/'broadcast_rules.json').read_text())
        for item in project['reviewed_exclusions']:
            cid=item['chat_id']
            dialog=live_groups.get(cid)
            if (cid not in current_folder or dialog is None or cid not in old
                    or dialog.title!=old[cid]['title']):return 'reviewed_related_chat_changed'
            people=await client.get_participants(cid,limit=200)
            if getattr(people,'total',None)!=len(people):return 'reviewed_related_members_incomplete'
            current={user.id for user in people if not getattr(user,'bot',False)
                     and not getattr(user,'deleted',False)}
            if current!=set(item['participant_ids']):return 'reviewed_related_members_changed'
            if item['kind']=='team_only':
                if current-confirmed_team:return 'reviewed_team_only_chat_has_client'
                activity=await work_activity(client,dialog.input_entity,rules,dialog.message)
                if (not activity['activity_known'] or not activity['work_at']
                        or datetime.fromisoformat(activity['work_at'])>=datetime.now(UTC)-timedelta(days=10)):
                    return 'reviewed_team_only_chat_recent_or_unknown_work'
            else:
                if item['kind']=='same_project':
                    reference_id=item['reference_chat_id']
                    reference=live_groups.get(reference_id)
                    if reference is None or reference_id not in current_folder:
                        return 'reviewed_related_reference_changed'
                    reference_people=await client.get_participants(reference_id,limit=200)
                    if getattr(reference_people,'total',None)!=len(reference_people):
                        return 'reviewed_related_reference_incomplete'
                    reference_ids={user.id for user in reference_people if
                                   not getattr(user,'bot',False) and not getattr(user,'deleted',False)}
                    shared=set(item['shared_client_ids'])
                    if (not shared<=current or not shared<=reference_ids or
                            shared&confirmed_team):
                        return 'reviewed_related_client_link_changed'
                if item.get('evidence_user_id'):
                    person=next((user for user in people if user.id==item['evidence_user_id']),None)
                    if (person is None or (person.username or '').casefold()!=
                            item['evidence_username'].casefold()):
                        return 'reviewed_business_contact_changed'
                if item.get('evidence_message_id'):
                    evidence=await client.get_messages(cid,ids=item['evidence_message_id'])
                    excerpt=(evidence.raw_text or '').strip() if evidence else ''
                    length=item.get('evidence_text_length')
                    if length is not None:excerpt=excerpt[:length]
                    if (not evidence or evidence.sender_id!=item['evidence_sender_id']
                            or hashlib.sha256(excerpt.encode()).hexdigest()!=item['evidence_sha256']):
                        return 'reviewed_business_evidence_changed'
        return None

    async def _verify_reviewed_updates(self,client,project,live_groups,current_folder):
        """Recheck each added/renamed chat against its saved identity evidence."""
        for item in project.get('reviewed_updates',[]):
            cid=item['chat_id'];dialog=live_groups.get(cid)
            if (dialog is None or cid not in current_folder
                    or dialog.title!=item['expected_title']):
                return 'reviewed_updated_chat_changed'
            people=await client.get_participants(cid,limit=200)
            if getattr(people,'total',None)!=len(people):return 'reviewed_updated_members_incomplete'
            current={user.id for user in people if not getattr(user,'bot',False)
                     and not getattr(user,'deleted',False)}
            if current!=set(item['participant_ids']):return 'reviewed_updated_members_changed'
            if item['kind']=='add_related_chat':
                reference=live_groups.get(item['reference_chat_id'])
                if reference is None or item['reference_chat_id'] not in current_folder:
                    return 'reviewed_updated_reference_changed'
                reference_people=await client.get_participants(item['reference_chat_id'],limit=200)
                if getattr(reference_people,'total',None)!=len(reference_people):
                    return 'reviewed_updated_reference_incomplete'
                reference_ids={user.id for user in reference_people if
                               not getattr(user,'bot',False) and not getattr(user,'deleted',False)}
                shared=set(item['shared_client_ids'])
                if not shared<=current or not shared<=reference_ids or shared&CONFIRMED_TEAM_IDS:
                    return 'reviewed_updated_client_link_changed'
                message=await client.get_messages(cid,ids=item['evidence_message_id'])
                if (not message or message.sender_id!=item['evidence_sender_id']
                        or hashlib.sha256((message.raw_text or '').strip().encode()).hexdigest()
                        !=item['evidence_sha256']):
                    return 'reviewed_updated_evidence_changed'
        return None

    @staticmethod
    def _live_groups(dialogs):
        return {dialog.id:dialog for dialog in dialogs if dialog.is_group and
                not getattr(dialog.entity,'left',False) and
                not getattr(dialog.entity,'deactivated',False) and
                not getattr(dialog.entity,'migrated_to',None)}

    def _legacy_sent(self,projects):
        with self.store.connect() as db:
            old={sheet_identity(row[0]) for row in db.execute("select project_name from renewal_followups where status='sent'")}
            new={row[0] for row in db.execute("select project_key from unified_lost_deliveries where status!='reserved'")}
        with sqlite3.connect(f'file:{PARTITION/"state.sqlite3"}?mode=ro',uri=True) as db:
            old_group_ids={row[0] for row in db.execute('select chat_id from deliveries')}
        return new|{project['key'] for project in projects if project_identity(project) in old
                    or old_group_ids.intersection(project['chat_ids'])}

    async def _unmatched_red_review(self,rows,unmatched,now):
        """Hold table-only red cases for operator review; never guess a group."""
        cities=set();unknown=False
        for case in unmatched:
            row=case['row']
            if row.color.upper() not in RED:continue
            same_name=[item for item in rows if norm(item.project)==norm(row.project)]
            if latest_sheet_state(same_name,now.year,now.month)!='red':continue
            with self.store.connect() as db:
                past=db.execute('''select status,reason from renewal_followups
                                   where sheet_name=? and row_number=? and project_name=?
                                   order by id desc limit 1''',(row.sheet,row.row_number,row.project)).fetchone()
                handled=bool(past and past['status'] in ('sent','skipped','blocked'))
                if past and past['status']=='merged':
                    reference=re.search(r'запись (\d+)',past['reason'] or '')
                    handled=bool(reference and db.execute("select 1 from renewal_followups where id=? and status='sent'",
                                                          (int(reference.group(1)),)).fetchone())
            if handled:continue
            identity=parse_chat_identity(row.project)
            located={norm(city) for city in identity['cities']}
            if located:cities.update(located)
            else:unknown=True
            await self._report_hold(row.project,
                                    f'Актуальная красная строка {row.sheet}:{row.row_number} не сопоставлена с проверенным чатом')
        return cities,unknown

    def _template(self):
        texts=self.store.renewal_templates('red')
        if not texts:raise RuntimeError('No approved red-row offer templates')
        with self.store.connect() as db:
            sent=db.execute("select count(*) from unified_lost_deliveries where status='sent'").fetchone()[0]
        return texts[sent%len(texts)]

    async def _project_ready(self,client,project,sheet_rows,manifest,previous,now,live_groups,current_folder):
        key=project['key']
        if key in previous:return None,'already_sent_or_reserved'
        if project.get('ambiguous_ids'):return None,'ambiguous_related_chats'
        if not project['chat_ids']:return None,'no_related_chats'
        update_issue=await self._verify_reviewed_updates(client,project,live_groups,current_folder)
        if update_issue:return None,update_issue
        exclusion_issue=await self._verify_reviewed_exclusions(client,project,manifest,live_groups,current_folder)
        if exclusion_issue:return None,exclusion_issue
        old={row['id']:row for row in manifest['inventory']}
        reviewed_folder=set(manifest['folder_ids'])
        from campaign_partition import work_activity,norm as text_norm
        rules=json.loads((PARTITION/'broadcast_rules.json').read_text())
        with self.store.connect() as db:
            archived=[row[0] for row in db.execute("select distinct template from unified_lost_deliveries where template!=''")]
        rules['texts']=list(rules['texts'])+[text_norm(text) for text in self.store.renewal_templates('red')+archived]
        dates=[];groups=[]
        for cid in project['chat_ids']:
            if cid not in old or cid not in reviewed_folder or cid not in current_folder:
                return None,'chat_not_in_reviewed_folder'
            dialog=live_groups.get(cid)
            if dialog is None:return None,'chat_inaccessible'
            entity=dialog.entity
            if (not hasattr(entity,'title') or getattr(entity,'left',False)
                    or getattr(entity,'deactivated',False) or getattr(entity,'migrated_to',None)
                    or text_norm(entity.title)!=text_norm(old[cid]['title'])):
                return None,'chat_changed_since_review'
            groups.append((cid,entity))
            with self.store.connect() as db:
                own_ids={row[0] for row in db.execute('''select message_id from unified_lost_messages
                    where project_key=? and destination_id=? and kind='group' and status='sent' and message_id is not null''',
                    (key,cid))}
            chat_rules={**rules,'ignore_message_ids':own_ids}
            try:
                activity=await work_activity(client,dialog.input_entity,chat_rules,dialog.message)
            except FloodWaitError:
                raise
            except (TimeoutError,OSError,RPCError):
                activity={'activity_known':False,'work_at':None}
            dates.append(datetime.fromisoformat(activity['work_at']) if activity['activity_known'] else None)
        state=latest_sheet_state(sheet_rows,now.year,now.month)
        decision=decide(now=now,activity_dates=dates,sheet_state=state or 'unknown',
                        previously_sent=False,mapping_ambiguous=False)
        return ((groups,decision),None) if decision.eligible else (None,decision.review_reason or 'not_due')

    async def _members(self,client,groups):
        config=json.loads((PARTITION/'config.json').read_text())
        # Confirmed on 2026-11-05; keep this campaign-specific so video is independent.
        team_ids=set(config['team_ids'])|CONFIRMED_TEAM_IDS
        team_names={group_norm(name) for name in config['team_names']}
        team_names|={group_norm(name) for name in self.renewal.excluded_usernames}
        team_names|={group_norm(name) for name in CONFIRMED_TEAM_USERNAMES}
        blacklist=current_blacklist(config)
        members_by_chat={}
        for cid,_ in groups:
            if str(cid) in blacklist:return None,'group_blacklisted'
            raw=[]
            async for user in client.iter_participants(cid):
                raw.append({'id':user.id,'username':user.username,'first_name':user.first_name,
                            'last_name':user.last_name,'bot':user.bot,'deleted':user.deleted})
            members=[member for member in eligible_members(raw,team_ids,team_names,blacklist)
                     if not self.store.is_blacklisted(member['id']) and not
                     self.renewal.is_excluded_contact(
                         member.get('username'),
                         ' '.join(filter(None,(member.get('first_name'),member.get('last_name')))))]
            if not members:return None,'no_external_members'
            if any(not member.get('username') and not
                   ' '.join(filter(None,(member.get('first_name'),member.get('last_name'))))
                   for member in members):return None,'member_without_mention_name'
            members_by_chat[cid]=members
        return members_by_chat,None

    @staticmethod
    def _contact_ids(members_by_chat):
        return {member['id'] for members in members_by_chat.values() for member in members}

    def _reserved_members(self,key):
        with self.store.connect() as db:
            rows=db.execute('select group_id,contact_id from unified_lost_mentions where project_key=?',(key,)).fetchall()
        result={}
        for group_id,contact_id in rows:result.setdefault(group_id,set()).add(contact_id)
        return result

    async def _private_ready(self,client,member,history_limit=None):
        """Preflight each tagged contact before the group message and before its DM."""
        peer_id=member['id']
        if self.store.is_blacklisted(peer_id):return 'blacklisted'
        username=member.get('username')
        name=' '.join(filter(None,(member.get('first_name'),member.get('last_name'))))
        if self.renewal.is_excluded_contact(username,name):return 'excluded_contact'
        try:
            peer=await client.get_input_entity(peer_id)
        except (ValueError,RPCError):
            return 'private_peer_unavailable'
        stop_words=[normalized(item) for item in self.store.active_stop_words()]
        permanent=[normalized(item) for item in BLACKLIST_WORDS]
        index=0
        try:
            async for message in client.iter_messages(peer,limit=history_limit):
                if not message.out:
                    text=normalized(message.message or '')
                    phrases=permanent if index>=100 else stop_words
                    if any(phrase and phrase in text for phrase in phrases):
                        return 'private_opt_out'
                index+=1
        except FloodWaitError:
            raise
        except (TimeoutError,OSError,RPCError):
            return 'private_history_unavailable'
        return 'ready'

    def _reserve(self,key,reasons,members_by_chat,template):
        group_ids=list(members_by_chat)
        contacts=sorted(self._contact_ids(members_by_chat))
        if not contacts:return False
        with self.store.connect() as db:
            db.execute('begin immediate')
            prior=db.execute('select 1 from unified_lost_deliveries where project_key=?',(key,)).fetchone()
            if prior:return False
            if any(db.execute("select 1 from unified_lost_messages where destination_id=? and kind='group'",(cid,)).fetchone() for cid in group_ids):return False
            if any(db.execute("select 1 from unified_lost_messages where destination_id=? and kind='private'",(contact,)).fetchone() for contact in contacts):return False
            db.execute('insert into unified_lost_deliveries values(?,?,?,?,?,?,?,NULL,NULL)',
                       (key,','.join(reasons),'reserved',json.dumps(group_ids),0,template,datetime.now(UTC).isoformat()))
            for cid in group_ids:
                db.execute("insert into unified_lost_messages values(?,?,?,?,'reserved',NULL)",
                           (key,cid,'group',random.randrange(1,2**63)))
                for member in members_by_chat[cid]:
                    db.execute('insert into unified_lost_mentions values(?,?,?)',(key,cid,member['id']))
            for contact in contacts:
                db.execute("insert into unified_lost_messages values(?,?,?,?,'reserved',NULL)",
                           (key,contact,'private',random.randrange(1,2**63)))
        return True

    def _hold_project(self,key,reason):
        now=datetime.now(UTC).isoformat()
        with self.store.connect() as db:
            db.execute("insert or ignore into unified_lost_deliveries values(?,?,'held','[]',0,'',?,?,?)",
                       (key,reason,now,now,reason))
            db.execute("update unified_lost_deliveries set status='held',detail=?,finished_at=? where project_key=? and status='reserved'",
                       (reason,now,key))

    async def _send_stage(self,client,key,members_by_chat,template,kind):
        with self.store.connect() as db:
            pending=db.execute("select destination_id,random_id,status from unified_lost_messages where project_key=? and kind=? and status!='sent' order by destination_id",(key,kind)).fetchall()
        contacts={member['id']:member for members in members_by_chat.values() for member in members}
        for destination,random_id,status in pending:
            if (self.store.setting('unified_lost_enabled','0')!='1'
                    or self.store.setting('global_paused','0')=='1'):
                return 'paused'
            if status!='reserved':
                with self.store.connect() as db:
                    db.execute("update unified_lost_deliveries set status='uncertain',detail='Prior attempt needs manual review' where project_key=?",(key,))
                return 'uncertain'
            with self.store.connect() as db:
                db.execute("update unified_lost_messages set status='attempted' where project_key=? and destination_id=? and kind=?",(key,destination,kind))
            if kind=='group':
                message_id=await send_offer_group(client,destination,members_by_chat[destination],template,random_id)
            else:
                if destination not in contacts:raise RuntimeError('Tagged contact is no longer a project member')
                sent=await client.send_message(await client.get_input_entity(destination),template)
                message_id=int(sent.id)
            with self.store.connect() as db:
                db.execute("update unified_lost_messages set status='sent',message_id=? where project_key=? and destination_id=? and kind=?",(message_id,key,destination,kind))
        if kind=='private':
            with self.store.connect() as db:
                db.execute("update unified_lost_deliveries set status='sent',finished_at=? where project_key=?",(datetime.now(UTC).isoformat(),key))
            return 'sent'
        return 'groups_sent'

    async def run_once(self):
        if self.store.setting('unified_lost_enabled','0')!='1':return 'disabled'
        test_one_bundle=self.store.setting('unified_lost_test_one_bundle','0')=='1'
        test_key=self.store.setting('unified_lost_test_project_key','') if test_one_bundle else ''
        test_target=self.store.setting('unified_lost_test_target_key','') if test_one_bundle else ''
        if test_key:
            with self.store.connect() as db:
                test_row=db.execute('select status from unified_lost_deliveries where project_key=?',(test_key,)).fetchone()
            if not test_row or test_row[0]!='reserved':
                self.store.set_setting('unified_lost_enabled','0')
                return 'test_stopped'
        if self.store.setting('global_paused','0')=='1':return 'global_paused'
        if self.store.setting('renewal_delivery_enabled','0')=='1':return 'old_campaign_enabled'
        now=datetime.now(UTC)
        local=now.astimezone(ZoneInfo(self.store.setting('timezone','Europe/Moscow')))
        start=int(self.store.setting('unified_lost_work_start_hour','8'))
        end=int(self.store.setting('unified_lost_work_end_hour','19'))
        if not 0<=start<end<=24:return 'invalid_schedule'
        if not start<=local.hour<end:return 'outside_hours'
        minimum=int(self.store.setting('unified_lost_delay_min_seconds','300'))
        maximum=int(self.store.setting('unified_lost_delay_max_seconds','1200'))
        if not 60<=minimum<=maximum:return 'invalid_delay'
        private_min=int(self.store.setting('unified_lost_private_delay_min_seconds','120'))
        private_max=int(self.store.setting('unified_lost_private_delay_max_seconds','180'))
        if not 60<=private_min<=private_max<=600:return 'invalid_private_delay'
        with self.store.connect() as db:
            active=db.execute("select project_key from unified_lost_deliveries where status='reserved' order by created_at limit 1").fetchone()
            active_key=active[0] if active else None
            pending_groups=(db.execute("select count(*) from unified_lost_messages where project_key=? and kind='group' and status!='sent'",(active_key,)).fetchone()[0]
                            if active_key else 0)
        if test_key and active_key!=test_key:
            self.store.set_setting('unified_lost_enabled','0')
            return 'test_reservation_changed'
        if (not active_key or pending_groups) and local.hour==end-1 and local.minute>=60-(private_max+30+59)//60:
            return 'outside_hours_for_project_bundle'
        for key in (('unified_lost_next_at',) if active_key and not pending_groups else
                    ('unified_lost_next_at','local_traffic_next_at','checkin_next_delivery_at','renewal_next_delivery_at') if not active_key else ()):
            if test_one_bundle and active_key and key=='unified_lost_next_at':continue
            raw=self.store.setting(key,'')
            if not raw:continue
            try:
                next_at=datetime.fromisoformat(raw)
                if next_at.tzinfo is None:return 'invalid_shared_slot'
                if next_at.astimezone(UTC)>now:return 'waiting_private' if active_key else 'waiting'
            except ValueError:
                return 'invalid_shared_slot'
        if not active_key:
            recent=[sent for sent in (self.store.last_renewal_sent_at(),self.store.last_checkin_sent_at()) if sent]
            if recent and now-max(recent).astimezone(UTC)<timedelta(minutes=40):return 'other_customer_message_recent'
        account=self.store.account_by_title('Local Traffic')
        if not account:return 'sender_unavailable'
        if account['send_status']=='unavailable':return 'sender_unavailable'
        client=self.telegram.client_for_account(int(account['id']))
        if client is None:return 'sender_unavailable'
        manifest=self._manifest();projects=manifest['projects']
        if not projects:return 'no_reviewed_projects'
        rows=workbook_rows(os.environ['RENEWAL_SHEET_XLSX_URL'])
        matching,unmatched=attach_rows(projects,rows)
        table_drift,table_unknown=await self._unmatched_red_review(rows,unmatched,now)
        if table_unknown:return 'unlocated_red_table_project'
        previous=self._legacy_sent(projects)
        await client.connect()
        try:
            if not await client.is_user_authorized():return 'sender_unauthorized'
            dialogs=[dialog async for dialog in client.iter_dialogs()]
            live_groups=self._live_groups(dialogs)
            config=json.loads((PARTITION/'config.json').read_text())
            filters=await client(functions.messages.GetDialogFiltersRequest())
            folder=next((item for item in getattr(filters,'filters',filters) if getattr(item,'id',None)==config['folder_id']),None)
            title=getattr(folder,'title','') if folder else ''
            if group_norm(getattr(title,'text',title))!=group_norm(config['folder_name']):return 'client_folder_renamed'
            current_folder=await folder_ids(client,config['folder_id'],config['folder_name'],
                                            [{'id':cid,'title':dialog.title} for cid,dialog in live_groups.items()])
            current_folder=set(current_folder)&set(live_groups)
            old_titles={row['id']:row['title'] for row in manifest['inventory']}
            current_titles={cid:dialog.title for cid,dialog in live_groups.items()}
            _,unknown,changed=drift_cities(old_titles,current_titles,set(old_titles),set(current_titles))
            folder_drift,folder_unknown,folder_changed=drift_cities(
                old_titles,current_titles,set(manifest['folder_ids']),current_folder)
            changed|=folder_changed
            blocked_by_changed_chat,fallback_cities,identity_unknown=drift_project_holds(
                old_titles,current_titles,changed,projects)
            drift=fallback_cities|table_drift
            unknown=unknown or folder_unknown or identity_unknown
            if unknown:return 'unlocated_new_or_changed_chat'
            if changed:
                await self._report_hold('Состав чатов','Изменения после проверки карты: '+
                                        ', '.join(sorted(blocked_by_changed_chat))[:350])
            cursor=int(self.store.setting('unified_lost_cursor','0'))%len(projects)
            order=([next((index for index,project in enumerate(projects) if project['key']==active_key),-1)]
                   if active_key else
                   [next((index for index,project in enumerate(projects) if project['key']==test_target),-1)]
                   if test_target else [(cursor+offset)%len(projects) for offset in range(min(10,len(projects)))])
            for index in order:
                if index<0:return 'test_project_missing_from_map' if test_target else 'reserved_project_missing_from_map'
                project=projects[index]
                if not active_key:self.store.set_setting('unified_lost_cursor',str((index+1)%len(projects)))
                key=project['key']
                if active_key and key!=active_key:continue
                if key in previous or project.get('ambiguous_ids'):continue
                if key in blocked_by_changed_chat or project_identity(project)[1]&drift:continue
                state=latest_sheet_state(matching.get(key,[]),now.year,now.month)
                if not active_key and state!='red':
                    historical=[datetime.fromisoformat(row['work_at']) for row in manifest['inventory']
                                if row['id'] in project['chat_ids'] and row.get('work_at')]
                    if any(moment>=now-timedelta(days=10) for moment in historical):continue
                checked,reason=await self._project_ready(client,project,matching.get(key,[]),manifest,previous,now,live_groups,current_folder)
                if checked is None:
                    if active_key:
                        with self.store.connect() as db:
                            db.execute("update unified_lost_deliveries set status='held',detail=?,finished_at=? where project_key=?",(reason,datetime.now(UTC).isoformat(),key))
                        await self._report_hold(key,'paused_after_partial_delivery:'+reason)
                        return 'held_after_partial'
                    continue
                groups,decision=checked
                old={row['id']:row for row in manifest['inventory']}
                project_title=old[groups[0][0]]['title']
                with self.store.connect() as db:
                    blocked=db.execute('select 1 from renewal_project_blocks where project_key in (?,?)',
                                       (project_chat_key(project_title),key)).fetchone()
                if blocked:
                    self._hold_project(key,'activity_center_project_blocked')
                    await self._report_hold(key,'activity_center_project_blocked')
                    continue
                members,reason=await self._members(client,groups)
                if members is None:
                    self._hold_project(key,reason)
                    await self._report_hold(key,reason)
                    return reason
                if active_key and self._reserved_members(key)!={
                        cid:{member['id'] for member in selected} for cid,selected in members.items()}:
                    self._hold_project(key,'tagged_member_set_changed')
                    await self._report_hold(key,'tagged_member_set_changed')
                    return 'tagged_member_set_changed'
                contacts={member['id']:member for selected in members.values() for member in selected}
                with self.store.connect() as db:
                    already_sent=[contact for contact in contacts if db.execute(
                        "select 1 from renewal_followups where status='sent' and source_key not like 'unified:%' and target_peer_id=? limit 1",
                        (contact,)).fetchone()]
                    repeated=[contact for contact in contacts if db.execute(
                        "select 1 from unified_lost_messages where kind='private' and destination_id=? and project_key!=? limit 1",
                        (contact,key)).fetchone()]
                if already_sent or repeated:
                    issue='direct_contact_already_received_old_offer' if already_sent else 'direct_contact_already_reserved_for_another_project'
                    self._hold_project(key,issue)
                    await self._report_hold(key,issue)
                    return issue
                for member in contacts.values():
                    preflight=await self._private_ready(client,member,20 if active_key else None)
                    if preflight!='ready':
                        self._hold_project(key,'private_'+preflight)
                        await self._report_hold(key,'private_'+preflight)
                        return 'private_'+preflight
                # The send switch, sheet and live activity are rechecked at
                # the final boundary. No stale preview is permission to send.
                if self.store.setting('unified_lost_enabled','0')!='1':return 'disabled'
                if self.store.setting('global_paused','0')=='1':return 'global_paused'
                newrows=workbook_rows(os.environ['RENEWAL_SHEET_XLSX_URL'])
                newmatch,_=attach_rows([project],newrows)
                fresh,reason=await self._project_ready(client,project,newmatch.get(key,[]),manifest,previous,datetime.now(UTC),live_groups,current_folder)
                if fresh is None or fresh[1].reasons!=decision.reasons:
                    if active_key:
                        self._hold_project(key,'final_project_state_changed')
                        await self._report_hold(key,'final_project_state_changed')
                        return 'final_project_state_changed'
                    continue
                if active_key:
                    with self.store.connect() as db:
                        reserved=db.execute('select template from unified_lost_deliveries where project_key=?',(key,)).fetchone()
                    if not reserved:
                        self._hold_project(key,'reserved_project_missing')
                        return 'reserved_project_missing'
                    template=reserved[0]
                else:
                    template=self._template()
                    if not self._reserve(key,decision.reasons,members,template):continue
                    if test_one_bundle:self.store.set_setting('unified_lost_test_project_key',key)
                # Reserve the sender through the chat -> private bundle. Other
                # Local Traffic campaigns cannot interleave their messages.
                stage='group' if not active_key or pending_groups else 'private'
                if stage=='group':
                    hold_until=(datetime.now(UTC)+timedelta(seconds=private_max+maximum+60)).isoformat()
                    self.store.set_setting('local_traffic_next_at',hold_until)
                else:
                    hold_until=(datetime.now(UTC)+timedelta(seconds=maximum+60)).isoformat()
                    self.store.set_setting('local_traffic_next_at',hold_until)
                try:
                    outcome=await self._send_stage(client,key,members,template,stage)
                except BaseException as exc:
                    with self.store.connect() as db:
                        db.execute("update unified_lost_deliveries set status='uncertain',detail=?,finished_at=? where project_key=?",(f'{type(exc).__name__}: {exc}'[:300],datetime.now(UTC).isoformat(),key))
                    try:
                        await self._report_hold(key,'delivery_result_uncertain')
                    except Exception:
                        pass
                    raise
                if outcome=='groups_sent':
                    due=(datetime.now(UTC)+timedelta(seconds=random.randint(private_min,private_max))).isoformat()
                    self.store.set_setting('unified_lost_next_at',due)
                    return 'partial'
                if outcome=='sent':
                    next_at=(datetime.now(UTC)+timedelta(seconds=random.randint(minimum,maximum))).isoformat()
                    self.store.set_setting('local_traffic_next_at',next_at)
                    self.store.set_setting('unified_lost_next_at',next_at)
                    if test_one_bundle:
                        self.store.set_setting('unified_lost_enabled','0')
                        self.store.set_setting('unified_lost_test_one_bundle','0')
                if outcome=='uncertain':
                    await self._report_hold(key,'delivery_result_uncertain')
                return outcome
            self.store.set_setting('unified_lost_next_at',(datetime.now(UTC)+timedelta(minutes=10)).isoformat())
            return 'empty_or_held'
        finally:
            await client.disconnect()

    async def _report_hold(self,key,reason):
        if self.report_bot is None:return
        chat_id=self.store.setting('renewal_errors_chat_id')
        if not chat_id:return
        with self.store.connect() as db:
            found=db.execute('select 1 from audit_log where kind=? and details=?',('unified_hold',key+':'+reason)).fetchone()
            if found:return
        await self.report_bot.send_message(int(chat_id),f'⚠️ Нужна проверка офера\nПроект: {key}\nПричина: {reason}')
        with self.store.connect() as db:
            db.execute('insert into audit_log(at,kind,details) values(?,?,?)',
                       (datetime.now(UTC).isoformat(),'unified_hold',key+':'+reason))
