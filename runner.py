"""One cancellable CFST process, with per-run files and thread-safe UI snapshots."""
from __future__ import annotations

import codecs
import json
import os
import platform
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from results import History, read_results, recommended_ip


SHANGHAI = timezone(timedelta(hours=8))
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PROGRESS = re.compile(r'(\d+)\s*/\s*(\d+)\s*\[[^\]]*\]')


@dataclass(frozen=True)
class Options:
    concurrency: int = 100
    probes: int = 3
    downloads: int = 5
    seconds: int = 5

    def validate(self):
        for name, low, high in [('concurrency', 1, 200), ('probes', 1, 20),
                                ('downloads', 1, 20), ('seconds', 3, 30)]:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'测速参数 {name} 必须在 {low}–{high} 之间')


def core_path(root: Path) -> Path:
    if os.name == 'nt':
        return root / 'bin' / 'cfst.exe'
    if platform.system() == 'Darwin':
        return root / 'bin' / ('cfst-arm64' if platform.machine() == 'arm64' else 'cfst-amd64')
    raise RuntimeError('此工具包提供 Mac 和 Windows 测速核心')


def clean_log(text: str) -> str:
    text = ANSI.sub('', text)
    text = PROGRESS.sub('', text)
    text = re.sub(r'可用:\s*\d*\s*', '', text)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())[-6000:]


