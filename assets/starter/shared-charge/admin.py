"""Run on server; writes invitation credentials to an owner-only file, never logs them."""
import argparse,os,json
from pathlib import Path
import store
from service import ORIGIN
p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('--output',required=True);a=p.parse_args()
os.umask(0o077);store.init()
out=Path(a.output)
if out.exists():raise SystemExit('Output exists; choose a new filename')
code=store.create_user(a.name)
with out.open('x') as f:f.write('超充提醒邀请试用\n网址：'+ORIGIN+'/charge/app/\n专属邀请码：'+code+'\n请保管邀请码，它同时用于下次登录。每人使用独立邀请码，不要公开转发。\n')
os.chmod(str(out),0o600)
print('Invitation saved to private file')
