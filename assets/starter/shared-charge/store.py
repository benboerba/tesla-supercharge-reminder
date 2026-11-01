"""Invite-only tenants. Never accepts a tenant id from an HTTP request."""
import os, json, sqlite3, secrets, hashlib, time, fcntl
from pathlib import Path
from contextlib import contextmanager
ROOT = Path(os.environ.get('CHARGE_SHARED_DATA', str(Path(__file__).resolve().parent / '.private')))

def digest(s): return hashlib.sha256(s.encode()).hexdigest()

def db():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(ROOT), 0o700)
    c = sqlite3.connect(str(ROOT/'shared.sqlite3'), timeout=15)
    c.row_factory = sqlite3.Row
    os.chmod(str(ROOT/'shared.sqlite3'), 0o600)
    return c

def init():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, login_hash TEXT UNIQUE, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, uid TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth(hash TEXT PRIMARY KEY, uid TEXT NOT NULL, session_hash TEXT NOT NULL, expires REAL NOT NULL, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, window REAL, count INTEGER);''')

def create_user(name):
    code=secrets.token_urlsafe(24); uid=secrets.token_hex(16)
    with db() as c:
        c.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,name[:60],digest(code),json.dumps({'state':{},'connected':False})))
    return code

def login(code):
    with db() as c:
        row=c.execute('SELECT id FROM users WHERE login_hash=?',(digest(code),)).fetchone()
        if not row: raise ValueError('邀请码无效，请向邀请人确认。')
        sid=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(sid),row['id'],time.time()+30*86400))
        return sid

def session(sid):
    with db() as c:
        row=c.execute('SELECT uid FROM sessions WHERE hash=? AND expires>?',(digest(sid),time.time())).fetchone()
    return row['uid'] if row else None

def logout(sid):
    with db() as c: c.execute('UPDATE sessions SET expires=0 WHERE hash=?',(digest(sid),))

def get(uid):
    with db() as c: row=c.execute('SELECT name,data FROM users WHERE id=?',(uid,)).fetchone()
    if not row: raise ValueError('用户不存在')
    d=json.loads(row['data']); d['name']=row['name']; return d

def put(uid,d):
    d=dict(d); d.pop('name',None)
    with db() as c: c.execute('UPDATE users SET data=? WHERE id=?',(json.dumps(d,ensure_ascii=False),uid))

@contextmanager
def lock(uid, blocking=True):
    if len(uid)!=32 or any(x not in '0123456789abcdef' for x in uid): raise ValueError('invalid tenant')
    fd=os.open(str(ROOT/(uid+'.lock')),os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as f:
        fcntl.flock(f,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)); yield

def oauth_start(uid,sid):
    state='multi1_'+secrets.token_urlsafe(32)
    with db() as c:
        c.execute('UPDATE oauth SET used=1 WHERE uid=?',(uid,))
        c.execute('INSERT INTO oauth VALUES(?,?,?,?,0)',(digest(state),uid,digest(sid),time.time()+600))
    return state

def oauth_consume(state,sid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM oauth WHERE hash=?',(digest(state),)).fetchone()
        if not row or row['used'] or row['expires']<time.time() or row['session_hash']!=digest(sid) or session(sid)!=row['uid']:
            raise ValueError('授权会话已过期或不匹配，请从原浏览器重新连接。')
        c.execute('UPDATE oauth SET used=1 WHERE hash=?',(digest(state),))
        return row['uid']

def limited(key, maximum, seconds):
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT window,count FROM limits WHERE key=?',(key,)).fetchone()
        window,count=(r['window'],r['count']) if r and now-r['window']<seconds else (now,0)
        if count>=maximum: raise ValueError('操作太频繁，请稍后再试。')
        c.execute('INSERT OR REPLACE INTO limits VALUES(?,?,?)',(key,window,count+1))

def user_ids():
    with db() as c:return [r['id'] for r in c.execute('SELECT id FROM users')]
