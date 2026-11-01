"""China Fleet API feasibility tool. No vehicle commands; no third-party dependencies."""
import argparse
from contextlib import contextmanager
import fcntl
import datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / '.private'
BASE = 'https://fleet-api.prd.cn.vn.cloud.tesla.cn'
TOKEN = 'https://auth.tesla.cn/oauth2/v3/token'
SCOPES = 'openid offline_access vehicle_device_data vehicle_location'


def save(name, data):
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(PRIVATE, 0o700)
    # Atomic replacement preserves refresh-token rotation; no credential output.
    pending = PRIVATE / (name + '.pending')
    fd = os.open(str(pending), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, PRIVATE / name)
    os.chmod(PRIVATE / name, 0o600)


def read(name):
    try:
        return json.loads((PRIVATE / name).read_text())
    except FileNotFoundError:
        raise RuntimeError('尚未配置：' + name)


def request(url, payload=None, token=None, form=False):
    headers = {'Accept': 'application/json'}
    body = None
    if payload is not None:
        body = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        # Do not print request URLs, raw responses or credential-bearing payloads.
        raise RuntimeError('服务返回 HTTP %s；检查授权、限额或服务状态' % exc.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError('请求失败或响应无效；不能确认当前空位') from None


def configure():
    client = input('Client ID: ').strip()
    secret = getpass.getpass('Client Secret（隐藏输入）: ').strip()
    redirect = input('已注册的 HTTPS 回调地址: ').strip()
    parsed = urllib.parse.urlsplit(redirect)
    if not client or not secret or parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeError('需要 Client ID、密钥和无查询参数的 HTTPS 回调地址')
    save('config.json', {'client_id': client, 'client_secret': secret, 'redirect_uri': redirect})
    print('配置已保存在本机受限目录，不要发送到聊天或提交到代码仓库。')


def authorize():
    config = read('config.json')
    state = secrets.token_urlsafe(32)
    save('oauth.json', {'state': state, 'created': time.time()})
    params = dict(client_id=config['client_id'], redirect_uri=config['redirect_uri'],
                  response_type='code', scope=SCOPES, state=state, locale='zh-CN')
    print('仅在 Tesla 官方页面登录：')
    print('https://auth.tesla.cn/oauth2/v3/authorize?' + urllib.parse.urlencode(params))


def validate_callback(url, config, pending, now):
    actual = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(config['redirect_uri'])
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
        raise RuntimeError('回调地址不匹配')
    query = urllib.parse.parse_qs(actual.query)
    if pending.get('used') or not pending.get('state') or now - pending['created'] > 600:
        raise RuntimeError('授权流程已使用或过期，请重新发起')
    if not secrets.compare_digest(query.get('state', [''])[0], pending['state']):
        raise RuntimeError('授权 state 不匹配')
    if query.get('error') or len(query.get('code', [])) != 1:
        raise RuntimeError('授权未完成或回调无有效 code')
    return query['code'][0]


@contextmanager
def token_lock():
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(str(PRIVATE / 'tokens.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _persist_token(response):
    if not response.get('access_token') or not response.get('refresh_token'):
        raise RuntimeError('授权响应缺少访问或刷新令牌')
    response['expires_at'] = time.time() + int(response.get('expires_in', 0))
    save('tokens.json', response)


def persist_token(response):
    with token_lock():
        _persist_token(response)


def exchange():
    config, pending = read('config.json'), read('oauth.json')
    callback = getpass.getpass('粘贴授权后的完整回调地址（隐藏输入）: ').strip()
    code = validate_callback(callback, config, pending, time.time())
    result = request(TOKEN, dict(grant_type='authorization_code', code=code,
                     client_id=config['client_id'], client_secret=config['client_secret'],
                     redirect_uri=config['redirect_uri'], audience=BASE), form=True)
    persist_token(result)
    save('oauth.json', {'used': True, 'created': pending['created']})
    print('授权已保存。下一步运行 vehicles / sites 验证中国区真实数据。')


def access_token():
    with token_lock():
        tokens = read('tokens.json')
        if tokens['expires_at'] <= time.time() + 120:
            config = read('config.json')
            result = request(TOKEN, dict(grant_type='refresh_token', client_id=config['client_id'],
                                        refresh_token=tokens['refresh_token']), form=True)
            _persist_token(result)
            tokens = read('tokens.json')
        return tokens['access_token']


def api(path):
    result = request(BASE + path, token=access_token())
    if 'response' not in result:
        raise RuntimeError('接口响应缺少 response')
    return result['response']


def sites(vin):
    data = api('/api/1/vehicles/' + urllib.parse.quote(vin, safe='') + '/nearby_charging_sites?detail=true&count=10&radius=50')
    if not isinstance(data, dict) or not isinstance(data.get('superchargers'), list):
        raise RuntimeError('未返回预期的超充站列表，需核实中国区响应格式')
    return data


def station_key(site):
    if site.get('id') is not None:
        return str(site['id'])
    location = site.get('location', {})
    return '%s|%s|%s' % (site.get('name'), location.get('lat'), location.get('long'))


def classify(data, key, now, max_age=180):
    timestamp = data.get('congestion_sync_time_utc_secs')
    # Generic HTTP/fetch timestamps do not prove occupancy freshness.
    if not isinstance(timestamp, (int, float)) or timestamp > now + 60 or now - timestamp > max_age:
        matches = [s for s in data.get('superchargers', []) if station_key(s) == key]
        if len(matches) == 1:
            site = matches[0]
            available, total = site.get('available_stalls'), site.get('total_stalls')
            if type(available) is int and type(total) is int and 0 <= available <= total and total > 0 and not site.get('site_closed'):
                age = ('约 %s 分钟前的数据' % max(1, int((now-timestamp)/60))) if isinstance(timestamp,(int,float)) and timestamp <= now else '更新时间无法确认'
                return 'unknown', '接口上次数据：空闲 %s / 总桩 %s（%s）；当前空位未确认' % (available,total,age)
        return 'unknown', '空位更新时间缺失或过期，请打开 Tesla App 核实'
    matches = [s for s in data.get('superchargers', []) if station_key(s) == key]
    if len(matches) != 1:
        return 'unknown', '无法唯一找到选定站点，请核实'
    site = matches[0]
    available, total = site.get('available_stalls'), site.get('total_stalls')
    if (site.get('site_closed') or type(available) is not int or type(total) is not int
            or total <= 0 or not 0 <= available <= total):
        return 'unknown', '站点关闭或空位数据异常，请核实'
    if available == 0:
        return 'full', '本站已无空闲桩；如已结束充电，请尽快挪车'
    if available <= 2:
        return 'low', '本站仅剩 %s 个空闲桩，请准备返回' % available
    return 'available', '本站剩余 %s/%s 个空闲桩（不代表费用保证）' % (available, total)


def notify(title, body):
    # Local reminder is only a fallback, not a validated phone delivery channel.
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
    subprocess.run(['/usr/bin/osascript', '-e', script, title, body], capture_output=True, check=False)
    endpoint = os.environ.get('BARK_PUSH_URL', '')
    if not endpoint:
        return False
    if urllib.parse.urlsplit(endpoint).scheme != 'https':
        raise RuntimeError('手机推送地址必须为 HTTPS')
    result = request(endpoint, {'title': title, 'body': body, 'group': '超充挪车提醒'})
    if result.get('code') != 200:
        raise RuntimeError('推送服务未确认接收；请检查手机提醒')
    return True


def watch(vin, key, interval):
    if interval < 60:
        raise RuntimeError('验证阶段查询间隔至少 60 秒，实际限额待确认')
    print('接口与手机提醒尚需实测。保持电脑唤醒；Ctrl+C 结束本次监控。')
    last_status, last_notice = None, 0
    while True:
        now = time.time()
        try:
            status, message = classify(sites(vin), key, now)
        except RuntimeError as exc:
            status, message = 'unknown', str(exc)
        print(datetime.datetime.now().isoformat(timespec='seconds'), status, message, flush=True)
        repeat = status in ('full', 'unknown') and now - last_notice >= 300
        if status != 'available' and (status != last_status or repeat):
            try:
                delivered = notify('超充挪车提醒', message)
                print('推送服务已接收（仍需确认手机到达）' if delivered else '仅尝试了本机提醒：手机通道未配置', flush=True)
                last_notice = now
            except RuntimeError as exc:
                print('提醒发送失败：' + str(exc), flush=True)
                time.sleep(interval)
                continue
        last_status = status
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('configure', 'authorize', 'exchange', 'vehicles', 'test-notify'):
        sub.add_parser(command)
    sub.add_parser('sites').add_argument('vin')
    monitor = sub.add_parser('watch')
    monitor.add_argument('vin')
    monitor.add_argument('station')
    monitor.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    try:
        if args.command == 'configure': configure()
        elif args.command == 'authorize': authorize()
        elif args.command == 'exchange': exchange()
        elif args.command == 'vehicles':
            # VINs are necessary for local setup; never copy this output to public reports.
            for car in api('/api/1/vehicles'):
                print(car.get('display_name', ''), car.get('vin', ''), car.get('state', ''))
        elif args.command == 'sites':
            data = sites(args.vin)
            print('空位数据同步时间：', data.get('congestion_sync_time_utc_secs', '未提供'))
            for site in data['superchargers']:
                print(station_key(site), site.get('name'), site.get('available_stalls'), '/', site.get('total_stalls'))
        elif args.command == 'test-notify':
            result = notify('超充提醒测试', '请确认手机实际收到这条消息，才算提醒通道验证成功。')
            print('推送服务已接收，待手机确认' if result else '尚未配置手机推送，仅尝试本机通知')
        elif args.command == 'watch': watch(args.vin, args.station, args.interval)
    except (RuntimeError, OSError) as exc:
        print('未完成：', exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('\n本次监控已停止。')


if __name__ == '__main__':
    main()
