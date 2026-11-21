"""Контекст сессии обработки аудиофайлов."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from file_state import FileRegistry
from report_events import log_operation

if TYPE_CHECKING:
    from recovery_store import RecoveryStore
    from run_report import RunReport

logger = logging.getLogger('clean_music')


@dataclass
class SessionContext:
    """Контекст сессии обработки аудиофайлов.

    Хранит состояние сессии, статистику и пользовательские выборы.
    Передается во все функции для явного управления состоянием.

    Attributes:
        dry_run: Если True, изменения не применяются (сухой прогон)
        debug: Если True, выводится отладочная информация
        remembered_choices: Запомненные выборы пользователя для санитизации
        ask_counts: Счетчики запросов для подсказок
        stats: Статистика обработки по категориям
        blocked_paths: Абсолютные пути и причины запрета изменений до конца сессии
    """

    dry_run: bool
    debug: bool
    target_dir: Path | None = None
    remembered_choices: dict[frozenset[str], int] = field(default_factory=dict)
    ask_counts: dict[frozenset[str], int] = field(default_factory=dict)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            'junk_removed': 0,
            'tags_cleaned': 0,
            'dash_normalized': 0,
            'spaces_normalized': 0,
            'duplicates_removed': 0,
            'renamed_by_tags': 0,
            'tags_written': 0,
            'artists_normalized': 0,
            'artists_normalized_unique': 0,
            'manual_input': 0,
        }
    )
    planned_stats: dict[str, int] = field(default_factory=dict)
    missing_files: list[Path] = field(default_factory=list)
    blocked_paths: dict[Path, str] = field(default_factory=dict)
    report: RunReport | None = None
    registry: FileRegistry = field(default_factory=FileRegistry)
    recovery: RecoveryStore | None = None
    ffprobe_missing_reported: bool = False

    def file_ids(self, paths: Iterable[Path]) -> list[int]:
        """Фиксирует порядок входных файлов и устраняет повторы по ID."""
        return list(dict.fromkeys(self.registry.track(path).id for path in paths))

    def present_path(self, file_id: int) -> Path | None:
        record = self.registry.record_of(file_id)
        if not record.present:
            return None
        try:
            self.registry.physical_path(record).lstat()
        except FileNotFoundError:
            self.registry.mark_missing(file_id)
            return None
        except OSError:
            # Недоступность пути не подтверждает исчезновение.
            pass
        return record.path

    def present_paths(self, file_ids: Iterable[int]) -> list[Path]:
        return [
            path
            for file_id in file_ids
            if (path := self.present_path(file_id)) is not None
        ]

    def block_path(self, path: Path, reason: str) -> None:
        """Запрещает дальнейшие изменения по пути, сохраняя первую причину.

        absolute() не разрешает ссылки. normcase() нормализует регистр
        только в Windows; на POSIX сохраняет исходное написание.
        """
        self.blocked_paths.setdefault(Path(os.path.normcase(path.absolute())), reason)
        logger.info(
            'Путь заблокирован: %s; причина: %s',
            path,
            reason,
            extra={
                'duplicate_record': path.is_absolute()
                and self.get_block_reason(path) == reason
            },
        )
        log_operation(
            'block',
            path,
            'блокировка',
            self.get_block_reason(path) or reason,
            proposed=self.dry_run,
        )

    def get_block_reason(self, path: Path) -> str | None:
        """Возвращает причину блокировки или None, если путь не заблокирован."""
        return self.blocked_paths.get(Path(os.path.normcase(path.absolute())))

    def is_path_blocked(self, path: Path) -> bool:
        """Проверяет запрет изменений по пути в текущей сессии."""
        return Path(os.path.normcase(path.absolute())) in self.blocked_paths

    def increment_stat(self, key: str, amount: int = 1) -> None:
        """Увеличивает счетчик статистики.

        Args:
            key: Ключ статистики
            amount: На сколько увеличить (по умолчанию 1)
        """
        counts = self.planned_stats if self.dry_run else self.stats
        counts[key] = counts.get(key, 0) + amount

    def get_stat(self, key: str) -> int:
        """Возвращает значение счетчика статистики.

        Args:
            key: Ключ статистики

        Returns:
            Значение счетчика или 0, если ключ не найден
        """
        return self.stats.get(key, 0)

    def log_debug(self, message: str) -> None:
        """Выводит отладочное сообщение, если включен debug режим.

        Args:
            message: Сообщение для вывода
        """
        if self.debug:
            logger.debug(message)
