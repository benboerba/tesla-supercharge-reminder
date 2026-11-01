"""Owner-only read-only Supercharger monitor; one cycle every nine minutes."""
import argparse
import base64
import json
import fcntl
import math
import time
import urllib.parse
from tesla_watch import api, sites, classify, station_key, read, save, request, PRIVATE

INTERVAL = 9 * 60


def granted_scopes(tokens):
    # Local preflight only; Tesla validates the actual signed token on every request.
    scope = tokens.get('scope')
    if scope is None:
        try:
            payload = tokens['access_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            scope = claims.get('scp', claims.get('scope', []))
        except (ValueError, KeyError, IndexError):
            return []
    return scope.split() if isinstance(scope, str) else (scope if isinstance(scope, list) else [])


def fresh_ms(timestamp, now):
    return (type(timestamp) in (int, float) and math.isfinite(timestamp)
            and -60 <= now - timestamp / 1000 <= 180)


def position(vehicle, now):
    drive = vehicle.get('drive_state', {})
    lat, lon = drive.get('latitude'), drive.get('longitude')
    gps = drive.get('gps_as_of')
    if type(gps) not in (int, float) or not fresh_ms(gps * 1000, now):
        return None
    if (not fresh_ms(drive.get('timestamp'), now) or type(lat) not in (int, float)
            or type(lon) not in (int, float) or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        return None
    return (lat, lon)


def distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dx, dy = math.radians(b[0]-a[0]), math.radians(b[1]-a[1])
    h = math.sin(dx/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dy/2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def site_position(site):
    p = site.get('location', {})
    lat, lon = p.get('lat'), p.get('long')
    if (type(lat) in (int,float) and type(lon) in (int,float)
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        return lat, lon
    return None


def is_supercharging(vehicle, now):
    c = vehicle.get('charge_state', {})
    return (fresh_ms(c.get('timestamp'), now) and c.get('charging_state') == 'Charging'
            and c.get('fast_charger_present') is True
            and str(c.get('fast_charger_brand', '')).lower() == 'tesla')


def select_station(vehicle, nearby, now):
    p = position(vehicle, now)
    if not p or not is_supercharging(vehicle, now):
        return None
    # Do not blindly choose the nearest station. Require a close unique match.
    candidates = [s for s in nearby.get('superchargers', [])
                  if site_position(s) and distance(p, site_position(s)) <= 300]
    if (len(candidates) != 1 or candidates[0].get('site_closed')
            or distance(p, site_position(candidates[0])) > 150):
        return None
    return candidates[0]


def transition(state, vehicle, nearby, now):
    state = dict(state)
    state['checked_at'] = now
    charge = vehicle.get('charge_state', {})
    state['charging_state'] = charge.get('charging_state')
    state['vehicle_available'] = bool(vehicle)
    state['charge_data_fresh'] = fresh_ms(charge.get('timestamp'), now)
    state['position_fresh'] = position(vehicle, now) is not None
    active = state.get('active')
    p = position(vehicle, now)
    if active and p:
        outside = distance(p, active['position']) > 500
        # Two fresh, different position samples are needed to establish departure.
        stamp = vehicle.get('drive_state', {}).get('gps_as_of')
        if outside and stamp != state.get('departure_timestamp'):
            state['departure_samples'] = state.get('departure_samples', 0) + 1
            state['departure_timestamp'] = stamp
        elif not outside:
            state['departure_samples'] = 0
        if state.get('departure_samples', 0) >= 2:
            state['active'] = active = None
            state['departure_samples'] = 0
            state['status'] = '已确认驶离，本次站点监控结束'
            state['ended_reason'] = 'departed'
    if not active:
        state['available'] = state['total'] = None
        state['occupancy_status'] = None
        if not is_supercharging(vehicle, now):
            if state.get('ended_reason') == 'departed':
                state['status'] = '已确认车辆驶离，本次监控已结束'
            elif not vehicle:
                state['status'] = '车辆休眠或离线，无法确认充电状态；请在车辆在线且开始超充后重试'
            elif not state['charge_data_fresh']:
                state['status'] = '充电状态数据缺失或过期，暂不能确认正在超充'
            elif charge.get('charging_state') == 'Disconnected':
                state['status'] = '车辆报告未连接充电枪；请插枪并开始 Tesla 超充后再点开始'
            elif charge.get('charging_state') == 'Complete':
                state['status'] = '车辆报告充电已完成；当前版本需要在超充进行中开启，才能确认本次站点'
            elif charge.get('charging_state') != 'Charging':
                state['status'] = '车辆目前没有在充电；请开始 Tesla 超充后重试'
            else:
                state['status'] = '车辆正在充电，但尚未确认使用 Tesla 超级充电桩'
            return state, None
        chosen = select_station(vehicle, nearby, now)
        if not chosen:
            state['status'] = ('已确认正在 Tesla 超充，但车辆位置缺失或过期' if not p else
                               '已确认正在 Tesla 超充，但无法唯一匹配所在站点')
            return state, ('unknown', state['status'])
        active = {'key': station_key(chosen), 'name': chosen.get('name', '本站'),
                  'position': site_position(chosen), 'started_at': now}
        state['active'] = active
    status, message = classify(nearby, active['key'], now)
    state['occupancy_status'] = status
    state['status'] = message
    state['sync_time'] = nearby.get('congestion_sync_time_utc_secs')
    match = [s for s in nearby.get('superchargers', []) if station_key(s) == active['key']]
    state['available'] = match[0].get('available_stalls') if len(match) == 1 and status != 'unknown' else None
    state['total'] = match[0].get('total_stalls') if len(match) == 1 and status != 'unknown' else None
    # User explicitly requested only zero-space alerts, no low-capacity warnings.
    if status in ('full', 'unknown'):
        return state, (status, active['name'] + '：' + message)
    return state, None


def push(body):
    config = read('notification.json')
    # Fixed public Bark endpoint: never send Tesla credentials or exact GPS to Bark.
    result = request('https://api.day.app/push', {'device_key': config['device_key'],
                     'title': '超充挪车提醒', 'body': body, 'group': 'tesla-charge',
                     'level': 'timeSensitive'})
    if result.get('code') != 200:
        raise RuntimeError('手机推送服务未确认接收')


def cycle(state, now):
    cars = api('/api/1/vehicles')
    if not isinstance(cars, list) or len(cars) != 1:
        raise RuntimeError('车辆列表不唯一，暂停自动识别')
    car = cars[0]
    state['vehicle_name'] = car.get('display_name') or '未命名车辆'
    if state.get('test_station'):
        # Only presence is simulated; occupancy and freshness come from Tesla.
        if not state.get('active'):
            raise RuntimeError('测试站点未配置')
        return transition(state, {}, sites(car['vin']), time.time())
    vehicle = {}
    if car.get('state') == 'online':
        path = '/api/1/vehicles/' + urllib.parse.quote(car['vin'], safe='')
        vehicle = api(path + '/vehicle_data?endpoints=charge_state%3Blocation_data')
    nearby = {}
    if state.get('active') or is_supercharging(vehicle, now):
        nearby = sites(car['vin'])
    return transition(state, vehicle, nearby, time.time())


def locked_state():
    from contextlib import contextmanager
    @contextmanager
    def lock():
        with open(str(PRIVATE/'session.lock'), 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
    return lock()


def load_state():
    return read('auto-state.json') if (PRIVATE/'auto-state.json').exists() else {}


def control(action):
    with locked_state():
        state = load_state()
        if action == 'start' and not state.get('enabled'):
            if not read('notification.json').get('confirmed'):
                raise RuntimeError('手机通知尚未确认')
            state = {'enabled': True, 'status': '正在确认本次超级充电和站点', 'next_check_at': 0,
                     'vehicle_name': state.get('vehicle_name')}
        elif action == 'stop':
            state.update(enabled=False, active=None, available=None, total=None,
                         next_check_at=None, test_station=False, status='本次监控已结束，不再查询车辆')
        save('auto-state.json', state)


def scheduled_step(now=None):
    now = time.time() if now is None else now
    with locked_state():
        state = load_state()
        if not state.get('enabled') or now < state.get('next_check_at', 0):
            return False
        try:
            state, alert = cycle(state, now)
        except (RuntimeError, OSError, ValueError, KeyError):
            state.update(status='监控数据获取失败，无法确认当前空位', checked_at=time.time(),
                         available=None, occupancy_status='unknown')
            alert = ('unknown', state['status'])
        if not state.get('active'):
            state['enabled'] = False
            state['next_check_at'] = None
            if state.get('ended_reason') != 'departed':
                state['status'] += '。未启动持续监控，不会继续查询'
        else:
            state['next_check_at'] = now + INTERVAL
        if state.get('test_station') and alert and alert[0] != 'full':
            alert = None
        if alert and (alert[0] == 'full' or state.get('last_alert') != alert[0]):
            try:
                body = alert[1]
                if state.get('test_station'):
                    body = '【定点测试：真实空位，模拟车辆在站】' + body
                push(body)
                state.update(last_alert=alert[0], last_push_at=time.time(), push_status='推送服务已接收')
            except (RuntimeError, OSError, ValueError):
                state.update(last_alert=None, push_status='推送失败')
        elif not alert:
            state['last_alert'] = None
        save('auto-state.json', state)
        print('MONITOR_SESSION_CHECK_DONE', flush=True)
        return True


def run(once=False):
    if not read('notification.json').get('confirmed'):
        raise RuntimeError('需先确认手机收到测试推送')
    if 'vehicle_location' not in granted_scopes(read('tokens.json')):
        raise RuntimeError('缺少车辆位置权限')
    with open(str(PRIVATE/'monitor.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            scheduled_step()
            if once: return
            # Only check local task state while idle; never call Tesla until explicitly started.
            time.sleep(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    run(args.once)
