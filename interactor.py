"""Интерфейс и реализация взаимодействия с пользователем."""

from typing import Protocol


class UserInteractor(Protocol):
    """Протокол для взаимодействия с пользоватales.

    Позволяет внедрять различные реализации (консоль, GUI, тестовые моки).
    """

    def prompt(self, message: str, default: str = '') -> str:
        """Запрашивает ввод у пользователя."""
        ...

    def display(self, message: str) -> None:
        """Выводит обычное сообщение."""
        ...

    def display_error(self, message: str) -> None:
        """Выводит сообщение об ошибке."""
        ...


class ConsoleInteractor:
    """Стандартная консольная реализация UserInteractor."""

    def prompt(self, message: str, default: str = '') -> str:
        choice = input(message).strip().lower()
        if not choice and default:
            print(f'  [Выбран вариант {default} по умолчанию]')
            return default
        return choice

    def display(self, message: str) -> None:
        print(message)

    def display_error(self, message: str) -> None:
        print(f'  ❌ Ошибка: {message}')
