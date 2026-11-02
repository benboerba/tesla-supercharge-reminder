import os,sys,time,json,uuid,unittest,threading,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
os.environ['CHARGE_SHARED_DATA']=str(Path(__file__).resolve().parent/'test-data'/uuid.uuid4().hex)
os.environ['CHARGE_ORIGIN']='http://127.0.0.1:15330'
import store,service,server
class SharedTests(unittest.TestCase):
 def setUp(self):
  store.ROOT=Path(__file__).resolve().parent/'test-data'/uuid.uuid4().hex;store.init()
  self.ca=store.create_user('A');self.cb=store.create_user('B')
  self.sa=store.login(self.ca);self.sb=store.login(self.cb);self.a=store.session(self.sa);self.b=store.session(self.sb)
 def ready(self,uid):
  d=store.get(uid);d.update(connected=True,selected='LRW12345678901234',vehicles=[{'id':'LRW12345678901234','name':'car','suffix':'1234'}],tokens={'access_token':uid,'refresh_token':'refresh-'+uid,'expires_at':time.time()+9999},notification={'key':'bark-'+uid,'confirmed':True},state={'enabled':True,'next_check_at':0,'active':{'key':'1','name':'station','position':[31,121]}});store.put(uid,d);return d
 def test_sessions_isolated(self):
  self.assertNotEqual(self.a,self.b);self.assertIsNone(store.session('fake'));store.logout(self.sa);self.assertIsNone(store.session(self.sa));self.assertEqual(store.session(self.sb),self.b)
 def test_oauth_session_binding_replay(self):
  s=store.oauth_start(self.a,self.sa)
  with self.assertRaises(ValueError):store.oauth_consume(s,self.sb)
  self.assertEqual(store.oauth_consume(s,self.sa),self.a)
  with self.assertRaises(ValueError):store.oauth_consume(s,self.sa)
 def test_oauth_new_attempt_invalidates_old(self):
  s=store.oauth_start(self.a,self.sa);new=store.oauth_start(self.a,self.sa)
  with self.assertRaises(ValueError):store.oauth_consume(s,self.sa)
  self.assertEqual(store.oauth_consume(new,self.sa),self.a)
 def test_expired_oauth_rejected(self):
  s=store.oauth_start(self.a,self.sa)
  with store.db() as c:c.execute('UPDATE oauth SET expires=0')
  with self.assertRaises(ValueError):store.oauth_consume(s,self.sa)
 def test_ownership_checked_on_select(self):
  with self.assertRaises(ValueError):service.action(self.a,'select',{'vehicle':'OTHERS_VIN'})
 def test_cannot_start_without_notification(self):
  with self.assertRaises(ValueError):service.action(self.a,'start',{})
 def test_snapshot_no_credentials_or_location(self):
  self.ready(self.a);s=json.dumps(service.snapshot(self.a,self.sa))
  for value in ['refresh-'+self.a,'bark-'+self.a,'position','tokens']:self.assertNotIn(value,s)
 def test_stop_only_own_job(self):
  self.ready(self.a);self.ready(self.b);service.action(self.a,'stop',{})
  self.assertFalse(store.get(self.a)['state']['enabled']);self.assertTrue(store.get(self.b)['state']['enabled'])
 def test_idle_never_calls_tesla(self):
  with patch.object(service,'request') as r:self.assertFalse(service.step(self.a));r.assert_not_called()
 def test_due_time_enforced(self):
  d=self.ready(self.a);d['state']['next_check_at']=time.time()+100;store.put(self.a,d)
  with patch.object(service,'request') as r:self.assertFalse(service.step(self.a));r.assert_not_called()
 def test_minute_reports_all_states_to_current_user(self):
  for status in ['unknown','low','available','full']:
   self.ready(self.a);self.ready(self.b)
   def trans(s,*args):s.update(occupancy_status=status,available=0 if status=='full' else 2,total=6);return s,None
   with patch.object(service.Client,'cars',return_value=[{'vin':'LRW12345678901234','state':'offline'}]),patch.object(service.Client,'sites',return_value={}),patch.object(service,'transition',side_effect=trans),patch.object(service,'push') as push:
    service.step(self.a);push.assert_called_once()
    self.assertEqual(push.call_args[0][0],'bark-'+self.a)
    self.assertIn('暂时无法确认' if status=='unknown' else '空闲 ',push.call_args[0][1])
   self.assertEqual(store.get(self.b)['state']['next_check_at'],0)
   self.assertLess(store.get(self.a)['state']['next_check_at'],time.time()+61)
 def test_query_failure_reports_unknown_without_counts(self):
  self.ready(self.a)
  with patch.object(service.Client,'cars',return_value=[]),patch.object(service.Client,'sites') as sites,patch.object(service,'push') as push:
   service.step(self.a);sites.assert_not_called();push.assert_called_once()
   self.assertIn('暂时无法确认',push.call_args[0][1])
  self.assertIsNone(store.get(self.a)['state']['available'])
 def test_unplug_stops_without_sites_or_more_queries(self):
  d=self.ready(self.a)
  with patch.object(service.Client,'cars',return_value=[{'vin':d['selected'],'state':'online'}]),patch.object(service.Client,'api',return_value={'charge_state':{'charging_state':'Disconnected','timestamp':time.time()*1000}}) as api,patch.object(service.Client,'sites') as sites,patch.object(service,'push') as push:
   service.step(self.a);sites.assert_not_called();push.assert_not_called()
   self.assertNotIn('location_data',api.call_args[0][0])
  s=store.get(self.a)['state'];self.assertFalse(s['enabled']);self.assertEqual(s['phase'],'stopped');self.assertIsNone(s['active'])
  with patch.object(service.Client,'cars') as cars:
   self.assertFalse(service.step(self.a));cars.assert_not_called()
 def test_complete_or_stale_disconnected_keeps_monitoring(self):
  for charging,stamp in [('Complete',time.time()*1000),('Disconnected',1000)]:
   d=self.ready(self.a)
   with patch.object(service.Client,'cars',return_value=[{'vin':d['selected'],'state':'online'}]),patch.object(service.Client,'api',return_value={'charge_state':{'charging_state':charging,'timestamp':stamp}}),patch.object(service.Client,'sites',return_value={}),patch.object(service,'push'):
    service.step(self.a)
   self.assertTrue(store.get(self.a)['state']['enabled'])
 def test_near_transitions_and_move_repeat(self):
  d=self.ready(self.a);s=d['state'];d['reminder_mode']='near'
  with patch.object(service,'push') as push:
   for status in ('low','unknown','low','full','full','available','low'):
    s.update(occupancy_status=status,available=0 if status=='full' else 1,total=6);service.deliver(d,s,None)
   self.assertEqual(push.call_count,3)
   push.reset_mock();d['reminder_mode']='move';s.update(occupancy_status='full',available=0,charge_data_fresh=True,charging_state='Charging',plug_present=True)
   service.deliver(d,s,None);push.assert_not_called();s['charging_state']='Complete';service.deliver(d,s,None);service.deliver(d,s,None)
   self.assertEqual(push.call_count,1);s['move_first']-=541;service.deliver(d,s,None);service.deliver(d,s,None);self.assertEqual(push.call_count,2)
 def test_refresh_only_current_user(self):
  d=self.ready(self.a);other=self.ready(self.b);d['tokens']['expires_at']=0;store.put(self.a,d)
  with patch.object(service,'config',return_value={'client_id':'app'}),patch.object(service,'request',side_effect=[{'access_token':'new','refresh_token':'newrefresh','expires_in':3600},{'response':[]}]) as r:
   with store.lock(self.a):service.Client(self.a,d).cars()
   self.assertEqual(r.call_args_list[0][0][1]['refresh_token'],'refresh-'+self.a)
  self.assertEqual(store.get(self.b)['tokens'],other['tokens']);self.assertEqual(store.get(self.a)['tokens']['refresh_token'],'newrefresh')
 def test_confirm_requires_test(self):
  with self.assertRaises(ValueError):service.action(self.a,'confirm',{})
 def test_disconnect_prevents_calls(self):
  self.ready(self.a);service.action(self.a,'disconnect',{})
  with patch.object(service,'request') as r:self.assertFalse(service.step(self.a));r.assert_not_called()
 def test_rate_limit(self):
  store.limited('x',1,60)
  with self.assertRaises(ValueError):store.limited('x',1,60)
 def test_callback_persists_only_own_tokens(self):
  state=store.oauth_start(self.a,self.sa)
  tokens={'access_token':'opaque-a','refresh_token':'secret-a','expires_in':300,'scope':'openid vehicle_location'}
  with patch.object(service,'config',return_value={'client_id':'app','client_secret':'secret'}),patch.object(service,'request',return_value=tokens):
   self.assertEqual(service.callback(state,self.sa,'code'),self.a)
  self.assertTrue(store.get(self.a)['connected']);self.assertNotIn('tokens',store.get(self.b))
 def test_legacy_owner_testing_toggle(self):
  legacy=store.ROOT/'legacy-tokens.json';legacy.write_text('{"access_token":"owner"}')
  legacy.with_name('auto-state.json').write_text('{"enabled":false}')
  tokens={'access_token':'owner','refresh_token':'owner-refresh','expires_in':300,'scope':'openid vehicle_location'}
  for allowed in [False,True]:
   state=store.oauth_start(self.a,self.sa)
   with patch.object(service,'LEGACY',legacy),patch.object(service,'ALLOW_LEGACY_ACCOUNT',allowed),patch.object(service,'subject',side_effect=lambda t:'owner' if t else None),patch.object(service,'config',return_value={'client_id':'app','client_secret':'secret'}),patch.object(service,'request',return_value=tokens):
    if allowed:self.assertEqual(service.callback(state,self.sa,'code'),self.a)
    else:
     with self.assertRaisesRegex(ValueError,'原单人服务'):service.callback(state,self.sa,'code')
 def test_legacy_override_rejects_running_old_job(self):
  legacy=store.ROOT/'legacy-tokens.json';legacy.write_text('{"access_token":"owner"}');legacy.with_name('auto-state.json').write_text('{"enabled":true}')
  state=store.oauth_start(self.a,self.sa)
  tokens={'access_token':'owner','refresh_token':'r','scope':'vehicle_location'}
  with patch.object(service,'LEGACY',legacy),patch.object(service,'ALLOW_LEGACY_ACCOUNT',True),patch.object(service,'subject',return_value='owner'),patch.object(service,'config',return_value={'client_id':'app','client_secret':'secret'}),patch.object(service,'request',return_value=tokens):
   with self.assertRaisesRegex(ValueError,'旧版监控尚未暂停'):service.callback(state,self.sa,'code')
 def test_declined_callback_never_exchanges(self):
  state=store.oauth_start(self.a,self.sa)
  with patch.object(service,'request') as r:
   with self.assertRaises(ValueError):service.callback(state,self.sa,None,'access_denied')
   r.assert_not_called()
 def test_notification_destination_not_browser_controlled(self):
  self.ready(self.a)
  with patch.object(service,'push') as p:
   service.action(self.a,'test',{'uid':self.b,'key':'attacker'})
   self.assertEqual(p.call_args[0][0],'bark-'+self.a)
 def test_invalid_timestamp_sanitized(self):
  client=service.Client(self.a,{})
  for stamp in [float('nan'),float('inf'),True]:
   with patch.object(client,'api',return_value={'superchargers':[],'congestion_sync_time_utc_secs':stamp}):
    self.assertIsNone(client.sites('LRW12345678901234')['congestion_sync_time_utc_secs'])
 def test_authorize_requests_missing_permissions(self):
  with patch.object(service,'config',return_value={'client_id':'app'}):url=service.authorize(self.a,self.sa,True)
  q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
  self.assertEqual(q['prompt_missing_scopes'],['true']);self.assertEqual(q['require_requested_scopes'],['true'])
 def test_auth_failure_isolated_and_redacted(self):
  service.record_auth_failure(self.sa,ValueError('尚未取得车辆位置权限，请重新授权。'))
  self.assertEqual(store.get(self.a)['auth_error']['code'],'missing_location');self.assertNotIn('auth_error',store.get(self.b))
  service.record_auth_failure(self.sa,RuntimeError('secret-token-example'))
  self.assertNotIn('secret-token-example',json.dumps(store.get(self.a)))
 def station_fixture(self,available=2):
  now=time.time();v={'drive_state':{'latitude':31.0,'longitude':121.0,'gps_as_of':now,'timestamp':now*1000},'charge_state':{'charging_state':'Charging','fast_charger_present':True,'fast_charger_brand':'Tesla','timestamp':now*1000}}
  nearby={'congestion_sync_time_utc_secs':now,'superchargers':[{'id':'test-station','name':'模拟绚荟城','location':{'lat':31.005,'long':121.0},'available_stalls':available,'total_stalls':6,'site_closed':False}]}
  return v,nearby
 def prepare_waiting(self,available=2):
  d=self.ready(self.a);d['state']={'enabled':True,'phase':'checking','next_check_at':0};store.put(self.a,d)
  v,n=self.station_fixture(available)
  with patch.object(service.Client,'cars',return_value=[{'vin':d['selected'],'state':'online'}]),patch.object(service.Client,'api',return_value=v),patch.object(service.Client,'sites',return_value=n),patch.object(service,'push') as p:
   service.step(self.a);p.assert_not_called()
  return store.get(self.a)
 def test_station_offset_offers_real_candidates(self):
  d=self.prepare_waiting();s=d['state']
  self.assertEqual(s['phase'],'awaiting_station');self.assertFalse(s['enabled']);self.assertEqual(s['candidates'][0]['id'],'test-station')
  self.assertGreater(s['candidates'][0]['distance_m'],150);self.assertIn('match_diagnostic',d)
 def test_manual_confirm_reuses_cache_and_anchors_departure(self):
  self.prepare_waiting()
  with patch.object(service,'request') as r,patch.object(service,'push'):
   service.action(self.a,'confirm_station',{'station':'test-station','confirmed':True});r.assert_not_called()
  s=store.get(self.a)['state'];self.assertTrue(s['enabled']);self.assertEqual(s['phase'],'monitoring');self.assertEqual(s['available'],2)
  self.assertEqual(s['active']['position'],[31.0,121.0]);self.assertGreater(s['next_check_at'],time.time()+50)
 def test_cached_fresh_full_notifies_current_user(self):
  self.prepare_waiting(0)
  with patch.object(service,'push') as p:
   service.action(self.a,'confirm_station',{'station':'test-station','confirmed':True})
   self.assertEqual(p.call_args[0][0],'bark-'+self.a)
 def test_cached_stale_occupancy_never_notifies(self):
  d=self.prepare_waiting(0);d['station_cache']['nearby']['congestion_sync_time_utc_secs']=time.time()-400;store.put(self.a,d)
  with patch.object(service,'push') as p:
   service.action(self.a,'confirm_station',{'station':'test-station','confirmed':True});p.assert_called_once();self.assertIn('暂时无法确认',p.call_args[0][1])
  self.assertIsNone(store.get(self.a)['state']['available'])
 def test_expired_selection_requires_recheck(self):
  d=self.prepare_waiting();d['station_cache']['expires']=0;store.put(self.a,d)
  with self.assertRaisesRegex(ValueError,'过期'):service.action(self.a,'confirm_station',{'station':'test-station','confirmed':True})
 def test_forged_station_or_other_user_rejected(self):
  self.prepare_waiting()
  with self.assertRaises(ValueError):service.action(self.a,'confirm_station',{'station':'forged','confirmed':True})
  self.ready(self.b)
  with self.assertRaises(ValueError):service.action(self.b,'confirm_station',{'station':'test-station','confirmed':True})
 def test_stopped_job_cannot_reuse_candidates(self):
  self.prepare_waiting();service.action(self.a,'stop',{})
  with self.assertRaises(ValueError):service.action(self.a,'confirm_station',{'station':'test-station','confirmed':True})
 def test_retry_countdown_and_checking_phase(self):
  d=self.ready(self.a);d['state']={};store.put(self.a,d)
  service.action(self.a,'start',{});self.assertEqual(store.get(self.a)['state']['phase'],'checking')
  service.action(self.a,'stop',{})
  with self.assertRaisesRegex(ValueError,'秒后'):service.action(self.a,'start',{})
 def test_public_candidates_hide_cached_gps(self):
  self.prepare_waiting();d=service.snapshot(self.a,self.sa)
  self.assertTrue(d['state']['candidates']);self.assertNotIn('station_cache',d);self.assertNotIn('latitude',json.dumps(d))
 def test_consent_required(self):
  with self.assertRaises(ValueError):service.authorize(self.a,self.sa,False)
 def test_http_auth_csrf_origin_and_user_override(self):
  http=server.Server(('127.0.0.1',15330),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
  def req(route,data=None,session=None,origin=service.ORIGIN,csrf=None):
   headers={'Origin':origin}
   if session:headers['Cookie']='charge_shared='+session
   if csrf:headers['X-CSRF-Token']=csrf
   if data is not None:headers['Content-Type']='application/json'
   r=urllib.request.Request(service.ORIGIN+service.PREFIX+'api/'+route,data=None if data is None else json.dumps(data).encode(),headers=headers)
   try:
    with urllib.request.urlopen(r) as x:return x.status,json.load(x)
   except urllib.error.HTTPError as x:return x.code,json.load(x)
  try:
   self.assertEqual(req('me')[0],401)
   self.assertEqual(req('action',{'action':'stop'},self.sa,origin='https://evil.example',csrf=store.digest('csrf:'+self.sa))[0],403)
   self.assertEqual(req('action',{'action':'stop'},self.sa)[0],403)
   self.ready(self.a);self.ready(self.b)
   self.assertEqual(req('action',{'action':'stop','uid':self.b},self.sa,csrf=store.digest('csrf:'+self.sa))[0],200)
   self.assertFalse(store.get(self.a)['state']['enabled']);self.assertTrue(store.get(self.b)['state']['enabled'])
   self.assertEqual(req('me',session=self.sa)[1]['name'],'A')
  finally:http.shutdown();http.server_close();thread.join()
if __name__=='__main__':unittest.main()
