"""Local Gradio interface. The official CFST binary does all measurement."""
from __future__ import annotations

import argparse
import atexit
import html
import os
import signal
import threading
import time
from pathlib import Path

os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'

import gradio as gr
from fastapi import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from results import rank_results, recommended_ip
from runner import Options, Runner


ROOT = Path(__file__).resolve().parent
HEADERS = ['IP 地址', '延迟 ms', '下载 MB/s', '连接失败率', 'CDN 机房', '验证状态']
COLUMN_WIDTHS = ['20%', '12%', '13%', '13%', '23%', '19%']
LABELS = {'running': '测速中', 'stopping': '正在停止', 'completed': '已完成',
          'cancelled': '已停止', 'failed': '失败', 'no_result': '无下载结果', '历史结果': '终端版'}


class LogDownloads(BaseHTTPMiddleware):
    """Text logs must download rather than open inline in embedded browsers."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (path.startswith('/gradio_api/file=') and path.endswith('/run.log')
                and response.status_code in (200, 206)):
            response.headers['Content-Disposition'] = 'attachment; filename="run.log"'
        return response


def progress_html(state: dict) -> str:
    running = state['running']
    done, total = state['current'], state['total']
    ratio = min(done / total, 1) if total else 0
    percent = (60 + ratio * 40) if state['phase'] == '下载测速' else ratio * 60
    if state['status'] in ('completed', 'no_result'):
        percent = 100
    title = html.escape(state['phase'])
    if state['status'] == 'idle':
        detail = '点击开始，用这台电脑的网络测试候选 IP。'
    elif running:
        count = f'{done} / {total}' if total else '正在准备'
        detail = f'{count} · 已用时 {state["elapsed"]:.0f} 秒'
    else:
        detail = f'用时 {state["elapsed"]:.1f} 秒'
        if state['status'] == 'cancelled':
            detail += ' · 本轮未生成推荐，历史结果仍然保留。'
        elif state['status'] == 'no_result':
            detail += ' · 请检查网络或稍后重试，本轮不推荐 IP。'
        elif state['error']:
            detail += ' · ' + html.escape(state['error'])
    return f'''<section class="run-status" aria-live="polite">
      <div class="status-line"><strong>{title}</strong><span>{detail}</span></div>
      <progress max="100" value="{percent:.0f}" aria-label="测速进度"></progress>
    </section>'''


def stats_html(rows) -> str:
    verified = [r for r in rows if r.verified]
    fastest = max((r.speed for r in verified), default=0)
    latency = min((r.latency for r in verified), default=0)
    return f'''<div class="result-stats">
      <span>下载已验证 <b>{len(verified)}</b> 个 IP</span>
      <span>最高下载 <b>{fastest:.2f}</b> MB/s</span>
      <span>最低已验证延迟 <b>{latency:.2f}</b> ms</span></div>'''


def build_app(runner: Runner) -> gr.Blocks:
    def history_choices():
        return [(f'{e["name"][:15]} · {LABELS.get(e["status"], e["status"])} · '
                 f'{"本地网页" if e["source"] == "local" else "原终端版"}', e['id'])
                for e in runner.history.list()]

    def refresh(mode, previous):
        state = runner.snapshot()
        key = f'{state["run_id"]}:{state["status"]}:{mode}'
        changed = key != previous
        rows = rank_results(state['rows'], mode)
        directory = Path(state['directory']) if state['directory'] else None
        csv_file = directory / 'result.csv' if directory else None
        log_file = directory / 'run.log' if directory else None
        settled = not state['running'] and directory is not None
        return (progress_html(state),
                state['best_ip'] if changed else gr.skip(),
                stats_html(rows) if changed else gr.skip(),
                [r.table_row() for r in rows] if changed else gr.skip(),
                str(csv_file) if settled and csv_file.is_file() and changed else (None if changed else gr.skip()),
                str(log_file) if settled and log_file.is_file() and changed else (None if changed else gr.skip()),
                (state['log'] or '开始后会在这里显示本轮运行记录。') if changed else gr.skip(),
                gr.update(interactive=not state['running']),
                gr.update(interactive=state['running'] and state['status'] != 'stopping'),
                key, gr.update(choices=history_choices()) if changed else gr.skip())

    def start(concurrency, probes, downloads, seconds, mode, previous):
        try:
            runner.start(Options(int(concurrency), int(probes), int(downloads), int(seconds)))
        except (ValueError, RuntimeError, TypeError) as exc:
            gr.Warning(str(exc))
        return refresh(mode, previous)

    def stop(mode, previous):
        runner.stop()
        return refresh(mode, previous)

    def view_history(key, mode):
        if not key:
            return '选择一轮历史测速查看结果。', '', [], None, None
        try:
            entry, rows = runner.history.load(key)
            directory = entry['directory']
            best = recommended_ip(rows) if entry['status'] in ('completed', '历史结果') else ''
            detail = f'**{entry["name"]}** · {LABELS.get(entry["status"], entry["status"])}'
            if entry['status'] not in ('completed', '历史结果'):
                detail += '。本轮未完成，不提供推荐。'
            csv_file, log_file = directory / 'result.csv', directory / 'run.log'
            return (detail, best, [r.table_row() for r in rank_results(rows, mode)],
                    str(csv_file) if csv_file.is_file() else None,
                    str(log_file) if log_file.is_file() and not log_file.is_symlink() else None)
        except (ValueError, OSError) as exc:
            raise gr.Error(f'无法读取历史记录：{exc}') from exc

    with gr.Blocks(title='CDN 优选测速', analytics_enabled=False, fill_width=True) as demo:
        gr.HTML('''<header class="page-heading"><div><p class="eyebrow">LOCAL NETWORK TOOL</p>
          <h1>CDN 优选测速</h1><p>用这台电脑的网络，找出当前表现更好的 Cloudflare 入口。</p></div>
          <span class="local-badge">● 仅本机运行</span></header>''')
        gr.Markdown('测速前请退出 **系统代理、TUN 和 VPN**。下载测速会消耗流量；结果仅代表本次网络环境。',
                    elem_id='network-note')
        with gr.Tabs():
            with gr.Tab('本次测速'):
                with gr.Row(elem_id='action-row'):
                    start_button = gr.Button('开始测速', variant='primary', scale=2, min_width=160)
                    stop_button = gr.Button('停止测速', variant='stop', interactive=False, scale=1, min_width=120)
                status = gr.HTML(progress_html(runner.snapshot()))
                with gr.Accordion('测速设置', open=False):
                    with gr.Row():
                        concurrency = gr.Number(100, label='连接并发', minimum=1, maximum=200, precision=0, min_width=140)
                        probes = gr.Number(3, label='每个 IP 连接测试次数', minimum=1, maximum=20, precision=0, min_width=140)
                        downloads = gr.Number(5, label='下载测试候选数', minimum=1, maximum=20, precision=0, min_width=140)
                        seconds = gr.Number(5, label='每个候选下载时长（秒）', minimum=3, maximum=30, precision=0, min_width=140)
                    gr.Markdown('默认沿用原工具参数。先筛连接延迟，再逐个验证下载速度；候选网段来自工具目录的 `ip.txt`。')
                gr.Markdown('## 本轮结果')
                with gr.Row():
                    best = gr.Textbox(label='推荐 IP', placeholder='完成下载测速后显示', interactive=False,
                                      buttons=['copy'], scale=3, min_width=240, elem_id='best-ip')
                    mode = gr.Dropdown(['下载优先', '延迟优先'], value='下载优先', label='结果排序',
                                       interactive=True, scale=1, min_width=160)
                stats = gr.HTML(stats_html([]))
                table = gr.Dataframe(headers=HEADERS, value=[], interactive=False, datatype=['str','number','number','str','str','str'],
                                     type='array', label='测速结果', max_height=420, column_widths=COLUMN_WIDTHS,
                                     buttons=['copy'], show_search='none', wrap=False, elem_id='current-results')
                gr.Markdown('速度为 0 的记录未完成下载验证，不参与推荐。表格排序仅调整展示顺序；推荐 IP 始终按下载速度选择。')
                gr.Markdown('机房列为本次测速命中的 CDN 入口地区，不是你的中转服务器位置。', elem_classes='muted-note')
                with gr.Row():
                    csv_download = gr.File(label='本轮 CSV', interactive=False, min_width=180)
                    log_download = gr.File(label='本轮原始日志', interactive=False, min_width=180)
                with gr.Accordion('运行记录', open=False):
                    log_box = gr.Textbox('开始后会在这里显示本轮运行记录。', show_label=False, lines=8,
                                         max_lines=12, interactive=False)
            with gr.Tab('历史记录'):
                gr.Markdown('## 历史测速\n每轮结果自动保存在本机，也可查看原来终端版的历史。')
                with gr.Row():
                    history_select = gr.Dropdown(choices=history_choices(), value=None, label='选择测速记录',
                                                 interactive=True, scale=4, min_width=240)
                    history_refresh = gr.Button('刷新列表', scale=1, min_width=120)
                history_info = gr.Markdown('选择一轮历史测速查看结果。')
                history_best = gr.Textbox(label='该轮推荐 IP', interactive=False, buttons=['copy'])
                history_mode = gr.Radio(['下载优先', '延迟优先'], value='下载优先', label='历史结果排序')
                history_table = gr.Dataframe(headers=HEADERS, value=[], type='array', interactive=False,
                                             datatype=['str','number','number','str','str','str'], max_height=500,
                                             column_widths=COLUMN_WIDTHS, label='历史测速结果')
                with gr.Row():
                    history_csv = gr.File(label='历史 CSV', interactive=False)
                    history_log = gr.File(label='历史原始日志', interactive=False)
        gr.Markdown('测速核心：XIU2 / CloudflareSpeedTest v2.3.5 · 网页仅在本机开放 · CDN 测速结果仍需在实际节点中验证',
                    elem_id='app-footer')
        previous = gr.State('')
        outputs = [status, best, stats, table, csv_download, log_download, log_box,
                   start_button, stop_button, previous, history_select]
        refresh_inputs = [mode, previous]
        gr.Timer(1).tick(refresh, refresh_inputs, outputs, queue=False, api_name='refresh', api_visibility='private')
        demo.load(refresh, refresh_inputs, outputs, queue=False, api_name='initial', api_visibility='private')
        start_button.click(start, [concurrency, probes, downloads, seconds, mode, previous], outputs,
                           queue=False, api_name='start_test', show_progress='hidden')
        stop_button.click(stop, refresh_inputs, outputs, queue=False, api_name='stop_test', show_progress='hidden')
        mode.change(refresh, refresh_inputs, outputs, queue=False, api_visibility='private')
        history_outputs = [history_info, history_best, history_table, history_csv, history_log]
        history_select.change(view_history, [history_select, history_mode], history_outputs, queue=False,
                              api_name='history_result', show_progress='hidden')
        history_mode.change(view_history, [history_select, history_mode], history_outputs, queue=False,
                            api_visibility='private')
        history_refresh.click(lambda: gr.update(choices=history_choices()), outputs=history_select,
                              queue=False, api_visibility='private')
    return demo


def serve(port: int, instance: str):
    runner = Runner(ROOT)
    shutdown_event = threading.Event()
    atexit.register(runner.stop)
    demo = build_app(runner)
    theme = gr.themes.Soft(primary_hue='teal', secondary_hue='slate', neutral_hue='slate',
                           font=['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
                           font_mono=['SFMono-Regular', 'Consolas', 'monospace'])
    application, _, _ = demo.launch(server_name='127.0.0.1', server_port=port, share=False,
        inbrowser=False, prevent_thread_lock=True, show_error=True, footer_links=[],
        theme=theme, css_paths=ROOT / 'style.css', ssr_mode=False, strict_cors=True,
        app_kwargs={'middleware': [Middleware(LogDownloads)]},
        allowed_paths=[str(path) for path in runner.history.roots.values()],
        blocked_paths=[str(ROOT / '.venv'), str(ROOT / '.git'), str(ROOT / 'bin'), str(ROOT / 'data')])
    application.add_api_route('/local-cdn-health', lambda: {
        'app': 'cdn-speedtest-gradio', 'instance': instance, 'pid': os.getpid(),
        'running': runner.snapshot()['running']}, methods=['GET'])

    def shutdown(payload: dict):
        if payload.get('instance') != instance:
            raise HTTPException(status_code=403, detail='Instance mismatch')
        shutdown_event.set()
        return {'stopping': True}

    application.add_api_route('/local-cdn-shutdown', shutdown, methods=['POST'])
    signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
    try:
        while not shutdown_event.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        if runner.thread:
            runner.thread.join(timeout=4)
        demo.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--instance', default='manual')
    args = parser.parse_args()
    serve(args.port, args.instance)
