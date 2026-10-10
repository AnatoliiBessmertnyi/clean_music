"""Стратегии очистки имен файлов от недопустимых символов."""

import re
from abc import ABC, abstractmethod

from config import INVALID_FILENAME_CHARS
from interactor import UserInteractor


class SanitizationStrategy(ABC):
    """Базовый класс для стратегий санитизации."""

    @abstractmethod
    def apply(self, raw_name: str, invalid_chars: set[str]) -> str | None:
        """Применяет стратегию и возвращает очищенное имя или None для пропуска."""
        pass


class RemoveCharsStrategy(SanitizationStrategy):
    """Стратегия: просто удалить недопустимые символы."""

    def apply(self, raw_name: str, invalid_chars: set[str]) -> str | None:
        result = raw_name
        for c in invalid_chars:
            result = result.replace(c, '')
        return re.sub(r'\s+', ' ', result).strip()


class ReplaceCharsStrategy(SanitizationStrategy):
    """Стратегия: заменить недопустимые символы на указанный."""

    def __init__(self, replace_with: str):
        self.replace_with = replace_with

    def apply(self, raw_name: str, invalid_chars: set[str]) -> str | None:
        result = raw_name
        for c in invalid_chars:
            result = result.replace(c, self.replace_with)
        return re.sub(r'\s+', ' ', result).strip()


class BilingualTitleStrategy(SanitizationStrategy):
    """Стратегия: оставить первое или второе название при наличии '/'."""

    def __init__(self, keep_first: bool):
        self.keep_first = keep_first

    def apply(self, raw_name: str, invalid_chars: set[str]) -> str | None:
        feat_suffix = ''
        feat_match = re.search(r'\s*\(feat\.\s*[^)]+\)\s*$', raw_name)
        if feat_match:
            feat_suffix = feat_match.group(0)
            name_without_feat = raw_name[: feat_match.start()]
        else:
            name_without_feat = raw_name

        slash_match = re.search(r'^(.*?\s+-\s+)([^/]+)\s*/\s*(.+)$', name_without_feat)
        if slash_match:
            prefix = slash_match.group(1)
            first_title = slash_match.group(2).strip()
            second_title = slash_match.group(3).strip()

            chosen_title = first_title if self.keep_first else second_title
            return f'{prefix}{chosen_title}{feat_suffix}'

        # Если паттерн не найден, просто удаляем '/'
        result = raw_name.replace('/', '')
        return re.sub(r'\s+', ' ', result).strip()


class ManualInputStrategy(SanitizationStrategy):
    """Стратегия: запросить имя вручную у пользователя."""

    def __init__(self, interactor: UserInteractor, filename: str):
        self.interactor = interactor
        self.filename = filename

    def apply(self, raw_name: str, invalid_chars: set[str]) -> str | None:
        self.interactor.display(f'\n  Файл: {self.filename}')
        self.interactor.display(f'  Предлагаемое: {raw_name}')
        custom = self.interactor.prompt(
            '  Введите имя (без расширения, пустой ввод - пропустить): '
        ).strip()

        if not custom:
            return None
        if INVALID_FILENAME_CHARS.search(custom):
            self.interactor.display('  Имя содержит недопустимые символы, пропускаю.')
            return None
        return custom


class StrategyFactory:
    """Фабрика для создания стратегий на основе имени и параметров."""

    @staticmethod
    def create(
        strategy_name: str, params: dict, interactor: UserInteractor, filename: str
    ) -> SanitizationStrategy:
        if strategy_name == 'remove':
            return RemoveCharsStrategy()
        elif strategy_name == 'replace':
            return ReplaceCharsStrategy(params.get('char', '-'))
        elif strategy_name in ('first_title', 'second_title'):
            return BilingualTitleStrategy(keep_first=(strategy_name == 'first_title'))
        elif strategy_name == 'manual':
            return ManualInputStrategy(interactor, filename)
        elif strategy_name == 'skip':
            raise ValueError("Strategy 'skip' should be handled before applying.")
        else:
            raise ValueError(f'Неизвестная стратегия: {strategy_name}')
