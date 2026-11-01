import os, json, time, secrets, hmac, urllib.parse, threading, fcntl
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import store, service
BASE=Path(__file__).resolve().parent
COOKIE='charge_shared'

def sid(headers):
    try:
        jar=SimpleCookie();jar.load(headers.get('Cookie',''));return jar[COOKIE].value
    except (KeyError,CookieError):return ''

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,data,ctype='application/json; charset=utf-8',headers=None):
        raw=json.dumps(data,ensure_ascii=False).encode() if ctype.startswith('application/json') else data
        self.send_response(status)
        for k,v in {'Content-Type':ctype,'Content-Length':str(len(raw)),'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():self.send_header(k,v)
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def redirect(self,url,headers=None):
        h=dict(headers or {});h['Location']=url;self.reply(303,{},headers=h)
    def cookie(self,s):
        return COOKIE+'='+s+'; Path=/charge/; HttpOnly; SameSite=Lax; Max-Age=2592000'+('' if service.ORIGIN.startswith('http://127.0.0.1:') else '; Secure')
    def valid_host(self):return self.headers.get('Host')==urllib.parse.urlsplit(service.ORIGIN).netloc
    def do_GET(self):
        if not self.valid_host():return self.reply(400,{'error':'地址不匹配'})
        u=urllib.parse.urlsplit(self.path);p=u.path
        if p=='/charge/auth/callback':
            q=urllib.parse.parse_qs(u.query)
            try:
                if len(q.get('state',[]))!=1 or len(q.get('code',[]))>1:raise ValueError('授权参数不完整')
                with store.lock('0'*32):
                    service.callback(q['state'][0],sid(self.headers),q.get('code',[None])[0],q.get('error'))
                return self.redirect(service.PREFIX+'?connected=1')
            except (ValueError,RuntimeError,OSError,KeyError) as exc:
                service.record_auth_failure(sid(self.headers),exc)
                return self.redirect(service.PREFIX+'?auth_error=1')
        if p==service.PREFIX+'api/health':return self.reply(200,{'service':'charge-shared','version':'0.1.0'})
        if p==service.PREFIX+'api/me':
            s=sid(self.headers);uid=store.session(s)
            if not uid:return self.reply(401,{'error':'请先输入邀请码登录'})
            return self.reply(200,service.snapshot(uid,s))
        assets={service.PREFIX:('index.html','text/html; charset=utf-8'),service.PREFIX+'app.js':('app.js','application/javascript'),service.PREFIX+'style.css':('style.css','text/css')}
        if p in assets:
            name,ctype=assets[p];return self.reply(200,(BASE/'static'/name).read_bytes(),ctype)
        self.reply(404,{'error':'页面不存在'})
    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin')!=service.ORIGIN or self.headers.get('Sec-Fetch-Site')=='cross-site':return self.reply(403,{'error':'请从本网站操作'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=4096:raise ValueError('提交内容无效')
            if self.headers.get_content_type()!='application/json':raise ValueError('提交格式无效')
            d=json.loads(self.rfile.read(length).decode())
            if not isinstance(d,dict):raise ValueError('提交格式无效')
            p=urllib.parse.urlsplit(self.path).path
            if p==service.PREFIX+'api/login':
                # Nginx overwrites this header. Direct service access is loopback only.
                ip=self.headers.get('X-Real-IP',self.client_address[0])
                store.limited('login:'+ip,10,600)
                code=d.get('code','')
                if not isinstance(code,str) or len(code)>128:raise ValueError('邀请码格式无效')
                s=store.login(code.strip());return self.reply(200,{'ok':True},headers={'Set-Cookie':self.cookie(s)})
            s=sid(self.headers);uid=store.session(s)
            if not uid:return self.reply(401,{'error':'登录已过期，请重新输入邀请码'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),store.digest('csrf:'+s)):return self.reply(403,{'error':'请刷新页面后重试'})
            if p==service.PREFIX+'api/logout':
                store.logout(s);return self.reply(200,{'ok':True},headers={'Set-Cookie':COOKIE+'=; Path=/charge/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'})
            if p==service.PREFIX+'api/authorize':return self.reply(200,{'url':service.authorize(uid,s,d.get('consent') is True)})
            if p==service.PREFIX+'api/action':
                kind=d.get('action')
                if not isinstance(kind,str):raise ValueError('请选择操作')
                service.action(uid,kind,d);return self.reply(200,{'ok':True})
            return self.reply(404,{'error':'操作不存在'})
        except (ValueError,RuntimeError) as e:return self.reply(400,{'error':str(e)})
        except (OSError,KeyError,TypeError):return self.reply(503,{'error':'操作暂未完成，请稍后重试或联系维护者'})

class Server(ThreadingMixIn,HTTPServer):daemon_threads=True

def worker():
    pending={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            for uid in store.user_ids():
                if uid not in pending or pending[uid].done():
                    if uid in pending:
                        try:pending[uid].result()
                        except Exception:print('SHARED_CHECK_FAILED',flush=True)
                    d=store.get(uid);s=d.get('state',{})
                    if d.get('connected') and s.get('enabled') and time.time()>=(s.get('next_check_at') or 0):pending[uid]=pool.submit(service.step,uid)
            time.sleep(2)

if __name__=='__main__':
    os.umask(0o077);store.init()
    with open(str(store.ROOT/'service.lock'),'w') as process_lock:
        fcntl.flock(process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        threading.Thread(target=worker,daemon=True).start()
        Server(('127.0.0.1',int(os.environ.get('CHARGE_SHARED_PORT','5330'))),Handler).serve_forever()
