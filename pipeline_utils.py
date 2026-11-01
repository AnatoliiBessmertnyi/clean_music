"""Утилиты для оркестрации пайплайна: безопасное переименование и интерактивный выбор."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from config import (FEAT_VARIANTS_PATTERN, INVALID_FILENAME_CHARS,
                    LEADING_TRACK_PATTERN)
from context import SessionContext
from interactor import UserInteractor
from naming import normalize_title_spacing
from parsing import clean_extracted_tags, parse_artist_title
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


def get_audio_duration(filepath: Path) -> float | None:
    """Возвращает длительность аудиофайла в секундах или None."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            return float(MP3(filepath).info.length)  # type: ignore[attr-defined]
        if ext in ('.m4a', '.mp4'):
            return float(MP4(filepath).info.length) # type: ignore[attr-defined]
        if ext == '.flac':
            return float(FLAC(filepath).info.length)  # type: ignore[union-attr]
        if ext == '.ogg':
            return float(OggVorbis(filepath).info.length)  # type: ignore[attr-defined]
        if ext == '.wma':
            return float(ASF(filepath).info.length)  # type: ignore[attr-defined]
    except Exception:
        pass
    return None


def format_duration(seconds: float) -> str:
    """Форматирует длительность в mm:ss."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{minutes}:{secs:02d}'


def format_file_size(size_bytes: int) -> str:
    """Форматирует размер файла в читаемый вид (KB, MB)."""
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    return f'{size_bytes / (1024 * 1024):.1f} MB'


def file_info_line(filepath: Path) -> str:
    """Формирует строку с размером и длительностью файла."""
    size = format_file_size(filepath.stat().st_size)
    dur = get_audio_duration(filepath)
    dur_str = f', {format_duration(dur)}' if dur else ''
    return f'{size}{dur_str}'


def name_quality_score(filepath: Path) -> int:
    """Оценивает качество имени файла (чем выше, тем лучше)."""
    if parse_artist_title(filepath.stem):
        return 3  # Формат "Artist - Title" (лучший)
    if LEADING_TRACK_PATTERN.match(filepath.stem):
        return 1  # Начинается с номера трека (худший)
    return 2  # Что-то среднее


# === Утилиты для анализа расхождений и санитизации ===

def has_encoding_issues(text: str) -> bool:
    """Проверяет, содержит ли текст признаки неправильной кодировки."""
    mojibake_pattern = re.compile(r'[À-ÿ]{2,}')
    return bool(mojibake_pattern.search(text))


def categorize_mismatch(
    filepath: Path, file_title: str, tag_title: str, tag_artist: str
) -> str:
    """Определяет категорию проблемы для файла с расхождением.

    Возвращает:
        'A' - файл с номером трека (нет исполнителя в имени)
        'B' - файл без тире (неправильный формат)
        'C' - файл с feat в имени (нужна нормализация)
        'D' - проблема с кодировкой в тегах
        'E' - спорный случай
    """
    if has_encoding_issues(f'{tag_artist} - {tag_title}'):
        return 'D'

    parsed = parse_artist_title(filepath.stem)
    if not parsed:
        if LEADING_TRACK_PATTERN.match(filepath.stem):
            return 'A'
        return 'B'

    if FEAT_VARIANTS_PATTERN.search(parsed[0]) or FEAT_VARIANTS_PATTERN.search(parsed[1]):
        return 'C'

    return 'E'


def sanitize_filename_interactive(
    name: str,
    current_filename: str,
    ctx: SessionContext,
    interactor: UserInteractor,
) -> str | None:
    """Интерактивно очищает имя файла от недопустимых символов."""
    invalid_chars = set(INVALID_FILENAME_CHARS.findall(name))
    if not invalid_chars:
        return name

    chars_key = frozenset(invalid_chars)
    chars_str = ''.join(sorted(invalid_chars))

    options: list[tuple[str, str]] = []

    # Опция 1: удалить символы
    opt1 = name
    for c in invalid_chars:
        opt1 = opt1.replace(c, '')
    opt1 = re.sub(r'\s+', ' ', opt1).strip()
    options.append((f'Удалить символы → {opt1}', opt1))

    # Опция 2: заменить на '-'
    opt2 = name
    for c in invalid_chars:
        opt2 = opt2.replace(c, '-')
    opt2 = re.sub(r'\s+', ' ', opt2).strip()
    options.append((f"Заменить на '-' → {opt2}", opt2))

    # Опция 3: заменить '/' на '; '
    if '/' in invalid_chars:
        opt3 = name.replace('/', '; ')
        for c in invalid_chars - {'/'}:
            opt3 = opt3.replace(c, '')
        opt3 = re.sub(r'\s+', ' ', opt3).strip()
        options.append((f"Заменить '/' на '; ' → {opt3}", opt3))

    # Опции 4-5: оставить первое/второе название (если есть '/')
    if '/' in invalid_chars:
        feat_suffix = ''
        feat_match = re.search(r'\s*\(feat\.\s*[^)]+\)\s*$', name)
        if feat_match:
            feat_suffix = feat_match.group(0)
            name_without_feat = name[: feat_match.start()]
        else:
            name_without_feat = name

        slash_match = re.search(r'^(.*?\s+-\s+)([^/]+)\s*/\s*(.+)$', name_without_feat)
        if slash_match:
            prefix = slash_match.group(1)
            first_title = slash_match.group(2).strip()
            second_title = slash_match.group(3).strip()

            opt4 = f'{prefix}{first_title}{feat_suffix}'
            options.append((f'Оставить первое название → {opt4}', opt4))

            opt5 = f'{prefix}{second_title}{feat_suffix}'
            options.append((f'Оставить второе название → {opt5}', opt5))

    options.append(('Ввести имя вручную', 'manual'))
    options.append((f'Оставить как есть: {current_filename}', 'SKIP'))

    # Проверяем запомненный выбор
    if chars_key in ctx.remembered_choices:
        remembered_idx = ctx.remembered_choices[chars_key]
        if 0 <= remembered_idx < len(options):
            _, value = options[remembered_idx]
            ctx.log_debug(
                f"Применяю запомненный выбор #{remembered_idx + 1} для символов '{chars_str}'"
            )
            if value not in ('manual', 'SKIP'):
                return value

    # Выводим меню
    interactor.display(f'\n  Внимание! Недопустимые символы: {chars_str}')
    interactor.display(f'  Предлагаемое имя: {name}')
    interactor.display(f'  Текущий файл:     {current_filename}')

    for i, (label, _) in enumerate(options, 1):
        marker = ' [по умолчанию]' if i == 1 else ''
        remembered = (
            ' [запомнено]'
            if (chars_key in ctx.remembered_choices and ctx.remembered_choices[chars_key] == i - 1)
            else ''
        )
        interactor.display(f'  {i}. {label}{marker}{remembered}')

    interactor.display(f"  a. Запомнить и применить вариант 1 ко всем файлам с '{chars_str}'")

    # Подсказка при повторении
    if chars_key not in ctx.ask_counts:
        ctx.ask_counts[chars_key] = 0
    ctx.ask_counts[chars_key] += 1

    count = ctx.ask_counts[chars_key]
    if count >= 3:
        interactor.display(
            f'  [Подсказка: вы уже {count} раз выбрали этот вариант. '
            f"Нажмите 'a', чтобы запомнить.]"
        )

    choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

    if choice == 'a':
        ctx.remembered_choices[chars_key] = 0
        interactor.display(f"  Запомнено: применять 'Удалить символы' для всех '{chars_str}'")
        _, value = options[0]
        return value

    if choice in ('n', 'no', 'н', 'нет'):
        return 'SKIP'

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            _, value = options[idx]
            if value == 'manual':
                custom = interactor.prompt('  Введите имя (без расширения): ').strip()
                if not custom:
                    return 'SKIP'
                if INVALID_FILENAME_CHARS.search(custom):
                    interactor.display('  Имя всё ещё содержит недопустимые символы, оставляю как есть.')
                    return 'SKIP'
                return custom
            return value
    except ValueError:
        pass

    return 'SKIP'


def clean_filename_safely(
    raw_name: str,
    current_filename: str,
    ctx: SessionContext,
    interactor: UserInteractor,
) -> str | None:
    """Очищает имя файла с проверкой недопустимых символов."""
    if not INVALID_FILENAME_CHARS.search(raw_name):
        return raw_name
    return sanitize_filename_interactive(raw_name, current_filename, ctx, interactor)
