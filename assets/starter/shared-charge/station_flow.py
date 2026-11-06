"""Bounded, tenant-local station confirmation using real API candidates only."""
import time
from auto_monitor import position, site_position, distance, is_supercharging
from tesla_watch import station_key
CACHE_SECONDS=180
RETRY_SECONDS=60

def candidates(vehicle,nearby,now):
    p=position(vehicle,now)
    if not p:return [],'车辆位置缺失或过期，暂不能提供可靠的附近站点。'
    rows=nearby_rows(p,nearby)
    close=[x for x in rows if x['distance_m']<=300]
    if len(close)>1:reason='300 米内有多个候选站，无法自动确定本站。'
    elif len(close)==1 and close[0]['closed']:reason='最近候选站被接口标记为关闭，不能自动启用。'
    elif len(close)==1 and close[0]['distance_m']>150:reason='最近候选站超过自动匹配的 150 米范围。'
    else:reason='300 米内未找到唯一可匹配的超充站。'
    return rows,reason

def nearby_rows(p,nearby):
    rows=[]
    for site in nearby.get('superchargers',[]):
        target=site_position(site)
        if not target:continue
        meters=round(distance(p,target))
        if meters<=2000:
            rows.append({'id':station_key(site),'name':site.get('name') or '未命名超充站','distance_m':meters,'closed':bool(site.get('site_closed'))})
    rows.sort(key=lambda x:x['distance_m'])
    return rows[:10]

def prepare(d,s,vehicle,nearby,now):
    rows,reason=candidates(vehicle,nearby,now)
    s['match_reason']=reason;s['candidates']=rows;s['candidate_expires_at']=now+CACHE_SECONDS
    s['phase']='awaiting_station' if any(not x['closed'] for x in rows) else 'failed'
    s['status']=reason+(' 请核对站名并确认你实际所在的站点。' if s['phase']=='awaiting_station' else ' 可稍后重新检查车辆。')
    # Only the latest bounded snapshot; never publish GPS or a location history.
    d['station_cache']={'vehicle':vehicle,'nearby':nearby,'selected':d.get('selected'),'expires':now+CACHE_SECONDS,'queried_at':now}
    d['match_diagnostic']={'checked_at':now,'reason':reason,'candidates':rows}

def confirm(d,s,station,now):
    cache=d.get('station_cache',{})
    if s.get('phase')!='awaiting_station' or cache.get('selected')!=d.get('selected') or now>cache.get('expires',0):
        raise ValueError('候选站已过期，请重新检查车辆后再选站。')
    v=cache['vehicle'];p=position(v,now)
    if not p or not is_supercharging(v,now):raise ValueError('车辆充电或位置数据已过期，请重新检查车辆。')
    choices=[x for x in s.get('candidates',[]) if x['id']==station and not x['closed']]
    sites=[x for x in cache['nearby'].get('superchargers',[]) if station_key(x)==station]
    if len(choices)!=1 or len(sites)!=1:raise ValueError('请选择列表中未关闭的真实站点。')
    # Anchor departure to the confirmed charging position, avoiding map-marker offset.
    s['active']={'key':station,'name':choices[0]['name'],'position':p,'started_at':now,'manual_confirmed':True}
    s['candidates']=[];s['candidate_expires_at']=None;s['phase']='monitoring'
    return v,cache['nearby']
