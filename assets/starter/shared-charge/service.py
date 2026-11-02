import sys, os, json, time, re, base64, math, urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tesla_watch import request, BASE, TOKEN, SCOPES
from auto_monitor import transition, is_supercharging, granted_scopes, fresh_ms
import store
import station_flow
CHECK_INTERVAL=60
ORIGIN=os.environ.get('CHARGE_ORIGIN','http://127.0.0.1:5330')
PREFIX='/charge/app/'
CALLBACK=ORIGIN+'/charge/auth/callback'
CONFIG=Path(os.environ.get('CHARGE_APP_CONFIG',str(Path(__file__).resolve().parent.parent/'.private/config.json')))
ALLOW_LEGACY_ACCOUNT=os.environ.get('CHARGE_ALLOW_LEGACY_ACCOUNT')=='1'
LEGACY=Path(os.environ.get('CHARGE_LEGACY_TOKENS',str(Path(__file__).resolve().parent.parent/'.private/tokens.json')))

def config():
    d=json.loads(CONFIG.read_text())
    if d['redirect_uri']!=CALLBACK: raise ValueError('应用回调地址配置不一致，请联系维护者。')
    return d

def subject(tokens):
    try:
        s=tokens['access_token'].split('.')[1]
        return json.loads(base64.urlsafe_b64decode(s+'='*(-len(s)%4))).get('sub')
    except (KeyError,ValueError,IndexError):return None

def authorize(uid,sid,consent=False):
    with store.lock(uid):
        d=store.get(uid)
        if d.get('state',{}).get('enabled'):raise ValueError('请先结束本次监控，再重新授权。')
        store.limited('oauth:'+uid,5,600)
        if consent is not True:raise ValueError('请先阅读并同意数据使用说明。')
        d.pop('auth_error',None)
        d['consent']={'version':'2026-10-27','accepted_at':time.time()};store.put(uid,d)
        c=config(); state=store.oauth_start(uid,sid)
    return 'https://auth.tesla.cn/oauth2/v3/authorize?'+urllib.parse.urlencode(dict(client_id=c['client_id'],redirect_uri=CALLBACK,response_type='code',scope=SCOPES,state=state,locale='zh-CN',prompt_missing_scopes='true',require_requested_scopes='true'))

def persist(d,t):
    if not t.get('access_token') or not t.get('refresh_token'): raise ValueError('授权响应不完整，请重新连接。')
    t=dict(t);t['expires_at']=time.time()+int(t.get('expires_in',0));d['tokens']=t

def callback(state,sid,code,error=None):
    uid=store.oauth_consume(state,sid)
    if error or not code:raise ValueError('你没有完成授权，可以返回页面重新连接。')
    with store.lock(uid):
        d=store.get(uid);c=config()
        t=request(TOKEN,dict(grant_type='authorization_code',client_id=c['client_id'],client_secret=c['client_secret'],code=code,redirect_uri=CALLBACK,audience=BASE),form=True)
        if 'vehicle_location' not in granted_scopes(t):raise ValueError('尚未取得车辆位置权限，请重新授权。')
        sub=subject(t)
        # Temporary owner testing is enabled only by server configuration.
        if sub and LEGACY.exists() and subject(json.loads(LEGACY.read_text()))==sub:
            if not ALLOW_LEGACY_ACCOUNT:
                raise ValueError('此账户已在原单人服务中使用，请联系维护者迁移，暂不重复连接。')
            legacy_state=LEGACY.with_name('auto-state.json')
            if not legacy_state.exists() or json.loads(legacy_state.read_text()).get('enabled'):
                raise ValueError('旧版监控尚未暂停，请先暂停后使用新版测试。')
        with store.db() as conn:
            for r in conn.execute('SELECT id,data FROM users WHERE id<>?',(uid,)):
                other=json.loads(r['data'])
                if sub and subject(other.get('tokens',{}))==sub:
                    raise ValueError('此特斯拉账户已绑定其他试用入口，请使用原入口。')
        d.pop('auth_error',None)
        persist(d,t);d.update(connected=True,vehicles=[],selected=None,state={},authorized_at=time.time())
        store.put(uid,d)
    return uid