class Runner:
    def __init__(self, root: Path, command: list[str] | None = None):
        self.root = root.resolve()
        self.command = command
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.history = History({
            'local': self.root / 'results',
            'mac': self.root.parent / 'CDN-SpeedTest-Mac' / 'results',
            'windows': self.root.parent / 'CDN-SpeedTest-Windows' / 'results',
        })
        self.state = self._empty()

    @staticmethod
    def _empty() -> dict:
        return dict(running=False, status='idle', phase='准备就绪', current=0, total=0,
                    available=0, best_ip='', rows=[], directory='', run_id='', pid=None,
                    exit_code=None, started_at='', elapsed=0, log='', error='')

    def snapshot(self) -> dict:
        with self.lock:
            state = {**self.state, 'rows': list(self.state['rows'])}
            if state['running']:
                state['elapsed'] = round(time.monotonic() - self.started, 1)
            return state

    def start(self, options: Options) -> str:
        with self.lock:
            if self.state['running']:
                raise RuntimeError('已经有一轮测速正在运行，请先停止或等待完成。')
            # A new attempt invalidates the previous on-screen result even if setup fails.
            self.started = time.monotonic()
            self.state = self._empty()
            try:
                options.validate()
                prefix = self.command or [str(core_path(self.root))]
                if not Path(prefix[0]).is_file():
                    raise ValueError('未找到测速核心，请完整解压工具包。')
                if not (self.root / 'ip.txt').is_file():
                    raise ValueError('未找到候选网段 ip.txt。')
                if os.name != 'nt' and not self.command:
                    Path(prefix[0]).chmod(Path(prefix[0]).stat().st_mode | 0o100)
                now = datetime.now(SHANGHAI)
                run_id = now.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
                directory = self.root / 'results' / run_id
                directory.mkdir(parents=True)
                self.cancel = threading.Event()
                self.state.update(running=True, status='running', phase='准备测速',
                    directory=str(directory), run_id=run_id, started_at=now.isoformat(timespec='seconds'))
                self.options = options
                self._persist()
            except (OSError, ValueError, RuntimeError) as exc:
                self.state.update(running=False, status='failed', phase='启动失败', error=str(exc))
                raise ValueError(f'无法开始测速：{exc}') from exc
            self.thread = threading.Thread(target=self._work, args=(prefix, directory, options),
                                           name='cfst-worker', daemon=True)
            self.thread.start()
            return run_id

    def _persist(self):
        state = self.state
        meta = {k: state[k] for k in ('status', 'run_id', 'started_at', 'elapsed',
                                      'exit_code', 'best_ip', 'error')}
        meta['options'] = asdict(self.options)
        directory = Path(state['directory'])
        temporary = directory / 'meta.json.tmp'
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(directory / 'meta.json')

    @staticmethod
    def _terminate(process: subprocess.Popen):
        if process.poll() is not None:
            return
        try:
            if os.name == 'nt':
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass

    def stop(self) -> str:
        with self.lock:
            if not self.state['running']:
                return '当前没有正在运行的测速。'
            self.cancel.set()
            self.state['status'] = 'stopping'
            self.state['phase'] = '正在停止'
            process = self.process
        if process is not None:
            self._terminate(process)
        return '正在停止本轮测速…'

    def _progress(self, text: str):
        plain = ANSI.sub('', text)
        with self.lock:
            if self.cancel.is_set():
                return
            if '开始下载测速' in plain:
                if self.state['phase'] != '下载测速':
                    self.state.update(current=0, total=self.options.downloads)
                self.state['phase'] = '下载测速'
            elif self.state['phase'] != '下载测速' and '开始延迟测速' in plain:
                self.state['phase'] = '连接延迟测试'
            # Scope counts to the download section once the phase switches.
            section = plain.rsplit('开始下载测速', 1)[-1] if '开始下载测速' in plain else plain
            matches = list(PROGRESS.finditer(section))
            if matches:
                self.state['current'], self.state['total'] = map(int, matches[-1].group(1, 2))
            counts = re.findall(r'可用:\s*(\d+)(?!\d|\s*/)', plain)
            if counts:
                self.state['available'] = int(counts[-1])

    def _work(self, prefix: list[str], directory: Path, options: Options):
        process = None
        error, code = '', None
        try:
            args = [*prefix, '-f', str(self.root / 'ip.txt'), '-n', str(options.concurrency),
                    '-t', str(options.probes), '-dn', str(options.downloads), '-dt', str(options.seconds),
                    '-tp', '443', '-tl', '2000', '-tlr', '0', '-p', '10',
                    '-url', 'https://speed.cloudflare.com/__down?bytes=20000000',
                    '-o', str(directory / 'result.csv')]
            env = {k: v for k, v in os.environ.items()
                   if k.lower() not in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}}
            env['NO_COLOR'] = '1'
            with self.lock:
                if not self.cancel.is_set():
                    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, cwd=self.root, env=env, bufsize=0,
                        start_new_session=os.name != 'nt',
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    self.process = process
                    self.state['pid'] = process.pid
            if process is not None:
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                buffer = ''
                with (directory / 'run.log').open('wb') as log:
                    while chunk := process.stdout.read(4096):
                        log.write(chunk)
                        log.flush()
                        buffer = (buffer + decoder.decode(chunk))[-16000:]
                        self._progress(buffer)
                code = process.wait(timeout=5)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if process is not None:
                self._terminate(process)
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self.lock:
                try:
                    rows = read_results(directory / 'result.csv')
                except (ValueError, OSError) as exc:
                    rows = []
                    error = error or f'读取结果失败：{exc}'
                best = recommended_ip(rows)
                if self.cancel.is_set():
                    status, phase, rows, best = 'cancelled', '已停止', [], ''
                elif error or code != 0:
                    status, phase, rows, best = 'failed', '测速失败', [], ''
                    error = error or f'测速核心退出码：{code}'
                elif not best:
                    status, phase = 'no_result', '没有完成下载验证的 IP'
                else:
                    status, phase = 'completed', '测速完成'
                self.state.update(running=False, status=status, phase=phase, rows=rows,
                    best_ip=best, pid=None, exit_code=code, error=error,
                    elapsed=round(time.monotonic() - self.started, 1))
                self.process = None
                log = directory / 'run.log'
                try:
                    if log.is_file():
                        self.state['log'] = clean_log(log.read_text(encoding='utf-8', errors='replace')[-80000:])
                except OSError as exc:
                    self.state['error'] += f'；读取日志失败：{exc}'
                try:
                    self._persist()
                except OSError as exc:
                    self.state['error'] += f'；保存历史失败：{exc}'
