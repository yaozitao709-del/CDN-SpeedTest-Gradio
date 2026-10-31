import tempfile
import unittest
from pathlib import Path

from results import History, read_results, rank_results, recommended_ip


HEADER = 'IP 地址,已发送,已接收,丢包率,平均延迟,下载速度(MB/s),地区码\n'


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.csv = self.root / 'result.csv'

    def tearDown(self):
        self.tmp.cleanup()

    def test_unverified_fast_tcp_is_never_recommended(self):
        self.csv.write_text(HEADER +
            '104.16.0.1,3,3,0,2,0,N/A\n'
            '172.66.0.1,3,3,0,65,2.1,HKG\n', encoding='utf-8-sig')
        rows = read_results(self.csv)
        self.assertEqual(recommended_ip(rows), '172.66.0.1')
        self.assertEqual(rank_results(rows, '延迟优先')[0].ip, '172.66.0.1')

    def test_invalid_or_nonfinite_rows_do_not_become_results(self):
        self.csv.write_text(HEADER +
            'bad-ip,3,3,0,1,99,HKG\n'
            '104.16.0.1,3,3,0,1,nan,HKG\n'
            '104.16.0.2,3,4,0,1,9,HKG\n'
            '104.16.0.3,3,3,0,-2,9,HKG\n'
            '104.16.0.4,3,3,0,5,1,HKG\n')
        self.assertEqual([r.ip for r in read_results(self.csv)], ['104.16.0.4'])

    def test_sorting_and_no_positive_download(self):
        self.csv.write_text(HEADER +
            '104.16.0.1,3,3,0,50,2,HKG\n'
            '104.16.0.2,3,3,0,100,8,SEA\n')
        rows = read_results(self.csv)
        self.assertEqual(rank_results(rows, '下载优先')[0].speed, 8)
        self.assertEqual(rank_results(rows, '延迟优先')[0].latency, 50)
        self.csv.write_text(HEADER + '104.16.0.1,3,3,0,50,0,N/A\n')
        self.assertEqual(recommended_ip(read_results(self.csv)), '')

    def test_history_loads_original_csv_without_writing_it(self):
        run = self.root / '20261031-123000-100'
        run.mkdir()
        data = HEADER + '104.16.0.1,3,3,0,50,2,HKG\n'
        (run / 'result.csv').write_text(data)
        history = History({'local': self.root})
        entries = history.list()
        self.assertEqual(len(entries), 1)
        entry, rows = history.load(entries[0]['id'])
        self.assertEqual(rows[0].ip, '104.16.0.1')
        self.assertEqual((run / 'result.csv').read_text(), data)
        self.assertEqual(entry['source'], 'local')

    def test_history_rejects_traversal_and_symlink(self):
        history = History({'local': self.root})
        for value in ['local:../secret', 'local:/etc', 'other:run', 'local:..']:
            with self.assertRaises(ValueError):
                history.load(value)
        (self.root / 'link').symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            history.load('local:link')

    def test_corrupt_large_csv_has_a_readable_error(self):
        self.csv.write_text('x' * 140000)
        with self.assertRaisesRegex(ValueError, 'CSV'):
            read_results(self.csv)


if __name__ == '__main__':
    unittest.main()
