"""Стратегии очистки имен файлов от недопустимых символов."""

import re
from abc import ABC, abstractmethod

from config import INVALID_FILENAME_CHARS
from interactor import UserInteractor
from naming import split_qualifiers, title_separator


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
        artist, divider, title = raw_name.partition(' - ')
        if divider:
            separator = title_separator(title)
            if separator is not None:
                first, second = (
                    title[:separator].strip(),
                    title[separator + 1 :].strip(),
                )
                if first and second:
                    second_base, groups = split_qualifiers(second)
                    if self.keep_first:
                        # Если всё второе название в скобках, оно само — вариант.
                        title = (
                            ' '.join([first, *groups])
                            if second_base != second
                            else first
                        )
                    else:
                        title = second
            raw_name = artist + divider + title
        return INVALID_FILENAME_CHARS.sub(
            lambda match: '_' if match[0] == '/' else '', raw_name
        )


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
        if strategy_name == 'replace':
            return ReplaceCharsStrategy(params.get('char', '-'))
        if strategy_name in ('first_title', 'second_title'):
            return BilingualTitleStrategy(keep_first=(strategy_name == 'first_title'))
        if strategy_name == 'manual':
            return ManualInputStrategy(interactor, filename)
        if strategy_name == 'skip':
            raise ValueError("Strategy 'skip' should be handled before applying.")
        raise ValueError(f'Неизвестная стратегия: {strategy_name}')
