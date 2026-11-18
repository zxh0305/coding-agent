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
        (environment/'.runtime-chromium').unlink(missing_ok=True)   # 新依赖可能换 playwright 版本，内核标记作废
    # playwright 内核（约 150MB）单独一步、失败不阻断启动：装不上时内置浏览器
    # 会给出分型错误提示（见 browser_tools），其余功能照常可用。
    chromium_stamp=environment/'.runtime-chromium'
    if not chromium_stamp.exists():
        probe=subprocess.run([str(python),'-c','import playwright'],cwd=root)
        if probe.returncode==0:
            print('正在安装内置浏览器内核 playwright chromium（约 190MB，首次较慢）…',flush=True)
            # 官方 CDN 在部分网络下会卡在 0%（实测），因此允许用镜像：
            #   PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
            # 优先级：用户显式设置的 > 默认官方。国内网络卡住时设这个环境变量重跑即可。
            env=dict(os.environ)
            if subprocess.run([str(python),'-m','playwright','install','chromium'],cwd=root,env=env).returncode==0:
                chromium_stamp.write_text('ok')
            else:
                print('内置浏览器内核安装失败：浏览器工具将不可用。'
                      '国内网络可设 PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright 后重跑；'
                      '或设 BROWSER_CHROME_PATH 指向本机 Chrome 来降级',flush=True)
    if not args.install_only:subprocess.run([str(python),str(root/'backend/app.py'),str(args.port)],cwd=root,check=True)
if __name__=='__main__':main()
