"""Один локальный журнал и независимая сверка файлов на каждый запуск CLI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import override
from uuid import uuid4

from mutagen import MutagenError

from config import SUPPORTED_EXTENSIONS
from tag_repository import AudioTags, TagRepository

logger = logging.getLogger('clean_music')


class _ReportHandler(logging.FileHandler):
    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Не продолжать обработку молча при потере журнала."""
        raise OSError('Не удалось записать журнал запуска') from sys.exception()


@dataclass
class FileSnapshot:
    path: str
    sha256: str | None = None
    tags: AudioTags | None = None
    identity: tuple[int, int] | None = None
    errors: list[str] = field(default_factory=list)


def _check_regular_path(path: Path) -> os.stat_result:
    """Не читать содержимое ссылок и reparse points, включая родителей."""
    info = path.lstat()
    for entry in (path, *path.parents):
        entry_info = info if entry == path else entry.lstat()
        if stat.S_ISLNK(entry_info.st_mode) or (
            getattr(entry_info, 'st_file_attributes', 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(f'ссылка или reparse point: {entry}')
    if not stat.S_ISREG(info.st_mode):
        raise OSError('не является обычным файлом')
    return info


def snapshot_file(path: Path, repo: TagRepository) -> FileSnapshot:
    """Читает файл и теги с диска, не используя кэш конвейера."""
    result = FileSnapshot(str(path))
    try:
        before = _check_regular_path(path)
        result.identity = (before.st_dev, before.st_ino)
        with path.open('rb') as stream:
            result.sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        try:
            result.tags = repo.read_tags_uncached(path)
        except (OSError, MutagenError, ValueError) as exc:
            result.errors.append(f'чтение тегов: {exc}')
        after = path.lstat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            result.errors.append('файл изменился во время снятия снимка')
    except OSError as exc:
        result.errors.append(f'чтение файла: {exc}')
    return result


def _snapshot_data(snapshot: FileSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        'path': snapshot.path,
        'sha256': snapshot.sha256,
        'tags': (
            {'artist': snapshot.tags.artist, 'title': snapshot.tags.title}
            if snapshot.tags is not None
            else None
        ),
        'errors': snapshot.errors,
    }


class RunReport:
    """Журнал диалога и проверка диска; не изменяет музыкальные файлы."""

    def __init__(self, directory: Path, dry_run: bool) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')
        self.path = directory / f'run-{stamp}-{uuid4().hex[:8]}.log'
        self.dry_run = dry_run
        self._handler = _ReportHandler(self.path, mode='x', encoding='utf-8')
        self._handler.setFormatter(
            logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        )
        self._previous_level = logger.level
        self._previous_propagate = logger.propagate
        self._directory: Path | None = None
        self._before: dict[str, FileSnapshot] = {}
        self._current_paths: dict[str, str] = {}
        self._initial_complete = False
        self.counts: Counter[str] = Counter()
        self.verification_complete = False

    def __enter__(self) -> RunReport:
        logger.addHandler(self._handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            logger.info('=== ЖУРНАЛ ЗАПУСКА ===')
            logger.info(
                'Режим: %s', 'dry-run (предложения)' if self.dry_run else 'реальный'
            )
            logger.info('Python: %s; платформа: %s', sys.version, sys.platform)
            logger.info('Рабочий каталог: %s', Path.cwd())
            logger.info('Отчёт: %s', self.path)
        except BaseException:
            self._close()
            raise
        return self

    def start(self, directory: Path) -> None:
        self._directory = directory.absolute()
        logger.info('Выбранная папка: %s', self._directory)
        logger.info('Снимаю исходное состояние (SHA-256 и теги с диска).')
        for path in self._audio_entries():
            snapshot = snapshot_file(path, TagRepository())
            self._before[snapshot.path] = snapshot
            self._current_paths[snapshot.path] = snapshot.path
            self._write_json('ДО', _snapshot_data(snapshot))
        self._initial_complete = True

    def _audio_entries(self) -> list[Path]:
        assert self._directory is not None
        # Не следовать ссылке на выбранную папку даже при чтении отчёта.
        for entry in (self._directory, *self._directory.parents):
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, 'st_file_attributes', 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OSError(f'ссылка или reparse point в пути папки: {entry}')
        return sorted(
            (
                path
                for path in self._directory.iterdir()
                if path.suffix.lower() in SUPPORTED_EXTENSIONS
            ),
            key=lambda path: path.name,
        )

    def record_rename(self, old_path: Path, new_path: Path) -> None:
        """Вызывается только после подтверждённого реального rename."""
        old, new = str(old_path.absolute()), str(new_path.absolute())
        for original, current in self._current_paths.items():
            if current == old:
                self._current_paths[original] = new
        self._write_json('ПЕРЕИМЕНОВАНИЕ ВЫПОЛНЕНО', {'old': old, 'new': new})

    def _write_json(self, label: str, data: object) -> None:
        logger.info('%s %s', label, json.dumps(data, ensure_ascii=False))

    def finish(self) -> None:
        logger.info('=== ФАКТИЧЕСКОЕ СОСТОЯНИЕ НА ДИСКЕ ===')
        if self._directory is None:
            logger.info('Папка не выбрана; обработка не начиналась.')
            return
        if not self._initial_complete:
            logger.warning('Исходный снимок неполон; полная сверка невозможна.')
        after: dict[str, FileSnapshot] = {}
        for path in self._audio_entries():
            snapshot = snapshot_file(path, TagRepository())
            after[str(path)] = snapshot
            # Даже повторный Ctrl+C оставит уже проверенную часть в журнале.
            self._write_json('ПОСЛЕ', _snapshot_data(snapshot))
        counts: Counter[str] = Counter()
        verification_complete = self._initial_complete
        matched: set[str] = set()
        for original, before in self._before.items():
            current = self._current_paths[original]
            final = after.get(current)
            if final is not None:
                matched.add(current)
            if final is None:
                status = 'отсутствует после запуска (удаление не подтверждено)'
            elif before.errors or final.errors:
                status = 'ошибка проверки'
                verification_complete = False
            elif before.identity != final.identity:
                status = 'ошибка проверки: файловый объект заменён'
                verification_complete = False
            else:
                renamed = original != current
                changed = before.sha256 != final.sha256 or before.tags != final.tags
                if renamed and changed:
                    status = 'переименован и изменено содержимое/теги'
                elif renamed:
                    status = 'переименован'
                elif changed:
                    status = 'изменено содержимое/теги'
                else:
                    status = 'не изменён'
            counts[status] += 1
            self._write_json(
                'ФАЙЛ',
                {
                    'status': status,
                    'before': _snapshot_data(before),
                    'after': _snapshot_data(final),
                },
            )
        for after_path, snapshot in after.items():
            if after_path not in matched:
                status = (
                    'появился' if self._initial_complete else 'нет исходного снимка'
                )
                counts[status] += 1
                self._write_json(
                    'ФАЙЛ', {'status': status, 'after': _snapshot_data(snapshot)}
                )
                if snapshot.errors:
                    verification_complete = False
        self.counts = counts
        self.verification_complete = verification_complete
        self._write_json('ИТОГО ПО ФАЙЛАМ', dict(counts))
        logger.info('Аудиозаписей до: %s; после: %s', len(self._before), len(after))
        logger.info(
            'Изменение SHA-256 всего файла не доказывает повреждение или сохранность аудиопотока.'
        )
        logger.info(
            'Сверка не устанавливает правильность названий и тегов и не защищает от конкурентных изменений.'
        )
        logger.info(
            'Удаления приложением не зарегистрированы; отсутствие файла не устанавливает причину исчезновения.'
        )
        summary = '; '.join(f'{status}: {count}' for status, count in counts.items())
        completeness = 'полная' if self.verification_complete else 'неполная'
        sys.stdout.write(
            f'Сверка файлов ({completeness}): {summary or "аудиофайлов нет"}\n'
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc is not None:
                logger.error(
                    'Запуск прерван: %s',
                    exc_type.__name__ if exc_type else '',
                    exc_info=(type(exc), exc, traceback),
                )
            else:
                logger.info('Конвейер завершён; выполняю сверку.')
            try:
                self.finish()
            except (Exception, KeyboardInterrupt) as final_error:
                self.verification_complete = False
                logger.exception('Итоговая сверка неполна: %s', final_error)
                sys.stderr.write(f'Итоговая сверка неполна; см. {self.path}\n')
            logger.info(
                '=== КОНЕЦ ОТЧЁТА; статус запуска: %s; полная сверка: %s ===',
                'завершён' if exc is None else 'прерван',
                self.verification_complete,
            )
            sys.stdout.write(f'Полный отчёт: {self.path}\n')
        finally:
            self._close()

    def _close(self) -> None:
        logger.removeHandler(self._handler)
        logger.setLevel(self._previous_level)
        logger.propagate = self._previous_propagate
        self._handler.close()
