"""Один локальный журнал и независимая сверка файлов на каждый запуск CLI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import override
from uuid import uuid4

from config import SUPPORTED_EXTENSIONS
from file_state import FileIdentity, check_directory, regular_file_info
from report_events import Operation, log_operation
from report_format import render_compact
from tag_repository import AudioTags, TagRepository

logger = logging.getLogger('clean_music')


class _ReportHandler(logging.FileHandler):
    observe: Callable[[Operation], None] | None = None

    structured_offsets: set[int]

    @override
    def emit(self, record: logging.LogRecord) -> None:
        offset = self.stream.tell() if self.stream is not None else None
        super().emit(record)
        operation = getattr(record, 'operation', None)
        if offset is not None and (
            isinstance(operation, Operation)
            or getattr(record, 'snapshot_record', False)
            or getattr(record, 'duplicate_record', False)
        ):
            self.structured_offsets.add(offset)
        if isinstance(operation, Operation) and self.observe is not None:
            self.observe(operation)

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


@dataclass
class FileChange:
    status: str
    before: FileSnapshot | None = None
    after: FileSnapshot | None = None

    @property
    def path(self) -> str:
        snapshot = self.before or self.after
        assert snapshot is not None
        return snapshot.path


def _git_commit() -> str:
    """Git — необязательный источник метаданных; без сетевых операций."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            encoding='ascii',
            errors='replace',
            timeout=2,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        value = result.stdout.strip()
        if len(value) in (40, 64) and all(char in '0123456789abcdef' for char in value):
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    return 'unknown'


