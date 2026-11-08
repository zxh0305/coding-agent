"""Portable bootstrap: python start.py [port], no system MySQL client required."""
import argparse,hashlib,os,subprocess,sys,venv
from pathlib import Path

def main():
    if sys.version_info<(3,11):raise SystemExit('需要 Python 3.11 或更新版本')
    parser=argparse.ArgumentParser(description='创建项目独立环境并启动 Coding Agent')
    parser.add_argument('port',nargs='?',default='8000',type=int);parser.add_argument('--install-only',action='store_true')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('端口须在1～65535之间')
    root=Path(__file__).resolve().parent;environment=root/'.venv';python=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.exists():
        print('正在创建项目独立 Python 环境…',flush=True);venv.EnvBuilder(with_pip=True).create(environment)
    digest=hashlib.sha256(b''.join((root/name).read_bytes() for name in ('requirements.txt','requirements-database.txt'))).hexdigest()
    stamp=environment/'.runtime-dependencies'
    if not stamp.exists() or stamp.read_text()!=digest:
        subprocess.run([str(python),'-m','pip','install','-r',str(root/'requirements.txt')],cwd=root,check=True)
        stamp.write_text(digest)
    if not args.install_only:subprocess.run([str(python),str(root/'backend/app.py'),str(args.port)],cwd=root,check=True)
if __name__=='__main__':main()
