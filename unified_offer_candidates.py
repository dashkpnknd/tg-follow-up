"""Read-only candidate discovery for the combined lost-client campaign."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from io import BytesIO
from urllib.request import urlopen

from unified_offer_policy import SheetRow,decide,latest_sheet_state,norm


def workbook_rows(xlsx_url: str) -> list[SheetRow]:
    from openpyxl import load_workbook
    with urlopen(xlsx_url,timeout=30) as response:
        content=response.read(20*1024*1024+1)
    if len(content)>20*1024*1024:raise ValueError('Spreadsheet export too large')
    workbook=load_workbook(BytesIO(content),data_only=True,read_only=False)
    result=[]
    for sheet in workbook.worksheets:
        for number in range(1,sheet.max_row+1):
            channel=' '.join(str(sheet.cell(number,2).value or '').split())
            project=' '.join(str(sheet.cell(number,3).value or '').split())
            if channel.casefold() not in ('вк','авито') or not project:continue
            color=sheet.cell(number,3).fill.fgColor
            rgb=(color.rgb or '').upper() if color.type=='rgb' else ''
            result.append(SheetRow(sheet.title,channel,project,rgb,number))
    return result


def sheet_identity(project_name: str):
    # This is the same parser used to build the reviewed chat map.  Ambiguous
    # titles are held rather than matched by a shop-name substring.
    from chat_identity import parse
    identity=parse(project_name)
    if identity['kind']!='named':return None
    return norm(identity['brand']),frozenset(norm(city) for city in identity['cities'])


def project_identity(project:dict):
    brand=norm(project.get('brand') or '')
    cities=frozenset(norm(city) for city in (project.get('cities') or ([project['city']] if project.get('city') else [])))
    if not cities:
        from chat_identity import parse
        identity=parse((project.get('titles') or [''])[0])
        cities=frozenset(norm(city) for city in identity['cities'])
    return brand,cities


def drift_cities(old_titles:dict[int,str],current_titles:dict[int,str],reviewed_ids:set[int],current_ids:set[int]):
    """Cities touched by folder changes; an unlocated client title blocks all."""
    from chat_identity import parse
    changed=(reviewed_ids^current_ids)|{cid for cid in reviewed_ids&current_ids
                                        if old_titles.get(cid)!=current_titles.get(cid)}
    cities=set();unknown=False
    for cid in changed:
        for title in {old_titles.get(cid),current_titles.get(cid)}-{None}:
            identity=parse(title)
            if identity['kind']=='internal':continue
            found={norm(city) for city in identity['cities']}
            if found:cities.update(found)
            else:unknown=True
    return cities,unknown,changed


def drift_project_holds(old_titles:dict[int,str],current_titles:dict[int,str],
                        changed_ids:set[int],projects:list[dict]):
    """Isolate explicitly named changes; city-only changes remain city-wide holds.

    A new shop in Moscow does not make every other named Moscow shop ambiguous.
    A changed or added chat with the same shop and intersecting cities still
    holds that shop until the reviewed project map includes it.
    """
    from chat_identity import parse
    identities={project['key']:project_identity(project) for project in projects}
    blocked=set();fallback_cities=set();unknown=False
    for cid in changed_ids:
        for title in {old_titles.get(cid),current_titles.get(cid)}-{None}:
            identity=parse(title)
            if identity['kind']=='internal':continue
            cities={norm(city) for city in identity['cities']}
            if identity['kind']=='named' and identity['brand'] and cities:
                brand=norm(identity['brand'])
                blocked.update(key for key,(known_brand,known_cities) in identities.items()
                               if known_brand==brand and known_cities&cities)
            elif cities:
                fallback_cities.update(cities)
            else:
                unknown=True
    blocked.update(key for key,(_,cities) in identities.items() if cities&fallback_cities)
    return blocked,fallback_cities,unknown


def attach_rows(projects:list[dict],rows:list[SheetRow]):
    """Exact brand and city evidence; never merges two cities of one chain."""
    matched=defaultdict(list);ambiguous=[]
    identities={project['key']:project_identity(project) for project in projects}
    for row in rows:
        identity=sheet_identity(row.project)
        if identity is None:
            ambiguous.append({'row':row,'reason':'table_name_not_parsed'})
            continue
        brand,cities=identity
        candidates=[key for key,(b,known) in identities.items() if b==brand and cities and cities<=known]
        if len(candidates)!=1:
            ambiguous.append({'row':row,'reason':'project_match_not_unique' if candidates else 'project_not_found'})
            continue
        matched[candidates[0]].append(row)
    return matched,ambiguous


def preview(projects,rows,activity_by_chat,previously_sent,now:datetime):
    matching,unmatched=attach_rows(projects,rows)
    decisions=[]
    for project in projects:
        cid=project['key']
        dates=[activity_by_chat.get(chat_id) for chat_id in project['chat_ids']]
        state=latest_sheet_state(matching.get(cid,[]),now.year,now.month)
        decision=decide(now=now,activity_dates=dates,sheet_state=state or 'unknown',
                        mapping_ambiguous=bool(project.get('ambiguous_ids')),
                        previously_sent=cid in previously_sent)
        decisions.append({'project_key':cid,'chat_ids':project['chat_ids'],
                          'sheet_state':state,'eligible':decision.eligible,
                          'reasons':decision.reasons,'review_reason':decision.review_reason})
    return decisions,unmatched
