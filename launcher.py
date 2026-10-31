"""Double-click bootstrap with a private virtualenv and a single local instance."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import venv
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
INSTANCE = DATA / 'instance.json'
VERSION = '6.31.0'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health(url: str, token: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            return False
        if parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        with HTTP.open(url.rstrip('/') + '/local-cdn-health', timeout=1) as response:
            data = json.load(response)
        return data.get('app') == 'cdn-speedtest-gradio' and data.get('instance') == token
    except (OSError, ValueError, AttributeError):
        return False


def find_instance(file: Path = INSTANCE) -> str | None:
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
        return data['url'] if health(data['url'], data['instance']) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def choose_port(first=7861, last=7890) -> int:
    for port in range(first, min(last, 65535) + 1):
        with socket.socket() as sock:
            try:
                if os.name != 'nt':
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('7861–7890 端口均被占用，请先关闭其他本地服务。')


class LaunchLock:
    def __init__(self, file: Path):
        self.file = file

    def __enter__(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.file.open('a+b')
        self.stream.write(b'0')
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise BlockingIOError('另一个启动进程正在准备环境，请稍等。') from exc
        return self

    def __exit__(self, *_):
        if os.name == 'nt':
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


def ensure_runtime() -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError('请安装 Python 3.11 或更新版本，再启动本工具。')
    directory = ROOT / '.venv'
    executable = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.is_file():
        print('首次启动：正在创建本工具专用的 Python 环境…', flush=True)
        venv.EnvBuilder(with_pip=True).create(directory)
    check = subprocess.run([str(executable), '-c',
        f'import gradio; assert gradio.__version__ == {VERSION!r}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        print('首次启动：正在安装网页依赖，需要联网，之后启动无需重复安装…', flush=True)
        requirements = ROOT / 'requirements.lock.txt'
        if not requirements.is_file():
            requirements = ROOT / 'requirements.txt'
        subprocess.run([str(executable), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
    return executable


def forget_instance(token: str):
    try:
        if json.loads(INSTANCE.read_text(encoding='utf-8')).get('instance') == token:
            INSTANCE.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass


def request_shutdown(url: str, token: str):
    request = urllib.request.Request(url + '/local-cdn-shutdown', method='POST',
        data=json.dumps({'instance': token}).encode(), headers={'Content-Type': 'application/json'})
    with HTTP.open(request, timeout=3):
        pass


def stop_instance():
    with LaunchLock(DATA / 'launch.lock'):
        _stop_instance()


def _stop_instance():
    url = find_instance()
    if not url:
        print('当前没有运行中的网页测速服务。')
        return
    data = json.loads(INSTANCE.read_text(encoding='utf-8'))
    # The HTTP shutdown works on Windows too, allowing the CFST child to exit cleanly.
    if not health(url, data['instance']):
        return
    request_shutdown(url, data['instance'])
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and health(url, data['instance']):
        time.sleep(0.2)
    if health(url, data['instance']):
        raise RuntimeError('服务尚未退出，请查看 data/server.log。')
    forget_instance(data['instance'])
    print('网页测速服务已关闭。')


def reap_failed_start(process, url: str, token: str):
    try:
        if health(url, token):
            request_shutdown(url, token)
            process.wait(timeout=4)
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def launch(open_browser: bool = True) -> str:
    DATA.mkdir(parents=True, exist_ok=True)
    with LaunchLock(DATA / 'launch.lock'):
        url = find_instance()
        if url:
            if open_browser:
                webbrowser.open(url)
            print(f'已打开正在运行的网页：{url}', flush=True)
            return url
        executable = ensure_runtime()
        port, token = choose_port(), uuid.uuid4().hex
        url = f'http://127.0.0.1:{port}'
        env = os.environ.copy()
        env.update(GRADIO_ANALYTICS_ENABLED='False', HF_HUB_DISABLE_TELEMETRY='1',
                   NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        with (DATA / 'server.log').open('ab') as log:
            process = subprocess.Popen([str(executable), str(ROOT / 'app.py'),
                '--port', str(port), '--instance', token], cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                start_new_session=os.name != 'nt',
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                if os.name == 'nt' else 0)
        print('正在打开本机测速网页…', flush=True)
        temporary = DATA / f'instance-{token}.tmp'
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if health(url, token):
                    meta = {'url': url, 'pid': process.pid, 'instance': token}
                    temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
                    temporary.chmod(0o600)
                    temporary.replace(INSTANCE)
                    if open_browser:
                        webbrowser.open(url)
                    print(f'网页已就绪：{url}\n可以关闭启动窗口。关闭浏览器不会终止后台服务。', flush=True)
                    return url
                if process.poll() is not None:
                    raise RuntimeError('网页服务启动失败，请查看工具目录中的 data/server.log。')
                time.sleep(0.2)
            raise RuntimeError('网页启动超时，请查看 data/server.log 后重试。')
        except BaseException:
            reap_failed_start(process, url, token)
            forget_instance(token)
            raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    try:
        if args.stop:
            stop_instance()
        else:
            launch(not args.no_browser)
    except BlockingIOError as exc:
        print(exc)
    except Exception as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        sys.exit(1)
