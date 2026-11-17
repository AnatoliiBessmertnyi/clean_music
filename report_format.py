"""Компактное представление фактов; полный журнал остаётся приложением."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from report_events import Operation
    from run_report import FileChange, RunReport


def _quoted(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _operation_line(operation: Operation) -> str:
    action = {'tags': 'запись тегов', 'rename': 'rename', 'block': 'блокировка'}[
        operation.kind
    ]
    result = (
        f'ПРЕДЛОЖЕНИЕ ({operation.result})' if operation.proposed else operation.result
    )
    text = f'  - [{operation.stage or "вне этапа"}] {action}: {result}; причина: {operation.reason}'
    if operation.kind == 'rename':
        text += f'; {_quoted(Path(operation.path).name)} → {_quoted(Path(operation.target).name if operation.target else None)}'
    if operation.kind == 'tags':
        text += (
            f'; artist={_quoted(operation.artist)}, title={_quoted(operation.title)}'
        )
    return text


def _delta_lines(change: FileChange) -> list[str]:
    before, after = change.before, change.after
    snapshot = after or before
    assert snapshot is not None
    lines = [f'ФАЙЛ: {_quoted(snapshot.path)}; {change.status}']
    if before is not None and after is not None:
        if before.path != after.path:
            lines.append(f'  путь: {_quoted(before.path)} → {_quoted(after.path)}')
        for field in ('artist', 'title'):
            old = getattr(before.tags, field, None)
            new = getattr(after.tags, field, None)
            if old != new:
                lines.append(f'  {field}: {_quoted(old)} → {_quoted(new)}')
        lines.append(
            '  содержимое (SHA-256): '
            + ('совпадает' if before.sha256 == after.sha256 else 'изменилось')
        )
    for label, snapshot in (('до', before), ('после', after)):
        if snapshot is not None:
            lines.extend(
                f'  ошибка {label}: {_quoted(error)}' for error in snapshot.errors
            )
    return lines


def render_compact(report: RunReport) -> str:
    """Итог сверху, проблемы и дельты; без дублирования неизменённых тегов."""
    statuses = (
        'не изменён',
        'переименован',
        'изменено содержимое/теги',
        'переименован и изменено содержимое/теги',
        'отсутствует после запуска (удаление не подтверждено)',
        'появился',
        'ошибка проверки',
    )
    lines = [
        '=== ОТЧЁТ ЗАПУСКА v2 ===',
        'Статус запуска: завершён; полная сверка: True',
        f'Режим: {"dry-run (ПРЕДЛОЖЕНИЯ отдельно от диска)" if report.dry_run else "реальный"}',
        f'Commit: {report.commit}',
        f'Python/платформа: {report.runtime}',
        f'Обработка (включая ввод и исходный снимок): {report.processing_seconds:.3f} с',
        f'Сверка: {report.verification_seconds:.3f} с',
        f'Папка: {_quoted(report.target_directory)}',
        '',
        '=== КРАТКИЙ ИТОГ: ФАКТ ДИСКА ===',
        *(f'{status}: {report.counts[status]}' for status in statuses),
        *(
            f'{status}: {count}'
            for status, count in report.counts.items()
            if status not in statuses
        ),
        '',
        '=== ПРОБЛЕМЫ ===',
    ]
    facts: dict[str, list[Operation]] = {}
    proposals: list[Operation] = []
    unknown: list[Operation] = []
    for operation in report.operations:
        if operation.proposed:
            proposals.append(operation)
        elif not operation.originals:
            unknown.append(operation)
        else:
            for original in operation.originals:
                facts.setdefault(original, []).append(operation)

    problem_paths: set[str] = set()
    for change in report.changes:
        key = change.path
        operations = facts.get(key, [])
        written = False
        written_then_rejected = False
        for operation in operations:
            if operation.kind == 'tags' and operation.result == 'успех':
                written = True
            if (
                written
                and operation.kind == 'rename'
                and operation.result in ('отказ', 'конфликт', 'блокировка')
            ):
                written_then_rejected = True
        has_problem = any(
            op.result in ('отказ', 'конфликт', 'блокировка') for op in operations
        )
        if change.status.startswith(('ошибка', 'отсутствует')) or has_problem:
            problem_paths.add(key)
            lines.extend(_delta_lines(change))
            if written_then_rejected:
                lines.append(
                    '  ВНИМАНИЕ: теги записаны, rename отклонён; запись тегов не откатывалась.'
                )
            lines.extend(_operation_line(op) for op in operations)
    for operation in unknown:
        lines.append(
            f'Не удалось однозначно сопоставить операцию с исходным файлом: {_quoted(operation.path)}'
        )
        lines.append(_operation_line(operation))
    if not problem_paths and not unknown:
        lines.append('Не выявлены.')

    lines.extend(('', '=== ИЗМЕНЁННЫЕ ФАЙЛЫ: ДЕЛЬТЫ И ОПЕРАЦИИ ==='))
    for change in report.changes:
        key = change.path
        if change.status != 'не изменён' and key not in problem_paths:
            lines.extend(_delta_lines(change))
            lines.extend(_operation_line(op) for op in facts.get(key, []))

    lines.extend(('', '=== НЕИЗМЕНЁННЫЕ: ИМЯ И SHA-256 ==='))
    for change in report.changes:
        if change.status == 'не изменён' and change.before is not None:
            lines.append(
                f'{_quoted(Path(change.before.path).name)} | SHA-256 {change.before.sha256}'
            )

    lines.extend(('', '=== ПРЕДЛОЖЕНИЯ: НЕ ФАКТ ДИСКА ==='))
    for operation in proposals:
        lines.append(f'Файл: {_quoted(operation.originals or (operation.path,))}')
        lines.append(_operation_line(operation))
    if not proposals:
        lines.append('Нет.')
    lines.extend(
        (
            '',
            'SHA-256 всего файла не проверяет аудиопоток отдельно от тегов.',
            'Отсутствие файла не доказывает удаление приложением; правильность текста тегов не проверяется.',
            '',
            '=== ПРИЛОЖЕНИЕ: ПОЛНЫЙ СЫРОЙ ЖУРНАЛ И СНИМКИ ДО/ПОСЛЕ ===',
            '',
        )
    )
    return '\n'.join(lines)
