"""Public corporate IR calendar. Client roadshows require other surfaces."""
import json
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from bs4 import BeautifulSoup
from app.events.collectors.parsers import source_item, StructuralDrift
from app.events.models import EventFacts
from app.events.normalize import parse_dates, city_name, country_name


def ir_items(data, cfg, url, stamp):
    view = data.get('view')
    if not isinstance(view,dict) or not view.get('NTCTITLE1') or not isinstance(data.get('dateList'),list):
        raise StructuralDrift('Samsung IR detail schema changed')
    content = data.get('content') or {}
    raw = content.get('NOTICFILEDESC','') if isinstance(content,dict) else str(content)
    text = BeautifulSoup(raw,'html.parser').get_text(' ',strip=True)
    dates = [parse_dates(row.get('PROCDT',''))[0] for row in data['dateList']]
    dates = [d for d in dates if d]
    if len(dates) != len(data['dateList']): raise StructuralDrift('Samsung IR dates changed')
    title = view['NTCTITLE1'].strip()
    # Nothing in a calendar title establishes a Swiss venue; unknown location
    # stays unknown. Store the original programme rather than inventing stops.
    city = city_name(text);country = country_name(text)
    facts = EventFacts(title=title,description=text,organizers=[cfg.organizer],
        start_date=min(dates) if dates else None,end_date=max(dates) if len(dates)>1 else None,
        schedule_raw='; '.join(str(d) for d in dates),date_precision='date' if dates else 'unknown',
        city=city,country=country or ('CH' if city else None),official_url=url,
        occurrence_kind='programme' if len(dates)>1 else 'event',event_types=['corporate_ir'])
    return [source_item(cfg,str(view['MENUSEQNO']),url,facts,stamp,identity_urls=[url])]


async def collect_ir(collector, stamp, cursor=None, detail_urls=None):
    cfg,result = collector.config,collector.result
    if detail_urls:
        for url in detail_urls[:cfg.max_details]:
            result.items.extend(ir_items((await collector.http.get(url)).json(),cfg,url,stamp));result.details+=1
        return result
    parsed=urlsplit(cursor or cfg.url);params=parse_qs(parsed.query)
    page=int(params.get('currentPage',['1'])[0]);seen=set()
    for _ in range(cfg.max_pages):
        query={'currentPage':page,'SearchYear':0,'SearchMonth':0,'viewType':'L'}
        url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query),''));collector.current_url=url
        data=(await collector.http.get(url)).json();result.pages+=1
        payload=data.get('page',{})
        rows=payload.get('list')
        if not isinstance(rows,list) or not isinstance(payload.get('total'),int):raise StructuralDrift('Samsung IR list schema changed')
        ids=tuple(str(r.get('MENUSEQNO')) for r in rows)
        if ids in seen and rows:raise StructuralDrift('Samsung IR repeated page')
        seen.add(ids)
        if not rows:
            if payload['total']>0 and page==1:raise StructuralDrift('Samsung IR missing entries')
            result.outcome='empty' if not result.items else 'success';break
        for row in rows:
            sid=str(row.get('MENUSEQNO',''))
            detail=urlunsplit((parsed.scheme,parsed.netloc,'/eng/invest/ir_detail_ajax.do',urlencode({'MenuSeqNo':sid}),''))
            if result.details>=cfg.max_details:
                result.signals.append({'url':detail,'title':row.get('NTCTITLE1',''),'source_id':sid,'reason':'detail_budget'});continue
            try:
                result.items.extend(ir_items((await collector.http.get(detail)).json(),cfg,detail,stamp));result.details+=1
            except Exception as error:
                result.outcome='partial';result.errors.append(type(error).__name__)
                result.signals.append({'url':detail,'title':row.get('NTCTITLE1',''),'source_id':sid,'reason':'detail_retry'})
        if page>=int(payload.get('maxPage',page)):result.cursor=None;break
        page+=1;query['currentPage']=page
        result.cursor=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query),''))
    else:result.complete=False
    return result
