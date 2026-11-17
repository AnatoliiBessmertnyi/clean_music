"""Сравнение с журналом слитого PR #27 на синтетической коллекции."""

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import run_report
from report_events import log_operation
from tag_repository import AudioTags

SECTION_LINES = 2


def test_scale_against_merged_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = subprocess.check_output(['git', 'show', '1639f6d:run_report.py'])
    baseline_path = tmp_path / 'baseline.py'
    baseline_path.write_bytes(source)
    spec = importlib.util.spec_from_file_location('report_baseline_v1', baseline_path)
    assert spec is not None and spec.loader is not None
    baseline = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, baseline)
    spec.loader.exec_module(baseline)

    folder = tmp_path / 'music'
    folder.mkdir()
    for index in range(1000):
        (folder / f'{index:04}.mp3').write_bytes(b'synthetic')

    def exercise(module: ModuleType, name: str) -> tuple[Any, bytes]:
        changed = False

        def snapshot(path: Path, repo: object) -> Any:
            index = int(path.stem.removeprefix('r'))
            tags_changed = changed and 900 <= index < 950
            return module.FileSnapshot(
                str(path),
                'b' * 64 if tags_changed else 'a' * 64,
                AudioTags('new' if tags_changed else 'old', 'title'),
                (1, index),
                [],
            )

        monkeypatch.setattr(module, 'snapshot_file', snapshot)
        with module.RunReport(tmp_path / name, False) as report:
            report.start(folder)
            for index in range(800, 900):
                old = folder / f'{index:04}.mp3'
                new = folder / f'r{index:04}.mp3'
                old.rename(new)
                report.record_rename(old, new)
            changed = True
            for index in range(900, 950):
                path = folder / f'{index:04}.mp3'
                module.logger.info(
                    'Теги записаны: %s; artist=%r; title=%r',
                    path,
                    'new',
                    'title',
                    extra={'duplicate_record': module is run_report},
                )
                if module is run_report:
                    log_operation(
                        'tags',
                        path,
                        'успех',
                        'теги записаны',
                        artist='new',
                        title='title',
                    )
            for index in range(950, 1000):
                path = folder / f'{index:04}.mp3'
                module.logger.info('Конфликт целевого имени: %s', path.name)
                if module is run_report:
                    log_operation('rename', path, 'отказ', 'конфликт имени')
        data = report.path.read_bytes()
        for index in range(800, 900):
            (folder / f'r{index:04}.mp3').rename(folder / f'{index:04}.mp3')
        return report, data

    baseline_report, baseline_data = exercise(baseline, 'baseline_logs')
    report, result = exercise(run_report, 'v3_logs')
    text = result.decode('utf-8')
    header, appendix = text.split('=== ПРИЛОЖЕНИЕ: JSONL И ДИАЛОГ ===\n', 1)
    before_names, names = header.split('=== НЕИЗМЕНЁННЫЕ ===\n', 1)
    assert all(len(line) <= 120 for line in names.splitlines())
    sections = before_names.split('=== ПРОБЛЕМЫ ===\n', 1)
    header_lines = len(sections[0].splitlines())
    problem_text, changed_text = sections[1].split('=== ИЗМЕНЁННЫЕ ФАЙЛЫ ===\n')
    assert len(problem_text.splitlines()) <= 50 * 4
    assert len(changed_text.splitlines()) <= 100 + 50 * 3
    assert len(problem_text.splitlines()) == 50
    for index in range(950, 1000):
        assert sum(f'{index:04}.mp3' in line for line in problem_text.splitlines()) == 1
    changed_lines = changed_text.splitlines()
    for index in range(800, 900):
        assert sum(f'{index:04}.mp3' in line for line in changed_lines) == 1
    for index in range(900, 950):
        start = next(
            i for i, line in enumerate(changed_lines) if f'{index:04}.mp3' in line
        )
        end = next(
            (
                i
                for i in range(start + 1, len(changed_lines))
                if not changed_lines[i].startswith('  ')
            ),
            len(changed_lines),
        )
        assert end - start <= 3
    assert header_lines <= 40
    assert SECTION_LINES <= 12
    assert len(before_names.splitlines()) <= 40 + 100 + 50 * 3 + 50 * 4 + SECTION_LINES
    records = [
        json.loads(line) for line in appendix.splitlines() if line.startswith('{')
    ]
    files = [item for item in records if item['type'] == 'ФАЙЛ']
    operations = [item for item in records if item['type'] == 'ОПЕРАЦИЯ']
    assert len(operations) == 200
    expected = [
        {
            'type': 'ФАЙЛ',
            'status': item.status,
            'before': run_report._snapshot_data(item.before),
            'after': run_report._snapshot_data(item.after),
        }
        for item in report.changes
    ]
    assert files == json.loads(json.dumps(expected))
    assert len(files) == 1000
    assert [
        {k: v for k, v in op.items() if k != 'type'} for op in operations
    ] == json.loads(json.dumps([asdict(op) for op in report.operations]))
    assert len(result) <= len(baseline_data)
    assert baseline_report.counts == report.counts
    sys.stdout.write(
        f'v1: {len(baseline_data.splitlines())} строк, {len(baseline_data)} байт; v3: {len(result.splitlines())} строк, {len(result)} байт; верх без имён: {len(before_names.splitlines())} строк\n'
    )
