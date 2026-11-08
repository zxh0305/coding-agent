"""Check a clean code copy starts with the project-installed Python environment."""
import shutil,subprocess,tempfile,socket,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[2];python=root/'.venv'/('Scripts/python.exe' if __import__('os').name=='nt' else 'bin/python')
with tempfile.TemporaryDirectory(prefix='agent-clean-clone-') as directory:
    clone=Path(directory)
    for name in ('backend','frontend'):
        shutil.copytree(root/name,clone/name,ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run([str(python),'-I','-c','import pymysql,cryptography; print("project-installed MySQL driver available")'],check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    with (clone/'startup.log').open('wb') as log:
        process=subprocess.Popen([str(python),str(clone/'backend/app.py'),str(port)],cwd=clone,stdout=log,stderr=log)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=1) as reply:assert reply.status==200
                break
            except Exception:time.sleep(.1)
        else:raise AssertionError('clean runtime failed to start')
        assert (clone/'data/agent_data.db').is_file()
        print('PASS: clean code copy, no original config/data, isolated project driver and HTTP startup')
    finally:
        process.terminate();process.wait(10)