class Client:
    def __init__(self,uid,d):self.uid,self.d=uid,d
    def api(self,path):
        if not self.d.get('connected'):raise ValueError('请先连接特斯拉账户。')
        t=self.d['tokens']
        if t['expires_at']<=time.time()+120:
            t=request(TOKEN,dict(grant_type='refresh_token',client_id=config()['client_id'],refresh_token=t['refresh_token']),form=True)
            persist(self.d,t);store.put(self.uid,self.d)
        r=request(BASE+path,token=self.d['tokens']['access_token'])
        if 'response' not in r:raise ValueError('车辆接口响应不完整。')
        return r['response']
    def cars(self):
        cars=self.api('/api/1/vehicles')
        if not isinstance(cars,list):raise ValueError('无法获取车辆列表。')
        return [c for c in cars if isinstance(c,dict) and re.fullmatch('[A-HJ-NPR-Z0-9]{17}',c.get('vin',''))]
    def sites(self,vin):
        r=self.api('/api/1/vehicles/'+vin+'/nearby_charging_sites?detail=true&count=10&radius=50')
        if not isinstance(r,dict) or not isinstance(r.get('superchargers'),list):raise ValueError('空位数据格式无法确认。')
        stamp=r.get('congestion_sync_time_utc_secs')
        if type(stamp) not in (int,float) or not math.isfinite(stamp):r['congestion_sync_time_utc_secs']=None
        return r

def push(key,body,title='超充挪车提醒'):
    r=request('https://api.day.app/push',dict(device_key=key,title=title,body=body,group='tesla-charge',level='timeSensitive'))
    if r.get('code')!=200:raise ValueError('通知服务未确认接收，请检查 Bark 设置。')

def action(uid,kind,data):
    with store.lock(uid):
        d=store.get(uid);s=d.setdefault('state',{})
        if kind=='vehicles':
            store.limited('vehicles:'+uid,3,60)
            if s.get('enabled'):raise ValueError('监控期间不能重新选车，请先停止。')
            cars=Client(uid,d).cars()
            d['vehicles']=[{'id':c['vin'],'name':c.get('display_name') or '我的特斯拉','suffix':c['vin'][-4:]} for c in cars]
            if d.get('selected') not in [c['id'] for c in d['vehicles']]:d['selected']=None
        elif kind=='select':
            if s.get('enabled'):raise ValueError('请先停止监控再换车。')
            if data.get('vehicle') not in [c['id'] for c in d.get('vehicles',[])]:raise ValueError('请选择自己账户中的车辆。')
            d['selected']=data['vehicle'];d['state']={};d.pop('station_cache',None)
        elif kind=='notification':
            if s.get('enabled'):raise ValueError('请先停止监控再修改通知。')
            key=data.get('key','').strip()
            if not re.fullmatch('[A-Za-z0-9_-]{8,128}',key):raise ValueError('请填写 Bark 设备密钥，不是整条网址。')
            d['notification']={'key':key,'confirmed':False}
        elif kind=='test':
            store.limited('push:'+uid,3,300)
            n=d.get('notification',{})
            if not n.get('key'):raise ValueError('请先保存 Bark 设备密钥。')
            push(n['key'],'这是你主动发送的测试通知。收到后请返回网页确认。','超充提醒连接测试')
            n.update(tested_at=time.time(),confirmed=False)
        elif kind=='confirm':
            n=d.get('notification',{})
            if not n.get('tested_at') or time.time()-n['tested_at']>1800:raise ValueError('请先发送一条测试通知。')
            n['confirmed']=True
        elif kind=='confirm_station':
            if not d.get('connected') or not d.get('notification',{}).get('confirmed'):raise ValueError('请先完成授权与手机通知确认。')
            if data.get('confirmed') is not True:raise ValueError('请确认这就是你实际所在的超充站。')
            now=time.time();vehicle,nearby=station_flow.confirm(d,s,data.get('station'),now)
            s,alert=transition(s,vehicle,nearby,now)
            s['checked_at']=d['station_cache']['queried_at']
            deliver(d,s,alert);s.update(enabled=True,phase='monitoring',next_check_at=now+CHECK_INTERVAL)
            d['state']=s;d.pop('station_cache',None)
        elif kind=='start':
            if s.get('enabled'):return
            if not d.get('connected') or not d.get('selected'):raise ValueError('请先授权并选择车辆。')
            if not d.get('notification',{}).get('confirmed'):raise ValueError('请先确认手机收到测试通知。')
            remaining=max(0,int(d.get('retry_after',0)-time.time()+0.999))
            if remaining:raise ValueError('请在 '+str(remaining)+' 秒后重新检查车辆；有效候选站仍可直接确认。')
            mode=data.get('reminder_mode',d.get('reminder_mode','minute'))
            if mode not in ('minute','near','move'):raise ValueError('请选择有效的提醒方式。')
            d['reminder_mode']=mode
            d['retry_after']=time.time()+station_flow.RETRY_SECONDS
            d.pop('station_cache',None)
            d['state']={'enabled':True,'phase':'checking','status':'正在检查车辆与站点，尚未开始空位监控','next_check_at':0}
        elif kind=='stop':
            s.update(enabled=False,phase='stopped',candidates=[],candidate_expires_at=None,next_check_at=None,status='本次监控已结束，不再查询车辆',active=None,available=None,total=None)
        elif kind=='disconnect':
            s.update(enabled=False,phase='stopped',candidates=[],candidate_expires_at=None,next_check_at=None,status='已断开，停止查询车辆',active=None,available=None,total=None)
            d['connected']=False
        else:raise ValueError('不支持的操作。')
        store.put(uid,d)