def snapshot_file(path: Path, repo: TagRepository) -> FileSnapshot:
    """Читает файл и теги с диска, не используя кэш конвейера."""
    result = FileSnapshot(str(path))
    try:
        before = regular_file_info(path)
        result.identity = (before.st_dev, before.st_ino)
        with path.open('rb') as stream:
            result.sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        metadata = repo.read_tags_uncached(path)
        result.tags = metadata.tags
        if metadata.error is not None:
            result.errors.append(f'чтение тегов: {metadata.error}')
        after = regular_file_info(path)
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
        'identity': snapshot.identity,
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
        self._handler.structured_offsets = set()
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
        self.changes: list[FileChange] = []
        self.operations: list[Operation] = []
        self.stage = ''
        self.commit = 'unknown'
        self.runtime = f'{sys.version}; {sys.platform}'
        self.processing_seconds = 0.0
        self.verification_seconds = 0.0
        self._started = time.perf_counter()
        self._proposed_paths: dict[str, tuple[str, ...]] = {}
        self._published_identities: dict[str, FileIdentity] = {}
        self._handler.observe = self._observe

    @property
    def target_directory(self) -> str | None:
        return str(self._directory) if self._directory is not None else None

    def __enter__(self) -> RunReport:
        logger.addHandler(self._handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            logger.info('=== ЖУРНАЛ ЗАПУСКА ===')
            self.commit = _git_commit()
            logger.info('Commit: %s', self.commit)
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
            if self.dry_run:
                self._proposed_paths[os.path.normcase(snapshot.path)] = (snapshot.path,)
            self._write_json('ДО', _snapshot_data(snapshot))
        self._initial_complete = True

    def _audio_entries(self) -> list[Path]:
        assert self._directory is not None
        # Не следовать ссылке на выбранную папку даже при чтении отчёта.
        check_directory(self._directory)
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
        log_operation(
            'rename',
            old_path,
            'успех',
            'выбранное конвейером имя; Windows rename подтверждён',
            target=new_path,
        )
        old, new = str(old_path.absolute()), str(new_path.absolute())
        for original, current in self._current_paths.items():
            if current == old:
                self._current_paths[original] = new
        self._write_json('ПЕРЕИМЕНОВАНИЕ ВЫПОЛНЕНО', {'old': old, 'new': new})

    def record_tag_publication(self, path: Path, identity: FileIdentity) -> None:
        current = path.absolute()
        for original, name in self._current_paths.items():
            if Path(name) == current:
                self._published_identities[original] = identity
                break

    def _observe(self, operation: Operation) -> None:
        """Связывает наблюдение с исходным файлом, не управляя обработкой."""
        path = os.path.normcase(operation.path)
        if self.dry_run:
            originals = self._proposed_paths.get(path, ())
        else:
            originals = tuple(
                original
                for original, current in self._current_paths.items()
                if os.path.normcase(current) == path
            )
            if not originals and operation.identity is not None:
                originals = tuple(
                    original
                    for original, snapshot in self._before.items()
                    if snapshot.identity == operation.identity
                )
        self.operations.append(
            replace(operation, originals=originals, stage=self.stage)
        )
        if (
            operation.kind == 'rename'
            and operation.result == 'ПРЕДЛОЖЕНИЕ'
            and operation.target is not None
        ):
            self._proposed_paths.pop(path, None)
            self._proposed_paths[os.path.normcase(operation.target)] = originals
        elif operation.kind == 'duplicate_delete' and operation.result == 'ПРЕДЛОЖЕНИЕ':
            self._proposed_paths.pop(path, None)

    def _write_json(self, label: str, data: object) -> None:
        logger.info(
            '%s %s',
            label,
            json.dumps(data, ensure_ascii=False),
            extra={
                'snapshot_record': label in ('ДО', 'ПОСЛЕ', 'ФАЙЛ'),
                'duplicate_record': label == 'ПЕРЕИМЕНОВАНИЕ ВЫПОЛНЕНО',
            },
        )

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
        self.changes = []
        verification_complete = self._initial_complete
        matched: set[str] = set()
        deleted = {
            original
            for operation in self.operations
            if operation.kind == 'duplicate_delete'
            and operation.result == 'успех'
            and not operation.proposed
            for original in operation.originals
        }
        uncertain = {
            original
            for operation in self.operations
            if operation.kind == 'duplicate_delete'
            and operation.result == 'неопределённость'
            for original in operation.originals
        }
        for original, before in self._before.items():
            current = self._current_paths[original]
            final = None if original in deleted else after.get(current)
            if final is not None:
                matched.add(current)
            if original in deleted:
                status = 'удалён как точная копия'
            elif original in uncertain:
                status = 'результат удаления неопределён'
                verification_complete = False
            elif final is None:
                status = 'отсутствует после запуска (удаление не подтверждено)'
            elif before.errors or final.errors:
                status = 'ошибка проверки'
                verification_complete = False
            elif (
                self._published_identities.get(original, before.identity)
                != final.identity
            ):
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
            self.changes.append(FileChange(status, before, final))
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
                self.changes.append(FileChange(status, after=snapshot))
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
        compact = False
        try:
            if exc is not None:
                logger.error(
                    'Запуск прерван: %s',
                    exc_type.__name__ if exc_type else '',
                    exc_info=(type(exc), exc, traceback),
                )
            else:
                logger.info('Конвейер завершён; выполняю сверку.')
            self.processing_seconds = time.perf_counter() - self._started
            verification_started = time.perf_counter()
            try:
                self.finish()
            except (Exception, KeyboardInterrupt) as final_error:
                self.verification_complete = False
                logger.exception('Итоговая сверка неполна: %s', final_error)
                sys.stderr.write(f'Итоговая сверка неполна; см. {self.path}\n')
            finally:
                self.verification_seconds = time.perf_counter() - verification_started
            logger.info(
                'Длительность обработки: %.3f с; сверки: %.3f с',
                self.processing_seconds,
                self.verification_seconds,
            )
            logger.info(
                '=== КОНЕЦ ОТЧЁТА; статус запуска: %s; полная сверка: %s ===',
                'завершён' if exc is None else 'прерван',
                self.verification_complete,
            )
            compact = exc is None and self.verification_complete
            if not compact:
                logger.warning(
                    'СЫРОЙ ФОРМАТ: запуск прерван или сверка неполна; компактный отчёт не сформирован.'
                )
            sys.stdout.write(f'Полный отчёт: {self.path}\n')
        finally:
            self._close()
        if compact:
            self._compact()

    def _compact(self) -> None:
        """Заменяет сырой журнал после записи полного плотного приложения."""
        temporary: Path | None = None
        try:
            header = render_compact(self).encode('utf-8')
            with (
                self.path.open('rb') as raw,
                tempfile.NamedTemporaryFile(
                    mode='wb',
                    dir=self.path.parent,
                    prefix=f'.{self.path.stem}-',
                    suffix='.tmp',
                    delete=False,
                ) as output,
            ):
                temporary = Path(output.name)
                output.write(header)
                for change in self.changes:
                    payload = {
                        'type': 'ФАЙЛ',
                        'status': change.status,
                        'before': _snapshot_data(change.before),
                        'after': _snapshot_data(change.after),
                    }
                    output.write(
                        (
                            json.dumps(
                                payload, ensure_ascii=False, separators=(',', ':')
                            )
                            + '\n'
                        ).encode('utf-8')
                    )
                for operation in self.operations:
                    payload = {'type': 'ОПЕРАЦИЯ', **asdict(operation)}
                    if operation.proposed:
                        payload['пометка'] = 'ПРЕДЛОЖЕНИЕ'
                    output.write(
                        (
                            json.dumps(
                                payload, ensure_ascii=False, separators=(',', ':')
                            )
                            + '\n'
                        ).encode('utf-8')
                    )
                while True:
                    offset = raw.tell()
                    line = raw.readline()
                    if not line:
                        break
                    if offset not in self._handler.structured_offsets:
                        output.write(line)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (Exception, KeyboardInterrupt) as error:
            self._compact_warning(
                f'Компактизация не выполнена; сохранён сырой журнал: {type(error).__name__}: {error}'
            )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    self._compact_warning(
                        f'Не удалось удалить временный файл {temporary}: {error}'
                    )

    def _compact_warning(self, message: str) -> None:
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(f'{datetime.now().isoformat()} WARNING {message}\n')
        except OSError:
            pass
        sys.stderr.write(message + '\n')

    def _close(self) -> None:
        logger.removeHandler(self._handler)
        logger.setLevel(self._previous_level)
        logger.propagate = self._previous_propagate
        self._handler.close()
