"""Интерфейс и реализация взаимодействия с пользователем."""

import logging
from typing import Protocol

from user_settings import Mode, load_mode, save_mode, settings_path

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


def choose_safe_mode(interactor: UserInteractor, *, dry_run: bool) -> bool:
    """Возвращает разрешение на запуск safe после завершения выбора."""
    path = None
    preferred = None
    try:
        path = settings_path()
        preferred = load_mode(path)
    except (OSError, RuntimeError, ValueError) as exc:
        interactor.display_error(
            f'Не удалось прочитать настройки: {exc}. Доступен safe.'
        )

    def remember(mode: Mode) -> None:
        if dry_run:
            interactor.display(
                'В dry-run предпочтение сохраняется только на этот запуск.'
            )
            return
        if path is None:
            interactor.display_error(
                'Не удалось сохранить предпочтение: путь недоступен.'
            )
            return
        try:
            save_mode(path, mode)
        except OSError as exc:
            interactor.display_error(f'Не удалось сохранить предпочтение: {exc}')
        else:
            interactor.display(
                f'Сохранено предпочтение для следующего запуска: {mode}.'
            )

    def choose_after_deep() -> bool:
        interactor.display(
            'Глубокий режим deep пока недоступен. Он предусматривает '
            'безвозвратное удаление достоверных дублей.'
        )
        while True:
            choice = prompt_command(
                interactor,
                'once — запустить safe один раз, safe — сохранить и запустить safe, '
                'deep — сохранить deep и выйти, Enter/cancel — выйти: ',
            )
            if choice in ('', 'cancel'):
                return False
            if choice == 'once':
                interactor.display(
                    'Разовый запуск safe. Сохранённое предпочтение остаётся прежним.'
                )
                return True
            if choice == 'safe' or choice == 'deep':
                mode: Mode = 'safe' if choice == 'safe' else 'deep'
                if mode != preferred:
                    remember(mode)
                return mode == 'safe'
            interactor.display_error('Неизвестная команда. Выберите вариант из меню.')

    try:
        if preferred == 'deep':
            interactor.display('Сохранённое предпочтение: deep.')
            return choose_after_deep()
        if preferred == 'safe':
            interactor.display('Сохранённое предпочтение: safe.')
        while True:
            choice = prompt_command(
                interactor,
                'Enter/safe — запустить безопасный режим, deep — выбрать глубокий, '
                'cancel — выйти: ',
                default='safe',
            )
            if choice == 'safe':
                if preferred != 'safe':
                    remember('safe')
                return True
            if choice == 'deep':
                return choose_after_deep()
            if choice == 'cancel':
                return False
            interactor.display_error('Неизвестная команда. Выберите вариант из меню.')
    except (EOFError, KeyboardInterrupt):
        interactor.display('Выбор режима прерван. Обработка не начата.')
        return False


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
