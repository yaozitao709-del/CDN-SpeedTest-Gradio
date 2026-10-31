"""Read CFST data without mistaking TCP-only results for a usable download."""
from __future__ import annotations

import csv
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from locations import colo_name


@dataclass(frozen=True)
class Result:
    ip: str
    sent: int
    received: int
    loss: float
    latency: float
    speed: float
    colo: str

    @property
    def verified(self) -> bool:
        return self.speed > 0

    def table_row(self) -> list:
        return [self.ip, round(self.latency, 2), round(self.speed, 2),
                f'{self.loss * 100:.1f}%', colo_name(self.colo),
                '下载已验证' if self.verified else '未完成下载验证']


def read_results(path: Path) -> list[Result]:
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open(encoding='utf-8-sig', errors='replace', newline='') as stream:
            for values in csv.reader(stream):
                try:
                    address = str(ipaddress.IPv4Address(values[0].strip()))
                    sent, received = int(values[1]), int(values[2])
                    loss, latency, speed = map(float, values[3:6])
                    if not (sent > 0 and 0 <= received <= sent and 0 <= loss <= 1):
                        continue
                    if not all(math.isfinite(v) and v >= 0 for v in (loss, latency, speed)):
                        continue
                    colo = values[6].strip() if len(values) > 6 else 'N/A'
                    rows.append(Result(address, sent, received, loss, latency, speed, colo))
                except (ValueError, IndexError):
                    continue
    except (csv.Error, OSError) as exc:
        raise ValueError(f'无法读取结果 CSV：{exc}') from exc
    return rows


def rank_results(rows: list[Result], mode: str = '下载优先') -> list[Result]:
    if mode == '延迟优先':
        return sorted(rows, key=lambda r: (not r.verified, r.latency, -r.speed))
    return sorted(rows, key=lambda r: (not r.verified, -r.speed, r.latency))


def recommended_ip(rows: list[Result]) -> str:
    ranked = rank_results(rows)
    return ranked[0].ip if ranked and ranked[0].verified else ''


class History:
    """Read only explicit run roots; callers cannot pass filesystem paths."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = {k: v.resolve() for k, v in roots.items()}

    def directory(self, key: str) -> tuple[str, Path]:
        source, _, name = key.partition(':')
        if (source not in self.roots or name in ('.', '..') or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', name)):
            raise ValueError('无效的历史记录')
        root = self.roots[source]
        path = root / name
        if path.is_symlink() or path.resolve().parent != root or not path.is_dir():
            raise ValueError('历史记录不存在')
        return source, path

    def _entry(self, key: str) -> dict:
        source, directory = self.directory(key)
        meta = {}
        try:
            meta = json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, ValueError):
            pass
        return {**meta, 'id': key, 'source': source, 'name': directory.name,
                'time': meta.get('started_at', directory.name),
                'status': meta.get('status', '历史结果'),
                'directory': directory}

    def list(self) -> list[dict]:
        entries = []
        for source, root in self.roots.items():
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                if not ((child / 'meta.json').is_file() or (child / 'result.csv').is_file()):
                    continue
                try:
                    entries.append(self._entry(f'{source}:{child.name}'))
                except ValueError:
                    continue
        return sorted(entries, key=lambda e: e['name'], reverse=True)[:100]

    def load(self, key: str) -> tuple[dict, list[Result]]:
        entry = self._entry(key)
        csv_path = entry['directory'] / 'result.csv'
        if csv_path.is_symlink():
            raise ValueError('历史结果文件无效')
        return entry, read_results(csv_path)
