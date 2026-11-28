"""Интерфейс и реализация взаимодействия с пользователем."""

import logging
import os
import sys
from collections.abc import Callable
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


_MENU = (
    ('once', 'Safe один раз'),
    ('safe', 'Сохранить safe и запустить'),
    ('confirm', 'Запустить deep…'),
    ('remember_deep', 'Сохранить deep и выйти'),
    ('cancel', 'Отмена'),
)


def _navigate(read_key: Callable[[bool], str], *, windows: bool) -> str:
    index = 0
    print('Стрелки ↑/↓ — выбрать, Enter — подтвердить.')
    for number, (_, label) in enumerate(_MENU, 1):
        print(f'{number}. {label}')
    while True:
        print(f'Выбрано: {_MENU[index][1]}', flush=True)
        key = read_key(False)
        if key in ('\r', '\n'):
            return _MENU[index][0]
        if key in ('', '\x03', '\x04', '\x1a') or (windows and key == '\x1b'):
            raise EOFError
        if key not in (('\x00', '\xe0') if windows else ('\x1b',)):
            continue
        # После префикса нельзя отдавать остаток клавиши построчному меню.
        try:
            code = read_key(True)
            if not windows:
                if code not in ('[', 'O'):
                    raise EOFError
                code = read_key(True)
        except (OSError, ValueError) as exc:
            raise EOFError from exc
        if code == ('H' if windows else 'A'):
            index = max(0, index - 1)
        elif code == ('P' if windows else 'B'):
            index = min(len(_MENU) - 1, index + 1)
        else:
            raise EOFError


def _arrow_action() -> str | None:
    """Возвращает None, когда требуется построчный интерфейс."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    if sys.platform == 'win32':
        import ctypes
        import msvcrt
        import time
        from ctypes import wintypes

        for stream in (sys.stdin, sys.stdout):
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            mode = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return None

        def win_key(continuation: bool) -> str:
            if continuation:
                limit = time.monotonic() + 0.5
                while not msvcrt.kbhit():
                    if time.monotonic() >= limit:
                        raise EOFError
                    time.sleep(0.01)
            return msvcrt.getwch()

        return _navigate(win_key, windows=True)

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    try:
        original = termios.tcgetattr(fd)
    except termios.error:
        return None

    def posix_key(continuation: bool) -> str:
        if continuation and not select.select([fd], [], [], 0.5)[0]:
            raise EOFError
        return os.read(fd, 1).decode('latin1')

    try:
        try:
            tty.setcbreak(fd, termios.TCSANOW)
        except termios.error:
            return None
        return _navigate(posix_key, windows=False)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except termios.error as exc:
            raise EOFError('Не удалось восстановить режим терминала') from exc


def _warn_deep(ui: UserInteractor, dry_run: bool) -> None:
    if dry_run:
        ui.display(
            'Deep в dry-run выполняет прогноз удаления. Для запуска введите run.'
        )
    else:
        ui.display(
            'Программа не может проверить отсутствие сторонних изменений. '
            'Они могут добавить поток NTFS или подменить имя после проверки.'
        )
        ui.display(
            'Точные дубли будут удалены безвозвратно. Приостановите синхронизацию '
            'и другие программы, которые могут менять эту папку. Для запуска введите run'
        )


def _line_action(ui: UserInteractor, preferred: Mode | None, dry_run: bool) -> str:
    if preferred == 'deep':
        _warn_deep(ui, dry_run)
    for number, (_, label) in enumerate(_MENU, 1):
        ui.display(f'{number}. {label}')
    aliases = {str(i): action for i, (action, _) in enumerate(_MENU, 1)}
    aliases.update(
        {
            '': 'once',
            'once': 'once',
            'safe': 'safe',
            'cancel': 'cancel',
            'deep': 'confirm',
        }
    )
    if preferred == 'deep':
        aliases.update({'deep': 'remember_deep', 'run': 'run'})
    while True:
        answer = prompt_command(ui, 'Выберите номер или команду [1 — Safe один раз]: ')
        if answer in aliases:
            return aliases[answer]
        ui.display_error('Неизвестная команда. Выберите вариант из меню.')


def choose_mode(interactor: UserInteractor, *, dry_run: bool) -> Mode | None:
    path = None
    preferred = None
    preference = 'отсутствует'
    try:
        path = settings_path()
        preferred = load_mode(path)
        if preferred:
            preference = preferred
            interactor.display(f'Сохранённое предпочтение: {preferred}.')
    except (OSError, RuntimeError, ValueError) as exc:
        preference = 'не удалось прочитать'
        interactor.display_error(
            f'Не удалось прочитать настройки: {exc}. Доступен safe.'
        )

    try:
        try:
            action = (
                _arrow_action() if isinstance(interactor, ConsoleInteractor) else None
            )
        except (OSError, ValueError, ImportError) as exc:
            interactor.display(f'Стрелочный выбор недоступен: {exc}.')
            action = None
        if action is None:
            action = _line_action(interactor, preferred, dry_run)
        else:
            interactor.display(f'Выбор стрелками: {action}.')
        if action == 'confirm':
            _warn_deep(interactor, dry_run)
            while True:
                answer = prompt_command(
                    interactor, 'run — запустить deep, Enter/cancel — выйти: '
                )
                if answer in ('', 'cancel', 'once', 'safe', 'deep', 'run'):
                    action = {'': 'cancel', 'deep': 'remember_deep'}.get(answer, answer)
                    break
                interactor.display_error('Для запуска deep требуется команда run.')
    except (EOFError, KeyboardInterrupt):
        interactor.display('Выбор режима прерван. Обработка не начата.')
        action = 'cancel'

    mode: Mode | None = None
    if action in ('once', 'safe'):
        mode = 'safe'
    elif action == 'run':
        mode = 'deep'

    requested: Mode | None = None
    if action == 'safe':
        requested = 'safe'
    elif action in ('run', 'remember_deep'):
        requested = 'deep'
    if requested and requested != preferred and not dry_run:
        try:
            if path is None:
                raise OSError('путь недоступен')
            save_mode(path, requested)
            preference = requested
        except OSError as exc:
            interactor.display_error(f'Не удалось сохранить предпочтение: {exc}')
    if action == 'once':
        interactor.display(
            'Разовый запуск safe. Сохранённое предпочтение остаётся прежним.'
        )
    interactor.display(
        f'Выбор режима: {action}. Запуск: {mode or "отмена"}. '
        f'Предпочтение: {preference}.'
    )
    return mode


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