def snapshot(uid,sid):
    d=store.get(uid);s=d.get('state',{});n=d.get('notification',{})
    # Explicit allowlist: tokens, notification keys, GPS and OAuth transactions never reach the browser.
    visible={k:s.get(k) for k in ['enabled','status','checked_at','next_check_at','available','total','charging_state','occupancy_status','push_status','last_push_at']}
    visible['reminder_mode']=d.get('reminder_mode','minute')
    visible['phase']=s.get('phase') or ('monitoring' if s.get('active') and s.get('enabled') else 'failed' if s.get('checked_at') else 'idle')
    visible['candidates']=s.get('candidates',[]) if s.get('phase')=='awaiting_station' else []
    visible['candidate_expires_at']=s.get('candidate_expires_at')
    visible['match_reason']=s.get('match_reason')
    visible['retry_after']=d.get('retry_after',0)
    visible['server_time']=time.time()
    visible['station']=(s.get('active') or {}).get('name')
    return dict(auth_error=d.get('auth_error'),name=d['name'],connected=d.get('connected',False),vehicles=d.get('vehicles',[]),selected=d.get('selected'),notification=dict(saved=bool(n.get('key')),tested=bool(n.get('tested_at')),confirmed=bool(n.get('confirmed'))),state=visible,csrf=store.digest('csrf:'+sid))

def deliver(d,s,alert):
    active=s.get('active')
    if not active or not d.get('notification',{}).get('confirmed'):return
    available,total=s.get('available'),s.get('total')
    mode=d.get('reminder_mode','minute')
    status=s.get('occupancy_status')
    prior=s.get('sent_band')
    title='超充站每分钟状态'
    if mode=='near':
        if status=='available':s.pop('sent_band',None)
        if status not in ('low','full') or prior==status:return
        title='超充站已满' if status=='full' else '超充站快满了'
    elif mode=='move':
        finished=s.get('charge_data_fresh') and s.get('charging_state') in ('Complete','Stopped') and s.get('plug_present') is True
        if status in ('available','low') or (s.get('charge_data_fresh') and s.get('charging_state')=='Charging'):
            s.pop('move_first',None)
            s.pop('move_repeated',None)
        if status!='full' or not finished:return
        if s.get('move_first') and (s.get('move_repeated') or time.time()-s['move_first']<540):return
        title='本站已满，充电已结束，请挪车'
    stamp=time.strftime('%H:%M:%S',time.gmtime(time.time()+8*3600))
    body=active.get('name','本站')+' · '+stamp+'（北京时间）\n'
    if s.get('occupancy_status')!='unknown' and type(available) is int and type(total) is int and 0<=available<=total:
        body+='空闲 '+str(available)+' / 总计 '+str(total)+' 个桩；不可用 '+str(total-available)+' 个（不一定全部有车占用）。'
        sync=s.get('sync_time')
        if type(sync) in (int,float):body+='\n站点数据更新：'+time.strftime('%H:%M:%S',time.gmtime(sync+8*3600))+'（北京时间）'
        if available==0:body+='\n当前无空闲桩。'
    else:
        body+='本轮数据缺失、过期或查询失败，暂时无法确认占位情况。'
    try:
        push(d['notification']['key'],body,title=title)
        if mode=='near':s['sent_band']=status
        if mode=='move':
            if s.get('move_first'):s['move_repeated']=True
            else:s['move_first']=time.time()
        s.update(last_push_at=time.time(),push_status='通知服务已接收')
    except (RuntimeError,ValueError,OSError):s['push_status']='推送失败，请检查手机通知设置'


