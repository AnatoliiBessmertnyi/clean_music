"""Интерфейс и реализация взаимодействия с пользователем."""

import logging
from dataclasses import dataclass
from pathlib import Path
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
    ('once', 'Safe один раз — без изменения предпочтения'),
    ('safe', 'Сохранить safe и запустить'),
    ('confirm', 'Запустить deep — после run сохранить deep (кроме dry-run)'),
    ('remember_deep', 'Сохранить deep и выйти'),
    ('cancel', 'Отмена'),
)


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
        ui.display(f'[{number}] {label}')
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
        answer = prompt_command(
            ui, '1/2 Выберите номер или команду [1 — Safe один раз]: '
        )
        if answer in aliases:
            return aliases[answer]
        ui.display_error('Неизвестная команда. Выберите вариант из меню.')


@dataclass(frozen=True)
class ModeChoice:
    mode: Mode
    remember: bool
    path: Path | None

    def save_preference(self, ui: UserInteractor) -> None:
        if self.remember:
            try:
                if self.path is None:
                    raise OSError('путь недоступен')
                save_mode(self.path, self.mode)
            except OSError as exc:
                ui.display_error(f'Не удалось сохранить предпочтение: {exc}')
            else:
                ui.display(f'Сохранено предпочтение: {self.mode}.')


def choose_mode(interactor: UserInteractor, *, dry_run: bool) -> ModeChoice | None:
    path = None
    preferred = None
    interactor.display('1/2 Выберите режим')
    try:
        path = settings_path()
        preferred = load_mode(path)
        if preferred:
            interactor.display(f'Сохранённое предпочтение: {preferred}.')
    except (OSError, RuntimeError, ValueError) as exc:
        interactor.display_error(
            f'Не удалось прочитать настройки: {exc}. Доступен safe.'
        )

    try:
        action = _line_action(interactor, preferred, dry_run)
        if action == 'confirm':
            _warn_deep(interactor, dry_run)
            while True:
                answer = prompt_command(
                    interactor, '1/2 run — запустить deep, Enter/cancel — выйти: '
                )
                if answer in ('', 'cancel', 'once', 'safe', 'deep', 'run'):
                    action = {'': 'cancel', 'deep': 'remember_deep'}.get(answer, answer)
                    break
                interactor.display_error('Для запуска deep требуется команда run.')
    except (EOFError, KeyboardInterrupt) as exc:
        interactor.display(f'1/2 {type(exc).__name__}: выбор прерван.')
        action = 'cancel'

    mode: Mode | None = None
    if action in ('once', 'safe'):
        mode = 'safe'
    elif action == 'run':
        mode = 'deep'

    interactor.display(f'1/2 Выбор режима: {action}. Запуск: {mode or "отмена"}.')
    if action == 'once':
        interactor.display(
            'Разовый запуск safe. Сохранённое предпочтение остаётся прежним.'
        )
    if action == 'remember_deep':
        ModeChoice('deep', not dry_run and preferred != 'deep', path).save_preference(
            interactor
        )
    if mode is None:
        return None
    return ModeChoice(
        mode, action != 'once' and mode != preferred and not dry_run, path
    )


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
