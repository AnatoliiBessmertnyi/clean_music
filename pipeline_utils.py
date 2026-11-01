"""Утилиты для оркестрации пайплайна: безопасное переименование и интерактивный выбор."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from config import INVALID_FILENAME_CHARS
from context import SessionContext
from interactor import UserInteractor
from tag_repository import TagRepository


def select_indices(items: list[str], description: str, interactor: UserInteractor) -> list[int]:
    """Показывает нумерованный список и возвращает индексы выбранных элементов."""
    interactor.display(f'\n{description}:')
    for i, item in enumerate(items, 1):
        interactor.display(f'  [✓] {i}. {item}')

    prompt = '(Enter - применить ко всем / номера - ИСКЛЮЧИТЬ из обработки / n - отменить все): '
    choice = interactor.prompt(prompt, default='').strip().lower()
    
    if not choice or choice in ('y', 'yes', 'д', 'да'):
        return list(range(len(items)))
    if choice in ('n', 'no', 'н', 'нет'):
        return []
    
    try:
        excluded = {int(p) for p in re.split(r'[,\s]+', choice) if p}
        selected = [i for i in range(len(items)) if (i + 1) not in excluded]
        skipped = len(items) - len(selected)
        if skipped:
            interactor.display(f'Исключено {skipped} файлов.')
        return selected
    except ValueError:
        interactor.display('Некорректный ввод, применяю ко всем.')
        return list(range(len(items)))


def files_are_same(old_path: Path, new_path: Path) -> bool:
    """Проверяет, являются ли два пути одним и тем же файлом на диске."""
    try:
        return old_path.samefile(new_path)
    except (OSError, FileNotFoundError, ValueError):
        return False


def rename_case_sensitive(old_path: Path, new_path: Path, ctx: SessionContext) -> bool:
    """Переименовывает файл с изменением только регистра (Windows)."""
    temp_name = new_path.stem + '__temp__' + new_path.suffix
    temp_path = old_path.with_name(temp_name)
    try:
        old_path.rename(temp_path)
        temp_path.rename(new_path)
        return True
    except OSError as e:
        ctx.log_debug(f'Ошибка при case-sensitive rename: {e}')
        if temp_path.exists():
            try:
                temp_path.rename(old_path)
            except OSError:
                pass
        return False


def safe_unlink(filepath: Path, interactor: UserInteractor) -> bool:
    """Безопасно удаляет файл, обрабатывая ошибки блокировки."""
    try:
        filepath.unlink()
        return True
    except PermissionError:
        interactor.display_error(f'файл заблокирован, не могу удалить: {filepath.name}')
        return False
    except OSError as e:
        interactor.display_error(f'при удалении {filepath.name}: {e}')
        return False


def safe_rename(
    old_path: Path, new_path: Path, ctx: SessionContext, repo: TagRepository
) -> tuple[str, Path | None]:
    """Безопасно переименовывает файл, обрабатывая дубликаты и смену регистра."""
    ctx.log_debug(f'rename: {old_path.name!r} -> {new_path.name!r}')

    if not old_path.exists():
        ctx.log_debug(f'Файл не найден: {old_path.name}')
        return 'skipped', None

    if files_are_same(old_path, new_path):
        ctx.log_debug('Это один и тот же файл (samefile), меняем только регистр')
        if ctx.dry_run:
            ctx.log_debug('[DRY RUN] Пропускаю смену регистра')
            return 'renamed', new_path
        if rename_case_sensitive(old_path, new_path, ctx):
            ctx.log_debug(f'Регистр успешно изменён: {new_path.name}')
            return 'renamed', new_path
        ctx.log_debug('Не удалось изменить регистр, пропускаю')
        return 'skipped', None

    if not new_path.exists():
        if ctx.dry_run:
            ctx.log_debug(f'[DRY RUN] Переименовал бы: {old_path.name} -> {new_path.name}')
            repo.simulate_rename(old_path, new_path)
            return 'renamed', new_path
        old_path.rename(new_path)
        repo.simulate_rename(old_path, new_path)
        return 'renamed', new_path

    return 'duplicate_exists', new_path


def handle_rename_result(
    status: str,
    old_path: Path,
    result_path: Path | None,
    ctx: SessionContext,
    stat_key: str,
) -> Path | None:
    """Обрабатывает результат safe_rename и обновляет статистику."""
    if status == 'renamed':
        if result_path is not None:
            ctx.log_debug(f'Переименован: {old_path.name} -> {result_path.name}')
        ctx.increment_stat(stat_key)
        return result_path

    if status == 'kept_existing':
        if result_path is not None:
            ctx.log_debug(
                f'Текущий файл удалён как дубликат: {old_path.name} '
                f'(файл {result_path.name} оставлен без изменений)'
            )
        ctx.increment_stat(stat_key)
        return result_path

    ctx.log_debug(f'Пропущен: {old_path.name}')
    return old_path


def process_rename_batch(
    files: list[Path],
    candidates: list[Path],
    transform: Callable[[str], str],
    description: str,
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
    stat_key: str,
) -> list[Path]:
    """Универсальная обработка пакета переименований."""
    if not candidates:
        return files
    
    labels = [f'{f.name}  ->  {transform(f.stem)}{f.suffix}' for f in candidates]
    idx = select_indices(labels, f'Найдено {len(candidates)} {description}', interactor)
    chosen = {candidates[i] for i in idx}
    
    new_files = []
    for f in files:
        if f in chosen:
            new_path = f.with_name(transform(f.stem) + f.suffix)
            status, result_path = safe_rename(f, new_path, ctx, repo)
            result = handle_rename_result(status, f, result_path, ctx, stat_key)
            assert result is not None
            new_files.append(result)
        else:
            new_files.append(f)

    if ctx.get_stat(stat_key) > 0:
        interactor.display(f'  Обработано файлов: {ctx.get_stat(stat_key)}')

    return new_files
