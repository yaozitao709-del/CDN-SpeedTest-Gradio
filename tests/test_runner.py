import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from runner import Options, Runner


STUB = r'''
import pathlib,sys,time
root=pathlib.Path(__file__).parent
mode=(root/'mode.txt').read_text()
output=pathlib.Path(sys.argv[sys.argv.index('-o')+1])
print('开始延迟测速（模式：TCP）',flush=True)
if mode=='header':
    print('256 / 256 [-----] 可用: 15',flush=True)
    print('开始下载测速',flush=True)
    time.sleep(30)
print('256 / 256 [-----] 可用: 15 开始下载测速',flush=True)
print('1 / 5 [-----]',flush=True)
if mode=='hang':
    time.sleep(30)
if mode=='fail':
    print('test failure',flush=True)
    sys.exit(7)
if mode=='broken':
    output.write_text('x'*140000)
    sys.exit(0)
speed='0' if mode=='zero' else '8.5'
output.write_text('IP 地址,已发送,已接收,丢包率,平均延迟,下载速度(MB/s),地区码\n'.replace('\\n','\n')+
    ('104.16.0.1,3,3,0,50,'+speed+',HKG\n').replace('\\n','\n'))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'ip.txt').write_text('104.16.0.1\n')
        (self.root / 'stub.py').write_text(STUB)
        self.mode('ok')
        self.runner = Runner(self.root, command=[sys.executable, str(self.root / 'stub.py')])

    def tearDown(self):
        self.runner.stop()
        self.wait_done()
        self.tmp.cleanup()

    def mode(self, value):
        (self.root / 'mode.txt').write_text(value)

    def wait_done(self):
        deadline = time.monotonic() + 5
        while self.runner.snapshot()['running'] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.runner.snapshot()['running'])
        return self.runner.snapshot()

    def test_completion_preserves_csv_log_and_metadata(self):
        self.runner.start(Options())
        state = self.wait_done()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['best_ip'], '104.16.0.1')
        directory = Path(state['directory'])
        self.assertTrue((directory / 'run.log').is_file())
        self.assertEqual(json.loads((directory / 'meta.json').read_text())['status'], 'completed')
        self.assertEqual(len(self.runner.history.list()), 1)

    def test_failure_cannot_reuse_a_previous_recommendation(self):
        self.runner.start(Options())
        self.wait_done()
        self.mode('fail')
        self.runner.start(Options())
        state = self.wait_done()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['best_ip'], '')
        self.assertEqual(state['exit_code'], 7)
        self.assertEqual(len(self.runner.history.list()), 2)

    def test_no_download_is_not_marked_success(self):
        self.mode('zero')
        self.runner.start(Options())
        state = self.wait_done()
        self.assertEqual(state['status'], 'no_result')
        self.assertEqual(state['best_ip'], '')

    def test_cancel_terminates_child_and_allows_restart(self):
        self.mode('hang')
        self.runner.start(Options())
        deadline = time.monotonic() + 3
        while not self.runner.snapshot()['pid'] and time.monotonic() < deadline:
            time.sleep(0.01)
        pid = self.runner.snapshot()['pid']
        self.assertTrue(pid)
        self.runner.stop()
        state = self.wait_done()
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['best_ip'], '')
        if os.name != 'nt':
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.mode('ok')
        self.runner.start(Options())
        self.assertEqual(self.wait_done()['status'], 'completed')

    def test_second_start_is_rejected_and_progress_is_readable(self):
        self.mode('hang')
        self.runner.start(Options())
        with self.assertRaises(RuntimeError):
            self.runner.start(Options())
        deadline = time.monotonic() + 3
        while self.runner.snapshot()['phase'] != '下载测速' and time.monotonic() < deadline:
            time.sleep(0.01)
        state = self.runner.snapshot()
        self.assertEqual(state['phase'], '下载测速')
        self.assertEqual((state['current'], state['total']), (1, 5))

    def test_cancel_before_process_spawn_is_safe(self):
        self.mode('hang')
        self.runner.start(Options())
        self.runner.stop()
        self.assertEqual(self.wait_done()['status'], 'cancelled')

    def test_option_limits_reject_bad_values_before_start(self):
        for options in [Options(downloads=0), Options(concurrency=10000), Options(seconds=-1)]:
            with self.assertRaises(ValueError):
                self.runner.start(options)
        self.assertFalse(self.runner.snapshot()['running'])

    def test_download_phase_resets_previous_tcp_counter(self):
        self.mode('header')
        self.runner.start(Options())
        deadline = time.monotonic() + 3
        while self.runner.snapshot()['phase'] != '下载测速' and time.monotonic() < deadline:
            time.sleep(0.01)
        state = self.runner.snapshot()
        self.assertEqual((state['current'], state['total']), (0, 5))

    def test_metadata_write_failure_does_not_leave_a_running_task(self):
        with patch('pathlib.Path.replace', side_effect=OSError('disk full')):
            with self.assertRaises(ValueError):
                self.runner.start(Options())
        self.assertFalse(self.runner.snapshot()['running'])
        self.runner.start(Options())
        self.assertEqual(self.wait_done()['status'], 'completed')

    def test_corrupt_csv_finishes_as_failed_and_allows_restart(self):
        self.mode('broken')
        self.runner.start(Options())
        state = self.wait_done()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['best_ip'], '')
        self.mode('ok')
        self.runner.start(Options())
        self.assertEqual(self.wait_done()['status'], 'completed')

    def test_preflight_failure_clears_previous_result(self):
        self.runner.start(Options())
        self.wait_done()
        (self.root / 'ip.txt').unlink()
        with self.assertRaises(ValueError):
            self.runner.start(Options())
        state = self.runner.snapshot()
        self.assertEqual(state['best_ip'], '')
        self.assertEqual(state['rows'], [])
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(len(self.runner.history.list()), 1)

    def test_result_read_error_cannot_strand_worker(self):
        with patch('runner.read_results', side_effect=OSError('cannot read CSV')):
            self.runner.start(Options())
            state = self.wait_done()
        self.assertEqual(state['status'], 'failed')
        self.assertIn('CSV', state['error'])


if __name__ == '__main__':
    unittest.main()
