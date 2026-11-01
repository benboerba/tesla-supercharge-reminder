import unittest
from unittest.mock import patch
from auto_monitor import transition, cycle, INTERVAL, granted_scopes, scheduled_step, control
from contextlib import nullcontext


class AutoMonitorTests(unittest.TestCase):
    def test_fixed_station_only_notifies_full(self):
        for kind in ['available','low','unknown','full']:
            s={'enabled':True,'test_station':True,'active':{'key':'1','name':'测试站'},'status':kind}
            alert=(kind,'测试站状态') if kind in ('unknown','full') else None
            with patch('auto_monitor.locked_state',return_value=nullcontext()),patch('auto_monitor.load_state',return_value=s),patch('auto_monitor.cycle',return_value=(s,alert)),patch('auto_monitor.save'),patch('auto_monitor.push') as push:
                scheduled_step(1000)
                self.assertEqual(push.call_count,1 if kind=='full' else 0)

    def test_fixed_station_uses_real_sites_without_vehicle_location(self):
        state={'enabled':True,'test_station':True,'active':{'key':'1','name':'测试站','position':(31,121)}}
        with patch('auto_monitor.api',return_value=[{'vin':'test','state':'online'}]) as api, patch('auto_monitor.sites',return_value=self.nearby()) as sites, patch('auto_monitor.time.time',return_value=1000):
            result,alert=cycle(state,1000)
        api.assert_called_once_with('/api/1/vehicles')
        sites.assert_called_once_with('test')
        self.assertEqual(result['available'],0)
        self.assertEqual(alert[0],'full')

    def test_idle_and_not_due_make_zero_api_calls(self):
        for state in [{}, {'enabled':False}, {'enabled':True,'next_check_at':2000}]:
            with patch('auto_monitor.locked_state',return_value=nullcontext()), patch('auto_monitor.load_state',return_value=state), patch('auto_monitor.cycle') as run:
                self.assertFalse(scheduled_step(1000));run.assert_not_called()

    def test_failed_start_and_departure_disable_task(self):
        for result in [{'enabled':True,'status':'未充电'}, {'enabled':True,'active':None,'status':'已驶离'}]:
            with patch('auto_monitor.locked_state',return_value=nullcontext()), patch('auto_monitor.load_state',return_value={'enabled':True}), patch('auto_monitor.cycle',return_value=(result,None)),patch('auto_monitor.save') as save:
                scheduled_step(1000)
                self.assertFalse(save.call_args[0][1]['enabled'])
                self.assertIsNone(save.call_args[0][1]['next_check_at'])

    def test_manual_stop_clears_task_without_api_calls(self):
        with patch('auto_monitor.locked_state',return_value=nullcontext()),patch('auto_monitor.load_state',return_value={'enabled':True,'active':{'key':'1'}}),patch('auto_monitor.save') as save,patch('auto_monitor.api') as api:
            control('stop');api.assert_not_called()
            self.assertFalse(save.call_args[0][1]['enabled'])
            self.assertIsNone(save.call_args[0][1]['active'])

    def test_active_task_next_poll_is_nine_minutes(self):
        s={'enabled':True,'active':{'key':'1'},'status':'还有空位'}
        with patch('auto_monitor.locked_state',return_value=nullcontext()),patch('auto_monitor.load_state',return_value=s),patch('auto_monitor.cycle',return_value=(s,None)),patch('auto_monitor.save') as save:
            scheduled_step(1000)
            self.assertEqual(save.call_args[0][1]['next_check_at'],1540)

    def vehicle(self, now=1000, charging='Charging', lat=31.0):
        return {'charge_state': {'timestamp': now*1000, 'charging_state': charging,
                'fast_charger_present': True, 'fast_charger_brand': 'Tesla'},
                'drive_state': {'timestamp': now*1000, 'gps_as_of': now, 'latitude': lat, 'longitude': 121.0}}

    def test_new_response_with_old_gps_cannot_match_station(self):
        v=self.vehicle();v['drive_state']['gps_as_of']=100
        state,alert=transition({},v,self.nearby(),1000)
        self.assertIsNone(state.get('active'));self.assertEqual(alert[0],'unknown')

    def nearby(self, count=0, now=1000):
        return {'congestion_sync_time_utc_secs': now, 'superchargers': [
            {'id': 1, 'name': '测试站', 'location': {'lat':31.0,'long':121.0},
             'available_stalls': count, 'total_stalls':6, 'site_closed': False}]}

    def test_starts_on_supercharge_and_alerts_only_zero(self):
        for count in [0,1,2,6]:
            state, alert=transition({}, self.vehicle(), self.nearby(count),1000)
            self.assertEqual(state['active']['key'],'1')
            self.assertEqual(alert is not None,count==0)
        self.assertEqual(INTERVAL,540)

    def test_no_start_for_home_or_stale_position(self):
        v=self.vehicle();v['charge_state']['fast_charger_brand']='Other'
        self.assertIsNone(transition({},v,self.nearby(),1000)[0].get('active'))
        v=self.vehicle();v['drive_state']['timestamp']=100
        state,alert=transition({},v,self.nearby(),1000)
        self.assertIsNone(state.get('active'));self.assertEqual(alert[0],'unknown')

    def test_ambiguous_nearby_does_not_choose_closest(self):
        nearby=self.nearby();s=dict(nearby['superchargers'][0]);s['id']=2
        nearby['superchargers'].append(s)
        state,alert=transition({},self.vehicle(),nearby,1000)
        self.assertIsNone(state.get('active'));self.assertEqual(alert[0],'unknown')

    def test_finished_charging_and_sleep_keep_station(self):
        state,_=transition({},self.vehicle(),self.nearby(3),1000)
        for v in [self.vehicle(charging='Complete'),{}]:
            state,alert=transition(state,v,self.nearby(),1000)
            self.assertEqual(state['active']['key'],'1');self.assertEqual(alert[0],'full')

    def test_missing_occupancy_never_means_free(self):
        state,_=transition({},self.vehicle(),self.nearby(3),1000)
        state,alert=transition(state,{},self.nearby(3),2000)
        self.assertIsNone(state['available']);self.assertEqual(alert[0],'unknown')

    def test_departure_needs_distinct_samples(self):
        state,_=transition({},self.vehicle(),self.nearby(3),1000)
        v=self.vehicle(1100,'Disconnected',31.02)
        state,_=transition(state,v,self.nearby(3,1100),1100)
        state,_=transition(state,v,self.nearby(3,1100),1100)
        self.assertIsNotNone(state['active'])
        v=self.vehicle(1200,'Disconnected',31.02)
        state,_=transition(state,v,self.nearby(3,1200),1200)
        self.assertIsNone(state['active']);self.assertIsNone(state['available'])

    def test_asleep_vehicle_not_woken(self):
        with patch('auto_monitor.api',return_value=[{'vin':'test','state':'asleep'}]) as api:
            with patch('auto_monitor.sites') as sites:
                cycle({},1000)
                api.assert_called_once_with('/api/1/vehicles');sites.assert_not_called()

    def test_scope_claim(self):
        import base64,json
        value=base64.urlsafe_b64encode(json.dumps({'scp':['vehicle_location']}).encode()).decode().rstrip('=')
        self.assertIn('vehicle_location',granted_scopes({'access_token':'x.'+value+'.x'}))
        self.assertEqual(granted_scopes({'access_token':'bad'}),[])
