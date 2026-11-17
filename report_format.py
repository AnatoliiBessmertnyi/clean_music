"""Компактный человекочитаемый итог сверки."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from report_events import Operation
    from run_report import RunReport


def _q(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_compact(report: RunReport) -> str:
    lines = [
        '=== ОТЧЁТ ЗАПУСКА v3 ===',
        'Статус запуска: завершён; полная сверка: True',
        f'Режим: {"dry-run (ПРЕДЛОЖЕНИЯ отдельно от диска)" if report.dry_run else "реальный"}',
        f'Commit: {report.commit}',
        f'Python/платформа: {report.runtime}',
        f'Обработка: {report.processing_seconds:.3f} с; сверка: {report.verification_seconds:.3f} с',
        f'Папка: {_q(report.target_directory)}',
        '=== КРАТКИЙ ИТОГ: ФАКТ ДИСКА ===',
        *(f'{status}: {count}' for status, count in report.counts.items()),
    ]
    facts: dict[str, list[Operation]] = {}
    for operation in report.operations:
        for original in operation.originals:
            facts.setdefault(original, []).append(operation)
    problems: list[str] = []
    changes: list[str] = []
    unchanged: list[str] = []
    for change in report.changes:
        before, after = change.before, change.after
        ops = facts.get(change.path, [])
        rejected = [
            op
            for op in ops
            if not op.proposed and op.result in ('отказ', 'конфликт', 'блокировка')
        ]
        name = _q(Path(change.path).name)
        delta = []
        if before is not None and after is not None:
            if before.path != after.path:
                delta.append(
                    f'{_q(Path(before.path).name)} → {_q(Path(after.path).name)}'
                )
            for field in ('artist', 'title'):
                old = getattr(before.tags, field, None)
                new = getattr(after.tags, field, None)
                if old != new:
                    delta.append(f'{field}: {_q(old)} → {_q(new)}')
        if rejected or change.status.startswith(('ошибка', 'отсутствует')):
            reason = rejected[-1].reason if rejected else change.status
            written = any(
                op.kind == 'tags' and op.result == 'успех' and not op.proposed
                for op in ops
            )
            suffix = (
                '; теги записаны, rename отклонён'
                if written and any(op.kind == 'rename' for op in rejected)
                else ''
            )
            problems.append(f'{name}: {_q(reason)}{suffix}')
            problems.extend(f'  {item}' for item in delta)
        elif change.status == 'не изменён':
            unchanged.append(name)
        else:
            changes.append(f'{name}: {delta[0] if delta else change.status}')
            changes.extend(f'  {item}' for item in delta[1:])
    lines.extend(
        [
            '=== ПРОБЛЕМЫ ===',
            *problems,
            '=== ИЗМЕНЁННЫЕ ФАЙЛЫ ===',
            *changes,
            '=== НЕИЗМЕНЁННЫЕ ===',
        ]
    )
    line = ''
    for name in unchanged:
        if len(name) > 120:
            if line:
                lines.append(line)
                line = ''
            lines.extend(
                name[index : index + 120] for index in range(0, len(name), 120)
            )
            continue
        candidate = f'{line}, {name}' if line else name
        if line and len(candidate) > 120:
            lines.append(line)
            line = name
        else:
            line = candidate
    if line:
        lines.append(line)
    lines.extend(['=== ПРИЛОЖЕНИЕ: JSONL И ДИАЛОГ ===', ''])
    return '\n'.join(lines)
