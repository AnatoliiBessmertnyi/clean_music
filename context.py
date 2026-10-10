"""Контекст сессии обработки аудиофайлов."""

from dataclasses import dataclass, field


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
    """

    dry_run: bool
    debug: bool
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

    def increment_stat(self, key: str, amount: int = 1) -> None:
        """Увеличивает счетчик статистики.

        Args:
            key: Ключ статистики
            amount: На сколько увеличить (по умолчанию 1)
        """
        self.stats[key] = self.stats.get(key, 0) + amount

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
            print(f'  [DEBUG] {message}')