def step(uid,now=None):
    now=time.time() if now is None else now
    with store.lock(uid):
        d=store.get(uid);s=d.get('state',{})
        if not d.get('connected') or not s.get('enabled') or now<(s.get('next_check_at') or 0):return False
        try:
            client=Client(uid,d);cars=client.cars()
            match=[c for c in cars if c['vin']==d.get('selected')]
            if len(match)!=1:raise ValueError('选定车辆已不在授权账户中。')
            car=match[0];vehicle={};nearby={}
            if car.get('state')=='online':
                endpoints='charge_state' if s.get('active') else 'charge_state%3Blocation_data'
                vehicle=client.api('/api/1/vehicles/'+car['vin']+'/vehicle_data?endpoints='+endpoints)
            charge=vehicle.get('charge_state',{})
            if charge.get('charging_state')=='Disconnected' and fresh_ms(charge.get('timestamp'),time.time()):
                s.update(enabled=False,phase='stopped',active=None,ended_reason='unplugged',status='已拔下充电枪，本次监控已结束，不再查询车辆',charging_state='Disconnected',checked_at=time.time(),next_check_at=None,available=None,total=None,occupancy_status=None,candidates=[],candidate_expires_at=None)
                d['state']=s;d.pop('station_cache',None);store.put(uid,d);return True
            if s.get('active') or is_supercharging(vehicle,time.time()):nearby=client.sites(car['vin'])
            s,alert=transition(s,vehicle,nearby,time.time())
            s['plug_present']=charge.get('fast_charger_present') is True or charge.get('conn_charge_cable') in ('IEC','SAE','GB_DC','GB_AC','Tesla','US')
            if not s.get('active') and is_supercharging(vehicle,time.time()) and s.get('ended_reason')!='departed':
                station_flow.prepare(d,s,vehicle,nearby,time.time())
            deliver(d,s,alert)
        except (RuntimeError,ValueError,OSError,KeyError,TypeError):
            s.update(checked_at=time.time(),available=None,total=None,occupancy_status='unknown',status='无法获取有效数据，请检查授权或稍后重试；本轮占位情况未知')
            deliver(d,s,None)
        s['enabled']=bool(s.get('active'))
        if s['enabled']:s['phase']='monitoring'
        elif s.get('phase')!='awaiting_station':s['phase']='failed'
        d['retry_after']=max(d.get('retry_after',0),time.time()+station_flow.RETRY_SECONDS)
        s['next_check_at']=time.time()+CHECK_INTERVAL if s['enabled'] else None
        d['state']=s;store.put(uid,d);return True


def record_auth_failure(sid,exc):
    # Store only controlled messages, never raw provider responses, URLs or tokens.
    messages={
      '授权会话已过期或不匹配，请从原浏览器重新连接。':'session',
      '你没有完成授权，可以返回页面重新连接。':'declined',
      '尚未取得车辆位置权限，请重新授权。':'missing_location',
      '此账户已在原单人服务中使用，请联系维护者迁移，暂不重复连接。':'legacy_duplicate',
      '旧版监控尚未暂停，请先暂停后使用新版测试。':'legacy_running',
      '此特斯拉账户已绑定其他试用入口，请使用原入口。':'duplicate',
      '授权响应不完整，请重新连接。':'incomplete_token',
      '应用回调地址配置不一致，请联系维护者。':'callback_config',
      '授权参数不完整':'parameters',
    }
    message=str(exc);code=messages.get(message)
    if not code:
        match=re.fullmatch('服务返回 HTTP ([0-9]{3})；检查授权、限额或服务状态',message)
        if match:code='exchange_http_'+match[1];message='Tesla 授权兑换返回 HTTP '+match[1]+'，请联系维护者检查应用配置或授权状态。'
        else:code='exchange_or_storage';message='授权兑换或保存未完成，请联系维护者检查服务器。'
    uid=store.session(sid)
    if uid:
        with store.lock(uid):
            d=store.get(uid);d['auth_error']={'code':code,'message':message,'at':time.time()};store.put(uid,d)
    print('SHARED_OAUTH_FAILURE '+code,flush=True)
    return code
