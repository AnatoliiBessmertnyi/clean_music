"""Интерфейс и реализация взаимодействия с пользователем."""

import logging
from typing import Protocol

logger = logging.getLogger('clean_music')


def logged_input(message: str) -> str:
    """Сохраняет вопрос и исходный ответ; EOF/Ctrl+C фиксирует запуск CLI."""
    logger.info('ВОПРОС: %s', message)
    answer = input(message)
    logger.info('ОТВЕТ: %r', answer)
    return answer


class UserInteractor(Protocol):
    """Протокол для взаимодействия с пользоватales.

    Позволяет внедрять различные реализации (консоль, GUI, тестовые моки).
    """

    def prompt(self, message: str, default: str = '') -> str:
        """Читает ответ с исходным регистром и пропускает исключения ввода."""
        ...

    def display(self, message: str) -> None:
        """Выводит обычное сообщение."""
        ...

    def display_error(self, message: str) -> None:
        """Выводит сообщение об ошибке."""
        ...


def prompt_command(interactor: UserInteractor, message: str, default: str = '') -> str:
    """Читает команду меню без учёта регистра."""
    return interactor.prompt(message, default=default).strip().lower()


class ConsoleInteractor:
    """Стандартная консольная реализация UserInteractor."""

    def prompt(self, message: str, default: str = '') -> str:
        choice = logged_input(message).strip()
        if not choice and default:
            self.display(f'  [Выбран вариант {default} по умолчанию]')
            return default
        return choice

    def display(self, message: str) -> None:
        logger.info(message)
        print(message)

    def display_error(self, message: str) -> None:
        logger.error(message)
        print(f'  ❌ Ошибка: {message}')
