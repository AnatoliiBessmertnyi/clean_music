import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from config import (
    ARTIST_NAME_EXCEPTIONS,
    FEAT_VARIANTS_PATTERN,
    INVALID_FILENAME_CHARS,
    JUNK_PATTERN,
    LEADING_TRACK_PATTERN,
    SUPPORTED_EXTENSIONS,
    TOTAL_STAGES,
)
from context import SessionContext
from interactor import ConsoleInteractor
from naming import (
    build_safe_filename,
    normalize_dash,
    normalize_filename_stem,
    normalize_title_spacing,
    normalize_unicode,
    remove_annotation_from_text,
    remove_junk,
)
from parsing import (
    clean_extracted_tags,
    extract_title_from_filename,
    parse_artist_title,
)
from strategies import StrategyFactory
from tag_repository import AudioTags, TagRepository


def show_progress(current: int, total: int, prefix: str = '') -> None:
    """Показывает прогресс-бар для больших коллекций."""
    if total <= 50:  # Не показываем для маленьких коллекций
        return
    percent = (current / total) * 100
    bar_length = 30
    filled = int(bar_length * current / total)
    bar = '█' * filled + '░' * (bar_length - filled)
    print(f'\r{prefix}{current}/{total} [{bar}] {percent:.0f}%', end='', flush=True)
    if current == total:
        print()


def has_encoding_issues(text: str) -> bool:
    """Проверяет, содержит ли текст признаки неправильной кодировки."""
    # Типичные артефакты двойной кодировки (кириллица в Latin-1)
    mojibake_pattern = re.compile(r'[À-ÿ]{2,}')
    return bool(mojibake_pattern.search(text))


def categorize_mismatch(filepath: Path, file_title: str, tag_title: str, tag_artist: str) -> str:
    """Определяет категорию проблемы для файла с расхождением.

    Возвращает:
        'A' - файл с номером трека (нет исполнителя в имени)
        'B' - файл без тире (неправильный формат)
        'C' - файл с feat в имени (нужна нормализация)
        'D' - проблема с кодировкой в тегах
        'E' - спорный случай
    """
    # Проверяем кодировку
    if has_encoding_issues(f'{tag_artist} - {tag_title}'):
        return 'D'

    # Проверяем, есть ли формат "Artist - Title" в имени
    parsed = parse_artist_title(filepath.stem)
    if not parsed:
        # Нет формата - проверяем, есть ли номер трека
        if LEADING_TRACK_PATTERN.match(filepath.stem):
            return 'A'  # Есть номер трека, но нет исполнителя
        else:
            return 'B'  # Нет тире вообще

    # Есть формат, проверяем наличие feat в имени
    if FEAT_VARIANTS_PATTERN.search(parsed[0]) or FEAT_VARIANTS_PATTERN.search(parsed[1]):
        return 'C'

    # Всё остальное - спорные случаи
    return 'E'


def sanitize_filename_interactive(
    name: str, current_filename: str, ctx: SessionContext, interactor: ConsoleInteractor
) -> str | None:
    """Интерактивно очищает имя файла от недопустимых символов."""
    invalid_chars = set(INVALID_FILENAME_CHARS.findall(name))
    if not invalid_chars:
        return name

    chars_key = frozenset(invalid_chars)
    chars_str = ''.join(sorted(invalid_chars))

    options = []

    # Существующие опции
    opt1 = name
    for c in invalid_chars:
        opt1 = opt1.replace(c, '')
    opt1 = re.sub(r'\s+', ' ', opt1).strip()
    options.append((f'Удалить символы → {opt1}', opt1))

    opt2 = name
    for c in invalid_chars:
        opt2 = opt2.replace(c, '-')
    opt2 = re.sub(r'\s+', ' ', opt2).strip()
    options.append((f"Заменить на '-' → {opt2}", opt2))

    if '/' in invalid_chars:
        opt3 = name.replace('/', '; ')
        for c in invalid_chars - {'/'}:
            opt3 = opt3.replace(c, '')
        opt3 = re.sub(r'\s+', ' ', opt3).strip()
        options.append((f"Заменить '/' на '; ' → {opt3}", opt3))

    if '/' in invalid_chars:
        # Извлекаем (feat. ...) отдельно, чтобы не потерять при разделении
        feat_suffix = ''
        feat_match = re.search(r'\s*\(feat\.\s*[^)]+\)\s*$', name)
        if feat_match:
            feat_suffix = feat_match.group(0)
            name_without_feat = name[: feat_match.start()]
        else:
            name_without_feat = name

        # Пытаемся найти паттерн "Artist - Name1 / Name2"
        slash_match = re.search(r'^(.*?\s+-\s+)([^/]+)\s*/\s*(.+)$', name_without_feat)
        if slash_match:
            prefix = slash_match.group(1)  # "Artist - "
            first_title = slash_match.group(2).strip()  # "Name1"
            second_title = slash_match.group(3).strip()  # "Name2"

            # Опция: оставить первое название (с сохранением feat)
            opt4 = f'{prefix}{first_title}{feat_suffix}'
            options.append((f'Оставить первое название → {opt4}', opt4))

            # Опция: оставить второе название (с сохранением feat)
            opt5 = f'{prefix}{second_title}{feat_suffix}'
            options.append((f'Оставить второе название → {opt5}', opt5))

    options.append(('Ввести имя вручную', 'manual'))
    options.append((f'Оставить как есть: {current_filename}', 'SKIP'))

    # Проверяем, есть ли запомненный выбор
    if chars_key in ctx.remembered_choices:
        remembered_idx = ctx.remembered_choices[chars_key]
        if 0 <= remembered_idx < len(options):
            _, value = options[remembered_idx]
            ctx.log_debug(
                f"Применяю запомненный выбор #{remembered_idx + 1} для символов '{chars_str}'"
            )
            # Для "manual" и "SKIP" не применяем запомненный выбор
            if value not in ('manual', 'SKIP'):
                return value

    # Выводим меню
    print(f'\n  Внимание! Недопустимые символы: {chars_str}')
    print(f'  Предлагаемое имя: {name}')
    print(f'  Текущий файл:     {current_filename}')

    for i, (label, _) in enumerate(options, 1):
        marker = ' [по умолчанию]' if i == 1 else ''
        remembered = (
            ' [запомнено]'
            if (chars_key in ctx.remembered_choices and ctx.remembered_choices[chars_key] == i - 1)
            else ''
        )
        print(f'  {i}. {label}{marker}{remembered}')

    print(f"  a. Запомнить и применить вариант 1 ко всем файлам с '{chars_str}'")

    # Проверяем, сколько раз уже выбирали этот символ
    if chars_key not in ctx.remembered_choices:
        if chars_key not in ctx.ask_counts:
            ctx.ask_counts[chars_key] = 0
        ctx.ask_counts[chars_key] += 1

        count = ctx.ask_counts[chars_key]
        if count >= 3:
            print(
                f'  [Подсказка: вы уже {count} раз выбрали этот вариант. '
                f"Нажмите 'a', чтобы запомнить.]"
            )

    choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

    # Обработка "запомнить для всех"
    if choice == 'a':
        ctx.remembered_choices[chars_key] = 0
        print(f"  Запомнено: применять 'Удалить символы' для всех '{chars_str}'")
        _, value = options[0]
        return value

    if choice in ('n', 'no', 'н', 'нет'):
        return 'SKIP'

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            _, value = options[idx]
            if value == 'manual':
                custom = input('  Введите имя (без расширения): ').strip()
                if not custom:
                    return 'SKIP'
                if INVALID_FILENAME_CHARS.search(custom):
                    print('  Имя всё ещё содержит недопустимые символы, оставляю как есть.')
                    return 'SKIP'
                return custom
            return value
    except ValueError:
        pass

    return 'SKIP'


def clean_filename_safely(
    raw_name: str, current_filename: str, ctx: SessionContext, interactor: ConsoleInteractor
) -> str | None:
    """Очищает имя файла с проверкой недопустимых символов."""
    if not INVALID_FILENAME_CHARS.search(raw_name):
        return raw_name

    return sanitize_filename_interactive(raw_name, current_filename, ctx, interactor)


def stage(number: int, name: str, ctx: SessionContext) -> None:
    """Печатает заголовок этапа обработки."""
    print(f'\n{"=" * 60}')
    print(f'[Этап {number}/{TOTAL_STAGES}] {name}')
    print(f'{"=" * 60}')
    ctx.log_debug(f'Начинаю этап {number}/{TOTAL_STAGES}: {name}')


def get_valid_path() -> Path:
    """Запрашивает у пользователя путь и проверяет его корректность."""
    while True:
        raw = input('Введите путь к папке: ').strip().strip('"')
        path_obj = Path(raw)
        if path_obj.is_dir():
            return path_obj
        print('Ошибка: путь не является папкой. Попробуйте снова.')


def get_audio_files(directory: Path) -> list[Path]:
    """Возвращает список всех аудиофайлов в заданной директории."""
    return [f for f in directory.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS]


def get_format_stats(files: list[Path]) -> dict[str, int]:
    """Подсчитывает количество файлов по форматам."""
    counter = Counter(f.suffix.lower() for f in files)
    return dict(counter)


def format_stats_message(stats: dict[str, int]) -> str:
    """Форматирует статистику по форматам для вывода."""
    parts = []
    for ext, count in sorted(stats.items(), key=lambda x: -x[1]):
        ext_upper = ext.upper().lstrip('.')
        parts.append(f'{count} {ext_upper}')
    return ', '.join(parts)


def format_file_size(size_bytes: int) -> str:
    """Форматирует размер файла в читаемый вид (KB, MB)."""
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    return f'{size_bytes / (1024 * 1024):.1f} MB'


def get_audio_duration(filepath: Path) -> float | None:
    """Возвращает длительность аудиофайла в секундах или None."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            return float(MP3(filepath).info.length)  # type: ignore
        if ext in ('.m4a', '.mp4'):
            return float(MP4(filepath).info.length)  # type: ignore
        if ext == '.flac':
            return float(FLAC(filepath).info.length)  # type: ignore
        if ext == '.ogg':
            return float(OggVorbis(filepath).info.length)  # type: ignore
        if ext == '.wma':
            return float(ASF(filepath).info.length)  # type: ignore
    except Exception:
        pass
    return None


def format_duration(seconds: float) -> str:
    """Форматирует длительность в mm:ss."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{minutes}:{secs:02d}'


def file_info_line(filepath: Path) -> str:
    """Формирует строку с размером и длительностью файла."""
    size = format_file_size(filepath.stat().st_size)
    dur = get_audio_duration(filepath)
    dur_str = f', {format_duration(dur)}' if dur else ''
    return f'{size}{dur_str}'


def find_tag_annotations(files: list[Path], repo: TagRepository) -> dict[str, list[Path]]:
    """Находит уникальные скобки в тегах названий."""
    annotations: dict[str, list[Path]] = {}
    bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')

    for f in files:
        if not f.exists():
            continue
        tags = repo.get_tags(f)
        if tags:
            title = tags.title
            matches = bracket_pattern.findall(title)
            for match in matches:
                match = match.strip()
                if not match:
                    continue
                if JUNK_PATTERN.search(f'({match})'):
                    continue
                if FEAT_VARIANTS_PATTERN.match(match):
                    continue
                if match not in annotations:
                    annotations[match] = []
                if f not in annotations[match]:
                    annotations[match].append(f)

    return annotations


def normalize_feat_pair(artist: str, title: str) -> tuple[str, str]:
    """Нормализует пару тегов: переносит feat из artist в title.

    Правила:
    - feat должен быть ТОЛЬКО в title
    - Из artist feat удаляется
    - Все гости объединяются в один блок (feat. X, Y, Z)
    - Если в блоке feat есть тире (feat. X - Y),
      то Y — это название, X — гость
    """
    # 1. Извлекаем всех feat-гостей из artist
    artist_guests = []
    cleaned_artist = artist

    feat_matches = list(FEAT_VARIANTS_PATTERN.finditer(artist))
    if feat_matches:
        for i, match in enumerate(feat_matches):
            start = match.end()
            if i + 1 < len(feat_matches):
                end = feat_matches[i + 1].start()
            else:
                end = len(artist)
            guest_part = artist[start:end].strip()
            for g in re.split(r'\s*[&,]\s*', guest_part):
                g = g.strip()
                if g and g not in artist_guests:
                    artist_guests.append(g)

        base = artist
        for i, match in enumerate(feat_matches):
            if i + 1 < len(feat_matches):
                end = feat_matches[i + 1].start()
            else:
                end = len(artist)
            base = base[: match.start()] + base[end:]
        cleaned_artist = re.sub(r'\s+', ' ', base).strip()

    # 2. Извлекаем feat-гостей из title
    title_guests = []
    cleaned_title = title
    real_title_part = ''  # часть, которая оказалась названием (после тире в feat)

    # Ищем feat в скобках
    bracket_feat_pattern = re.compile(
        r'[\(\[]\s*(?:feat(?:uring)?|ft)\b\.?\s*([^\)\]]+)[\)\]]', re.IGNORECASE
    )
    matches = list(bracket_feat_pattern.finditer(title))
    if matches:
        result_parts = []
        last_end = 0
        for match in matches:
            result_parts.append(title[last_end : match.start()])
            guests_str = match.group(1)
            for guest in re.split(r'\s*[&,]\s*', guests_str):
                guest = guest.strip()
                if guest and guest not in title_guests:
                    title_guests.append(guest)
            last_end = match.end()
        result_parts.append(title[last_end:])
        cleaned_title = ''.join(result_parts)
        cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()

    # Ищем feat вне скобок в title
    title_feat_matches = list(FEAT_VARIANTS_PATTERN.finditer(cleaned_title))
    if title_feat_matches:
        base_parts = []
        last_end = 0
        for i, match in enumerate(title_feat_matches):
            base_parts.append(cleaned_title[last_end : match.start()])
            start = match.end()
            if i + 1 < len(title_feat_matches):
                end = title_feat_matches[i + 1].start()
            else:
                end = len(cleaned_title)
            guest_part = cleaned_title[start:end].strip()

            # ПРОВЕРКА: есть ли тире в гостевой части?
            # Если "feat. X - Y", то Y — это реальное название
            dash_match = re.search(r'\s+[-–—]\s+', guest_part)
            if dash_match:
                guest_only = guest_part[: dash_match.start()].strip()
                title_part = guest_part[dash_match.end() :].strip()
                if title_part:
                    real_title_part = title_part

                for g in re.split(r'\s*[&,]\s*', guest_only):
                    g = g.strip()
                    if g and g not in title_guests:
                        title_guests.append(g)
            else:
                for g in re.split(r'\s*[&,]\s*', guest_part):
                    g = g.strip()
                    if g and g not in title_guests:
                        title_guests.append(g)

            last_end = end

        base_parts.append(cleaned_title[last_end:])
        cleaned_title = ''.join(base_parts)
        cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()

        # Если нашли реальное название после тире — используем его
        if real_title_part:
            cleaned_title = real_title_part

    # 3. Объединяем гостей
    all_guests = []
    for g in artist_guests + title_guests:
        if g not in all_guests:
            all_guests.append(g)

    # 4. Формируем финальный title с feat
    if all_guests:
        guests_str = ', '.join(all_guests)
        final_title = f'{cleaned_title} (feat. {guests_str})'
    else:
        final_title = cleaned_title

    final_title = normalize_title_spacing(final_title)

    return cleaned_artist, final_title


def normalize_artist_separators(artist: str) -> str:
    """Нормализует разделители исполнителей на запятую с пробелом.

    Правила:
    - ';' → ',' (стандартная замена)
    - '/' → ',' (коллаборация), кроме групп из списка исключений
    - Группы типа 'AC/DC' не обрабатываются как коллаборация

    Примеры:
    - 'Artist1;Artist2' → 'Artist1, Artist2'
    - 'Dr. Dre/Snoop Dogg' → 'Dr. Dre, Snoop Dogg'
    - 'AC/DC' → 'AC/DC' (без изменений, имя группы)
    """
    # Проверяем, не является ли исполнитель исключением
    artist_lower = artist.lower()
    for exception in ARTIST_NAME_EXCEPTIONS:
        if exception.lower() in artist_lower:
            # Не трогаем '/' в именах групп из списка исключений
            # Но всё равно нормализуем ; если они есть
            result = artist.replace(';', ',')
            result = re.sub(r'\s*,\s*', ', ', result)
            result = re.sub(r'\s+', ' ', result).strip()
            return result

    # Для остальных: заменяем ; и / на ,
    result = artist.replace(';', ',')
    result = re.sub(r'\s*/\s*', ', ', result)
    result = re.sub(r'\s*,\s*', ', ', result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


def remove_feat_guests_from_artist(artist: str, title: str) -> str:
    """Удаляет гостей из Artist, если они уже указаны в Title через (feat.).

    По стандарту отрасли гости должны быть только в Title,
    а в Artist — только основные исполнители.
    """
    # Извлекаем гостей из Title: (feat. X, Y, Z)
    feat_pattern = re.compile(r'\(feat\.\s*([^\)]+)\)', re.IGNORECASE)
    match = feat_pattern.search(title)
    if not match:
        return artist

    guests_str = match.group(1)
    guests = [g.strip() for g in re.split(r'\s*,\s*', guests_str)]

    # Разделяем Artist на исполнителей (по запятой или точке с запятой)
    artists = [a.strip() for a in re.split(r'\s*[,;]\s*', artist)]

    # Удаляем гостей из Artist (сравнение без учёта регистра)
    guests_lower = {g.lower() for g in guests}
    filtered_artists = [a for a in artists if a.lower() not in guests_lower]

    if not filtered_artists:
        return artist  # Если все удалены, возвращаем оригинал

    return ', '.join(filtered_artists)


def name_quality_score(filepath: Path) -> int:
    """Оценивает качество имени файла (чем выше, тем лучше)."""
    if parse_artist_title(filepath.stem):
        return 3  # Формат "Artist - Title" (лучший)
    if LEADING_TRACK_PATTERN.match(filepath.stem):
        return 1  # Начинается с номера трека (худший)
    return 2  # Что-то среднее


def get_best_name_from_group(group: list[Path]) -> Path:
    """Выбирает файл с наиболее правильным именем из группы дубликатов."""
    return max(group, key=name_quality_score)


def read_audio_tags(filepath: Path, repo: TagRepository) -> AudioTags | None:
    """Читает теги из аудиофайла через репозиторий.

    Args:
        filepath: Путь к аудиофайлу
        repo: Репозиторий тегов

    Returns:
        AudioTags или None
    """
    return repo.get_tags(filepath)


def write_audio_tags(filepath: Path, artist: str, title: str, repo: TagRepository) -> bool:
    """Записывает теги через репозиторий.

    Args:
        filepath: Путь к аудиофайлу
        artist: Имя исполнителя
        title: Название трека
        repo: Репозиторий тегов

    Returns:
        True если запись успешна
    """
    return repo.update_tags(filepath, artist, title)


def titles_match(file_title: str, tag_title: str) -> bool:
    """Проверяет, совпадают ли названия (с допуском на различия)."""
    f = file_title.lower().strip()
    t = tag_title.lower().strip()
    if f == t:
        return True
    if f in t or t in f:
        return True
    return False


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


def safe_unlink(filepath: Path) -> bool:
    """Безопасно удаляет файл, обрабатывая ошибки блокировки."""
    try:
        filepath.unlink()
        return True
    except PermissionError:
        print(f'  Ошибка: файл заблокирован, не могу удалить: {filepath.name}')
        return False
    except OSError as e:
        print(f'  Ошибка при удалении {filepath.name}: {e}')
        return False


def simple_rename(
    old_path: Path, new_path: Path, ctx: SessionContext, repo: TagRepository
) -> tuple[str, Path | None]:
    """Простое переименование без обработки дубликатов."""
    ctx.log_debug(f'simple_rename: {old_path.name!r} -> {new_path.name!r}')

    if not old_path.exists():
        print(f'  Файл не найден: {old_path.name}')
        return 'skipped', None

    if files_are_same(old_path, new_path):
        ctx.log_debug('Это один и тот же файл (samefile), меняем только регистр')
        if ctx.dry_run:
            ctx.log_debug('[DRY RUN] Пропускаю смену регистра')
            return 'renamed', new_path
        if rename_case_sensitive(old_path, new_path, ctx):
            ctx.log_debug(f'Регистр успешно изменён: {new_path.name}')
            repo.simulate_rename(old_path, new_path)
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


def safe_rename(
    old_path: Path, new_path: Path, ctx: SessionContext, repo: TagRepository
) -> tuple[str, Path | None]:
    """Безопасно переименовывает файл, обрабатывая дубликаты."""
    ctx.log_debug(f'rename: {old_path.name!r} -> {new_path.name!r}')

    if not old_path.exists():
        print(f'  Файл не найден: {old_path.name}')
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

    # Дубликат обнаружен — решаем автоматически
    old_tags = repo.get_tags(old_path)
    existing_tags = repo.get_tags(new_path)

    tags_match = (
        old_tags
        and existing_tags
        and old_tags.artist.lower().strip() == existing_tags.artist.lower().strip()
        and old_tags.title.lower().strip() == existing_tags.title.lower().strip()
    )

    old_info = file_info_line(old_path)
    existing_info = file_info_line(new_path)

    old_score = (old_path.stat().st_size, name_quality_score(old_path))
    new_score = (new_path.stat().st_size, name_quality_score(new_path))

    print(f'\n  Обнаружен дубликат: {new_path.name}')
    if tags_match:
        print('      Теги совпадают — одна и та же песня.')

    if old_score >= new_score:
        print(f'      Оставляю: {old_path.name} ({old_info})')
        print(f'      Удаляю:   {new_path.name} ({existing_info})')

        if ctx.dry_run:
            ctx.log_debug('[DRY RUN] Удалил бы дубликат')
            repo.simulate_unlink(new_path)
            return 'skipped', None

        if not safe_unlink(new_path):
            return 'skipped', None

        old_path.rename(new_path)
        repo.simulate_unlink(new_path)
        repo.simulate_rename(old_path, new_path)
        return 'renamed', new_path
    else:
        print(f'      Оставляю: {new_path.name} ({existing_info})')
        print(f'      Удаляю:   {old_path.name} ({old_info})')

        if ctx.dry_run:
            ctx.log_debug('[DRY RUN] Удалил бы старый файл')
            repo.simulate_unlink(old_path)
            return 'skipped', None

        if not safe_unlink(old_path):
            return 'skipped', None
        repo.simulate_unlink(old_path)
        return 'kept_existing', new_path


def handle_rename_result(
    status: str,
    old_path: Path,
    result_path: Path | None,
    ctx: SessionContext,
    stat_key: str,
) -> Path | None:
    """Обрабатывает результат safe_rename и обновляет статистику."""
    if status == 'renamed':
        if result_path:
            ctx.log_debug(f'Переименован: {old_path.name} -> {result_path.name}')

        ctx.increment_stat(stat_key)
        return result_path
    if status == 'kept_existing':
        if result_path:
            ctx.log_debug(
                f'Текущий файл удалён как дубликат: {old_path.name} '
                f'(файл {result_path.name} оставлен без изменений)'
            )
        ctx.increment_stat(stat_key)
        return result_path
    ctx.log_debug(f'Пропущен: {old_path.name}')
    return old_path


def select_indices(items: list[str], description: str) -> list[int]:
    """Показывает нумерованный список и возвращает индексы выбранных."""
    print(f'\n{description}:')
    for i, item in enumerate(items, 1):
        print(f'  [✓] {i}. {item}')

    prompt = '(Enter - применить ко всем / номера - ИСКЛЮЧИТЬ из обработки / n - отменить все): '
    choice = input(prompt).strip().lower()
    if not choice or choice in ('y', 'yes', 'д', 'да'):
        return list(range(len(items)))
    if choice in ('n', 'no', 'н', 'нет'):
        return []
    try:
        excluded = {int(p) for p in re.split(r'[,\s]+', choice) if p}
        selected = [i for i in range(len(items)) if (i + 1) not in excluded]
        skipped = len(items) - len(selected)
        if skipped:
            print(f'Исключено {skipped} файлов.')
        return selected
    except ValueError:
        print('Некорректный ввод, применяю ко всем.')
        return list(range(len(items)))


def find_duplicates(files: list[Path], repo: TagRepository) -> list[list[Path]]:
    """Находит дубликаты файлов по тегам и нормализованным именам."""
    tag_map: dict[tuple[str, str, str], list[Path]] = {}
    for f in files:
        tags = repo.get_tags(f)
        normalized_name = normalize_filename_stem(f.stem).lower()

        if tags:
            artist_norm = normalize_unicode(tags.artist).lower().strip()
            title_norm = normalize_unicode(tags.title).lower().strip()
            key = (artist_norm, title_norm, 'tags')
        else:
            name_norm = normalize_unicode(normalized_name)
            key = (name_norm, '', 'name')

        if key not in tag_map:
            tag_map[key] = []
        tag_map[key].append(f)

    return [group for group in tag_map.values() if len(group) > 1]


def find_artist_variations(
    files: list[Path], repo: TagRepository
) -> dict[str, list[tuple[Path, str, str]]]:
    """Находит разные варианты написания одного исполнителя."""
    artist_map: dict[str, list[tuple[Path, str, str]]] = {}
    for f in files:
        tags = repo.get_tags(f)
        if tags:
            full_artist = tags.artist.strip()
            individual_artists = [a.strip() for a in re.split(r'\s*[,;]\s*', full_artist)]

            for artist in individual_artists:
                if not artist:
                    continue
                key = artist.lower()
                if key not in artist_map:
                    artist_map[key] = []
                if not any(files_are_same(existing_f, f) for existing_f, _, _ in artist_map[key]):
                    artist_map[key].append((f, full_artist, artist))

    variations: dict[str, list[tuple[Path, str, str]]] = {}
    for key, entries in artist_map.items():
        unique_artists = set(individual_artist for _, _, individual_artist in entries)
        if len(unique_artists) > 1:
            variations[key] = entries
    return variations


def process_rename_batch(
    files: list[Path],
    candidates: list[Path],
    transform: Callable[[str], str],
    description: str,
    ctx: SessionContext,
    repo: TagRepository,
    stat_key: str,
) -> list[Path]:
    """Универсальная обработка пакета переименований."""
    if not candidates:
        return files
    labels = [f'{f.name}  ->  {transform(f.stem)}{f.suffix}' for f in candidates]
    idx = select_indices(labels, f'Найдено {len(candidates)} {description}')
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

    if ctx.stats.get(stat_key, 0) > 0:
        print(f'  Обработано файлов: {ctx.stats[stat_key]}')

    return new_files


def process_tag_junk_cleaning(files: list[Path], ctx: SessionContext, repo: TagRepository) -> None:
    """Очищает мусор из тегов исполнителя и названия."""
    candidates = []
    for f in files:
        if not f.exists():
            continue
        tags = repo.get_tags(f)
        if not tags:
            continue
        artist, title = tags.artist, tags.title
        new_artist = remove_junk(str(artist))
        new_title = remove_junk(str(title))
        if new_artist != artist or new_title != title:
            candidates.append((f, artist, title, new_artist, new_title))

    if not candidates:
        print('  Файлов с мусором в тегах не найдено.')
        return

    print(f'\nНайдено {len(candidates)} файлов с мусором в тегах:')
    for i, (f, artist, title, new_artist, new_title) in enumerate(candidates, 1):
        print(f'  {i}. {f.name}')
        if new_artist != artist:
            print(f'      Исполнитель: {artist!r} → {new_artist!r}')
        if new_title != title:
            print(f'      Название:    {title!r} → {new_title!r}')

    idx = select_indices(
        [f.name for f, *_ in candidates],
        'Какие файлы очистить?',
    )

    cleaned_count = 0
    for i in idx:
        f, _, _, new_artist, new_title = candidates[i]
        if ctx.dry_run:
            ctx.log_debug(f'[DRY RUN] Очистил бы теги: {f.name}')
            repo.simulate_update_tags(f, new_artist, new_title)
            cleaned_count += 1
        elif repo.update_tags(f, new_artist, new_title):
            print(f'  Теги очищены: {f.name}')
            cleaned_count += 1

    if cleaned_count:
        ctx.stats['tags_cleaned'] = cleaned_count
        print(f'  Очищено тегов: {cleaned_count}')


def process_duplicates(files: list[Path], ctx: SessionContext, repo: TagRepository) -> list[Path]:
    """Обрабатывает группы дубликатов с умным объединением имён.

    Сначала показывает все группы с планом действий,
    потом один раз спрашивает, какие обработать.
    """
    duplicates = find_duplicates(files, repo)
    if not duplicates:
        print('  Дубликатов не найдено.')
        return files

    # Собираем план действий для каждой группы
    plans: list[dict[str, Any]] = []
    for group in duplicates:
        sorted_group = sorted(group, key=lambda p: p.stat().st_size, reverse=True)
        best_name_file = get_best_name_from_group(group)
        keep_file = sorted_group[0]
        files_to_delete = [f for f in sorted_group if f != keep_file]

        # Нужно ли переименование?
        needs_rename = keep_file != best_name_file and not files_are_same(keep_file, best_name_file)

        plans.append(
            {
                'group': sorted_group,
                'keep': keep_file,
                'delete': files_to_delete,
                'best_name_file': best_name_file,
                'needs_rename': needs_rename,
            }
        )

    # Показываем все группы с планом действий
    print(f'\nНайдено {len(plans)} групп дубликатов:')

    for i, plan in enumerate(plans, 1):
        print(f'\n  {i}. Группа из {len(plan["group"])} файлов:')

        for f in plan['group']:
            info = file_info_line(f)
            marker = ' [оставить]' if files_are_same(f, plan['keep']) else ' [удалить]'
            print(f'      {f.name} ({info}){marker}')

        if plan['needs_rename']:
            final_name = plan['best_name_file'].name
            print(f'      Итог:     {plan["keep"].name} → {final_name}')
        else:
            print('      Итог:     без переименования')

    # Один вопрос для всех групп
    group_labels = []
    for plan in plans:
        label = f'{plan["keep"].name}'
        if plan['needs_rename']:
            label += f' → {plan["best_name_file"].name}'
        group_labels.append(label)

    idx = select_indices(
        group_labels,
        '\nКакие группы обработать?',
    )

    if not idx:
        print('  Все группы пропущены.')
        return files

    # Применяем выбор
    processed_count = 0
    for i in idx:
        plan = plans[i]

        if ctx.dry_run:
            ctx.log_debug(f'[DRY RUN] Обработал бы группу: {plan["keep"].name}')
            for f in plan['delete']:
                repo.simulate_unlink(f)
                ctx.stats['duplicates_removed'] += 1

            if plan['needs_rename']:
                new_name = plan['best_name_file'].name
                new_path = plan['keep'].with_name(new_name)
                repo.simulate_rename(plan['keep'], new_path)

            processed_count += 1
            continue

        # Удаляем все файлы, кроме выбранного
        for f in plan['delete']:
            if safe_unlink(f):
                print(f'  Удалён: {f.name}')
                repo.simulate_unlink(f)
                ctx.stats['duplicates_removed'] += 1

        # Переименовываем в лучшее имя, если нужно
        if plan['needs_rename']:
            new_name = plan['best_name_file'].name
            new_path = plan['keep'].with_name(new_name)
            status, result_path = safe_rename(plan['keep'], new_path, ctx, repo)
            if status == 'renamed':
                print(f'  Переименован в лучшее имя: {plan["keep"].name} → {result_path.name}')
            elif status == 'kept_existing':
                print(f'  Оставлен существующий: {result_path.name}')

        processed_count += 1

    print(f'  Обработано групп: {processed_count}')

    return [f for f in files if f.exists()]


def categorize_annotations(annotations: dict[str, list[Path]]) -> dict[str, dict]:
    """Группирует уточнения по категориям."""
    categories: dict[str, dict[str, Any]] = {
        'remaster': {
            'name': 'Ремастеринг',
            'pattern': re.compile(r'(?:remaster|remastered|remix)', re.I),
            'items': {},
        },
        'soundtrack': {
            'name': 'Саундтреки (from/From)',
            'pattern': re.compile(r'^(?:from|From)\s', re.I),
            'items': {},
        },
        'version': {
            'name': 'Версии треков (Edit/Version/Mix)',
            'pattern': re.compile(r'(?:edit|version|mix|single|album|radio)', re.I),
            'items': {},
        },
        # feat исключены на уровне find_tag_annotations
        'other': {
            'name': 'Прочее',
            'pattern': None,
            'items': {},
        },
    }

    for ann, files in annotations.items():
        categorized = False
        for cat_key, cat_data in categories.items():
            if cat_key == 'other':
                continue
            if cat_data['pattern'] and cat_data['pattern'].search(ann):
                cat_data['items'][ann] = files
                categorized = True
                break
        if not categorized:
            categories['other']['items'][ann] = files

    return {k: v for k, v in categories.items() if v['items']}


def collect_invalid_filename_files(
    files: list[Path], repo: TagRepository
) -> dict[str, list[tuple[Path, str, str]]]:
    """Собирает файлы с недопустимыми символами и группирует их."""
    problems: dict[str, list[tuple[Path, str, str]]] = {}

    for f in files:
        if not f.exists():
            continue

        tags = repo.get_tags(f)
        if not tags:
            continue

        artist = remove_junk(str(tags.artist))
        title = remove_junk(str(tags.title))
        raw_name = f'{artist} - {title}'
        raw_name = normalize_title_spacing(raw_name)

        invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
        if not invalid_chars:
            continue

        cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if f'{cleaned}{f.suffix}' == f.name:
            continue

        if '/' in invalid_chars:
            slash_type = categorize_slash_usage(raw_name)
            if slash_type == 'bilingual_title':
                problem_key = 'bilingual_title'
            elif slash_type == 'artist_name':
                problem_key = 'artist_name'
            else:
                problem_key = 'other'
        else:
            chars_str = ''.join(sorted(invalid_chars))
            problem_key = f'other:{chars_str}'

        if problem_key not in problems:
            problems[problem_key] = []

        problems[problem_key].append((f, f.name, raw_name))

    return problems


def choose_strategy_for_group(
    problem_key: str, examples: list[tuple[Path, str, str]], interactor: ConsoleInteractor
) -> tuple[str, dict]:
    """Запрашивает у пользователя стратегию обработки для группы файлов.

    Возвращает: (стратегия, параметры)
    """
    # Определяем стратегию по умолчанию для предпросмотра
    if problem_key == 'bilingual_title':
        default_strategy = 'first_title'
    elif problem_key == 'artist_name':
        default_strategy = 'remove'
    else:
        default_strategy = 'remove'

    # Определяем строку символов для вывода
    if problem_key.startswith('other:'):
        chars_str = problem_key[6:]  # Убираем префикс 'other:'
    else:
        chars_str = '/'

    # Показываем, что будет применено по умолчанию
    print('\n  Будет применено по умолчанию: ', end='')

    if problem_key == 'bilingual_title':
        print('оставить первое название')
    elif problem_key == 'artist_name':
        print("удалить '/'")
    else:
        print(f"удалить '{chars_str}'")

    # Показываем все файлы с итоговыми именами (только один раз!)
    print()
    for i, (f, current_name, proposed_name) in enumerate(examples, 1):
        strategy_obj = StrategyFactory.create(default_strategy, {}, interactor, f.name)
        preview_result = strategy_obj.apply(
            proposed_name, set(INVALID_FILENAME_CHARS.findall(proposed_name))
        )
        print(f'  {i}. {current_name}')
        print(f'     {proposed_name}')
        if preview_result:
            print(f'     → {preview_result}')
        else:
            print(f'     → {proposed_name}')

    # Формируем опции в зависимости от типа
    print(f'\n  Как обработать все {len(examples)} файлов?')

    if problem_key == 'bilingual_title':
        print("  1. Оставить первое название (до '/') [по умолчанию]")
        print("  2. Оставить второе название (после '/')")
        print('  3. Ввести имя вручную для каждого файла')
        print('  4. Пропустить все файлы')

        choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

        try:
            idx = int(choice)
        except ValueError:
            idx = 1

        if idx == 1:
            return ('first_title', {})
        elif idx == 2:
            return ('second_title', {})
        elif idx == 3:
            return ('manual', {})
        else:
            return ('skip', {})

    elif problem_key == 'artist_name':
        print("  1. Удалить '/' из имени [по умолчанию]")
        print('  2. Ввести имя вручную для каждого файла')
        print('  3. Пропустить все файлы')

        choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

        try:
            idx = int(choice)
        except ValueError:
            idx = 1

        if idx == 1:
            return ('remove', {})
        elif idx == 2:
            return ('manual', {})
        else:
            return ('skip', {})

    else:
        # Другие символы (? * : и т.д.)
        print(f"  1. Удалить символы '{chars_str}' [по умолчанию]")
        print(f"  2. Заменить '{chars_str}' на '-'")
        print('  3. Ввести имя вручную для каждого файла')
        print('  4. Пропустить все файлы')

        choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

        try:
            idx = int(choice)
        except ValueError:
            idx = 1

        if idx == 1:
            return ('remove', {})
        elif idx == 2:
            return ('replace', {'char': '-'})
        elif idx == 3:
            return ('manual', {})
        else:
            return ('skip', {})


def categorize_slash_usage(raw_name: str) -> str:
    """Определяет тип использования '/' в имени.

    Возвращает:
        'bilingual_title' - '/' в названии (билингвальное название)
        'artist_name' - '/' в имени исполнителя
        'other' - другие недопустимые символы
    """
    # Проверяем, есть ли формат "Artist - Title"
    match = re.match(r'^(.+?)\s+-\s+(.+)$', raw_name)
    if match:
        artist_part = match.group(1)
        title_part = match.group(2)

        # Если '/' в имени исполнителя
        if '/' in artist_part:
            return 'artist_name'
        # Если '/' в названии
        if '/' in title_part:
            return 'bilingual_title'

    # Если нет тире, но есть '/' - скорее всего имя исполнителя
    if '/' in raw_name:
        return 'artist_name'

    return 'other'


def main() -> None:
    """Запускает основной цикл обработки и переименования файлов."""
    ctx = SessionContext(
        dry_run='--dry-run' in sys.argv,
        debug='--debug' in sys.argv,
    )
    repo = TagRepository()
    interactor = ConsoleInteractor()

    print('=== Обработчик аудиофайлов ===')
    if ctx.dry_run:
        print('*** СУХОЙ РЕЖИМ: изменения не будут применены ***')

    target_dir = get_valid_path()
    files = get_audio_files(target_dir)

    total = len(files)
    stats = get_format_stats(files)
    stats_msg = format_stats_message(stats)

    print(f'\nНайдено {total} аудиофайлов ({stats_msg}).')
    if not files:
        return

    # --- Шаг 1: Очистка от мусора ---
    stage(1, 'Очистка от мусора', ctx)
    junk = [f for f in files if JUNK_PATTERN.search(f.stem)]
    files = process_rename_batch(
        files,
        junk,
        remove_junk,
        'файлов с мусором (vksaver)',
        ctx,
        repo,
        'junk_removed',
    )

    print('\n  [Подэтап 1.2] Очистка мусора из тегов')
    ctx.log_debug('Подэтап 1.2: очистка мусора из тегов')
    process_tag_junk_cleaning(files, ctx, repo)

    # --- Шаг 2: Нормализация тире и пробелов ---
    stage(2, 'Нормализация тире и пробелов', ctx)

    def normalize_text(text: str) -> str:
        text = normalize_dash(text)
        text = normalize_filename_stem(text)
        return text

    candidates = [f for f in files if normalize_text(f.stem) != f.stem]
    files = process_rename_batch(
        files,
        candidates,
        normalize_text,
        'файлов с нестандартными тире или лишними пробелами',
        ctx,
        repo,
        'dash_normalized',
    )

    # --- Шаг 3: Переименование по тегам ---
    stage(3, 'Переименование по тегам', ctx)

    # --- Подэтап 3.1: Нормализация (feat. X) ---
    print('\n  [Подэтап 3.1] Нормализация участников (feat./ft./featuring)')
    print('  Приводим все вариации к единому формату: (feat. Guest)')
    ctx.log_debug('Подэтап 3.1: нормализация (feat. X)')
    feat_candidates = []

    for f in files:
        if not f.exists():
            continue
        tags = repo.get_tags(f)
        if not tags:
            continue

        artist, title = tags.artist, tags.title
        if not (FEAT_VARIANTS_PATTERN.search(artist) or FEAT_VARIANTS_PATTERN.search(title)):
            continue

        new_artist, new_title = normalize_feat_pair(str(artist), str(title))
        new_artist = remove_feat_guests_from_artist(new_artist, new_title)

        if new_artist != artist or new_title != title:
            new_filename = build_safe_filename(new_artist, new_title, f.suffix)
            feat_candidates.append((f, artist, title, new_artist, new_title, new_filename))

    if feat_candidates:
        print(f'\nНайдено {len(feat_candidates)} файлов для нормализации (feat.):')
        for i, (f, artist, title, new_artist, new_title, new_filename) in enumerate(
            feat_candidates, 1
        ):
            print(f'\n  {i}. {f.name}')
            print('      БЫЛО:')
            print(f'        Исполнитель: {artist!r}')
            print(f'        Название:    {title!r}')
            print('      СТАНЕТ:')
            print(f'        Исполнитель: {new_artist!r}')
            print(f'        Название:    {new_title!r}')
            if new_filename != f.name:
                print(f'        Файл:        {new_filename}')

        selected_indices = select_indices(
            [f.name for f, *_ in feat_candidates],
            '\nКакие файлы нормализовать?',
        )

        normalized_count = 0
        for i in selected_indices:
            f, _, _, new_artist, new_title, _ = feat_candidates[i]
            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Нормализовал бы (feat.): {f.name}')
                repo.simulate_update_tags(f, new_artist, new_title)
                normalized_count += 1
            elif repo.update_tags(f, new_artist, new_title):
                ctx.log_debug(f'Нормализован (feat.): {f.name}')
                normalized_count += 1

        if normalized_count:
            print(f'  Нормализовано (feat.): {normalized_count}')
    else:
        print('  Файлов для нормализации (feat.) не найдено.')

    # --- Подэтап 3.2: Нормализация разделителей исполнителей ---
    print('\n  [Подэтап 3.2] Нормализация разделителей исполнителей')
    print("  Приводим все разделители к запятой с пробелом: 'Artist1, Artist2'")
    ctx.log_debug('Подэтап 3.2: нормализация разделителей исполнителей')

    sep_candidates = []
    for f in files:
        if not f.exists():
            continue
        tags = repo.get_tags(f)
        if not tags:
            continue
        artist, title = tags.artist, tags.title
        new_artist = normalize_artist_separators(str(artist))
        if new_artist != artist:
            sep_candidates.append((f, artist, title, new_artist))

    if sep_candidates:
        print(f'\nНайдено {len(sep_candidates)} файлов для нормализации разделителей:')
        for i, (f, artist, title, new_artist) in enumerate(sep_candidates, 1):
            print(f'\n  {i}. {f.name}')
            print('      БЫЛО:')
            print(f'        Исполнитель: {artist!r}')
            print('      СТАНЕТ:')
            print(f'        Исполнитель: {new_artist!r}')

        selected_indices = select_indices(
            [f.name for f, *_ in sep_candidates],
            '\nКакие файлы нормализовать?',
        )

        normalized_count = 0
        for i in selected_indices:
            f, _, _, new_artist = sep_candidates[i]
            title = sep_candidates[i][2]
            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Нормализовал бы разделители: {f.name}')
                repo.simulate_update_tags(f, new_artist, title)
                normalized_count += 1
            elif repo.update_tags(f, new_artist, title):
                ctx.log_debug(f'Нормализован (разделители): {f.name}')
                normalized_count += 1

        if normalized_count:
            print(f'  Нормализовано разделителей: {normalized_count}')
    else:
        print('  Файлов для нормализации разделителей не найдено.')

    # --- Подэтап 3.3: Анализ уточнений в скобках ---
    print('\n  [Подэтап 3.3] Анализ уточнений в скобках')
    print('  Находим уточнения (ремастеринг, версии, саундтреки)')
    print('  и предлагаем удалить лишние из тегов.')
    annotations = find_tag_annotations(files, repo)
    if annotations:
        problematic = [ann for ann in annotations if INVALID_FILENAME_CHARS.search(ann)]
        if problematic:
            print(
                f'\n  Внимание: {len(problematic)} уточнений содержат '
                f'недопустимые символы и будут удалены автоматически:'
            )
            for ann in problematic:
                print(f'    - ({ann})')

            removed_count = 0
            for ann in problematic:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = repo.get_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(tags.title, ann)
                        if new_title != tags.title:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Автоудалил бы: ({ann}) из {f.name}')
                                repo.simulate_update_tags(f, tags.artist, new_title)
                                removed_count += 1
                            elif repo.update_tags(f, tags.artist, new_title):
                                ctx.log_debug(f'Автоудалено: ({ann}) из {f.name}')
                                removed_count += 1
            if removed_count:
                print(f'  Автоматически удалено: {removed_count}')
                for ann in problematic:
                    del annotations[ann]

    if annotations:
        categories = categorize_annotations(annotations)

        print(f'\nНайдено {len(annotations)} уникальных уточнений в {len(categories)} категориях:')

        for cat_key, cat_data in categories.items():
            total_files = sum(len(files) for files in cat_data['items'].values())
            print(f'\n  {cat_data["name"]} ({total_files} файлов):')
            for i, (ann, file_list) in enumerate(
                sorted(cat_data['items'].items(), key=lambda x: len(x[1]), reverse=True), 1
            ):
                print(f'    {i}. ({ann}) — {len(file_list)} файлов')

        print('\nКакие категории удалить из тегов?')
        print('  Полные названия: remaster, soundtrack, version, other')
        print('  Сокращения:      r,        s,          v,       o')
        choice = interactor.prompt('  (y - все, n - оставить все) [y]: ', default='y')

        shortcuts = {'r': 'remaster', 's': 'soundtrack', 'v': 'version', 'o': 'other'}

        to_remove = set()
        if choice in ('y', 'yes', 'д', 'да'):
            for cat_data in categories.values():
                to_remove.update(cat_data['items'].keys())
        elif choice not in ('n', 'no', 'н', 'нет', ''):
            selected_cats = [c.strip() for c in choice.split(',')]
            for cat_input in selected_cats:
                cat_key = shortcuts.get(cat_input, cat_input)
                if cat_key in categories:
                    to_remove.update(categories[cat_key]['items'].keys())
                else:
                    print(f'  Неизвестная категория: {cat_input}')

        if to_remove:
            removed_count = 0
            for ann in to_remove:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = repo.get_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(tags.title, ann)
                        if new_title != tags.title:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Удалил бы из тега: ({ann}) из {f.name}')
                                repo.simulate_update_tags(f, tags.artist, new_title)
                                removed_count += 1
                            elif repo.update_tags(f, tags.artist, new_title):
                                ctx.log_debug(f'Удалено из тега: ({ann}) из {f.name}')
                                removed_count += 1
            print(f'  Удалено уточнений из тегов: {removed_count}')

    # --- Подэтап 3.4: Формирование новых имён файлов по тегам ---
    print('\n  [Подэтап 3.4] Формирование новых имён файлов по тегам')
    ctx.log_debug('Подэтап 3.4: формирование имён из тегов')

    print('  Поиск файлов с недопустимыми символами...')
    invalid_files = collect_invalid_filename_files(files, repo)

    strategies = {}
    if invalid_files:
        print(
            f'\n  Найдено {sum(len(v) for v in invalid_files.values())} файлов '
            f'с недопустимыми символами в {len(invalid_files)} группах'
        )

        priority_order = ['bilingual_title', 'artist_name']
        other_keys = [k for k in invalid_files.keys() if k not in priority_order]
        ordered_keys = priority_order + sorted(other_keys)

        for problem_key in ordered_keys:
            if problem_key not in invalid_files:
                continue
            examples = invalid_files[problem_key]
            strategy, params = choose_strategy_for_group(problem_key, examples, interactor)
            strategies[problem_key] = (strategy, params)

    safe_candidates = []
    mismatch_candidates = []
    missing = []

    for idx, f in enumerate(files, 1):
        if not f.exists():
            continue

        file_title = extract_title_from_filename(f.stem)
        tags = repo.get_tags(f)
        ctx.log_debug(f'Анализ: {f.name}, теги: {tags}, извлечённое название: {file_title!r}')

        if tags:
            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            raw_name = f'{artist} - {title}'
            raw_name = normalize_title_spacing(raw_name)
            user_already_chose = False

            invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
            if invalid_chars:
                if '/' in invalid_chars:
                    slash_type = categorize_slash_usage(raw_name)
                    if slash_type == 'bilingual_title':
                        problem_key = 'bilingual_title'
                    elif slash_type == 'artist_name':
                        problem_key = 'artist_name'
                    else:
                        problem_key = 'other'
                else:
                    chars_str = ''.join(sorted(invalid_chars))
                    problem_key = f'other:{chars_str}'

                if problem_key in strategies:
                    strategy, params = strategies[problem_key]

                    if strategy != 'skip':
                        strategy_obj = StrategyFactory.create(strategy, params, interactor, f.name)
                        base_name = strategy_obj.apply(
                            raw_name, set(INVALID_FILENAME_CHARS.findall(raw_name))
                        )
                        if base_name:
                            user_already_chose = True
                        else:
                            ctx.log_debug(f'Пропущен (стратегия вернула None): {f.name}')
                            continue
                    else:
                        ctx.log_debug(f'Пропущен (стратегия skip): {f.name}')
                        continue
                else:
                    ctx.log_debug(f'Пропущен (нет стратегии): {f.name}')
                    continue
            else:
                base_name = raw_name

            new_name = f'{base_name}{f.suffix}'

            if new_name == f.name:
                continue

            if user_already_chose or titles_match(file_title, title):
                safe_candidates.append((f, new_name))
            else:
                mismatch_candidates.append((f, file_title, new_name))
        else:
            missing.append(f)

    if safe_candidates:
        labels = [f'{f.name}  ->  {new_name}' for f, new_name in safe_candidates]
        selected_indices = select_indices(
            labels,
            f'Найдено {len(safe_candidates)} файлов для переименования по тегам (названия совпадают)',
        )

        renamed_count = 0
        duplicates_found = []

        for i in selected_indices:
            old_path, new_name = safe_candidates[i]
            new_path = old_path.with_name(new_name)

            status, result_path = simple_rename(old_path, new_path, ctx, repo)

            if status == 'renamed':
                ctx.log_debug(f'Переименован: {old_path.name} -> {new_path.name}')
                ctx.increment_stat('renamed_by_tags')
                renamed_count += 1
            elif status == 'duplicate_exists':
                duplicates_found.append((old_path, new_path))

        if renamed_count:
            print(f'  Переименовано файлов: {renamed_count}')

        if duplicates_found:
            print(f'\n  При переименовании обнаружено {len(duplicates_found)} дубликатов:')

            for i, (old_path, new_path) in enumerate(duplicates_found, 1):
                old_info = file_info_line(old_path)
                existing_info = file_info_line(new_path)

                old_tags = repo.get_tags(old_path)
                existing_tags = repo.get_tags(new_path)
                tags_match = (
                    old_tags
                    and existing_tags
                    and old_tags.artist.lower().strip() == existing_tags.artist.lower().strip()
                    and old_tags.title.lower().strip() == existing_tags.title.lower().strip()
                )
                tags_note = ' (теги совпадают)' if tags_match else ''

                old_score = (old_path.stat().st_size, name_quality_score(old_path))
                new_score = (new_path.stat().st_size, name_quality_score(new_path))
                best_is_old = old_score >= new_score

                best_marker = ' [лучшее]' if best_is_old else ''
                existing_marker = ' [лучшее]' if not best_is_old else ''

                print(f'\n  {i}. {new_path.name}{tags_note}')
                print(f'      1. {old_path.name} ({old_info}){best_marker}')
                print(f'      2. {new_path.name} ({existing_info}){existing_marker}')

                if best_is_old:
                    print('      → Предлагается: оставить 1 (лучшее), удалить 2')
                else:
                    print('      → Предлагается: оставить 2 (лучшее), удалить 1')

            choice = (
                input(
                    f'\n  Объединить {len(duplicates_found)} дубликатов по лучшему качеству? '
                    f'(y/n) [y]: '
                )
                .strip()
                .lower()
            )

            if choice in ('', 'y', 'yes', 'д', 'да'):
                merged_count = 0
                for old_path, new_path in duplicates_found:
                    status, result_path = safe_rename(old_path, new_path, ctx, repo)
                    handle_rename_result(status, old_path, result_path, ctx, 'renamed_by_tags')
                    if status in ('renamed', 'kept_existing'):
                        merged_count += 1

                print(f'  Объединено дубликатов: {merged_count}')
            else:
                print('  Дубликаты пропущены.')

    if mismatch_candidates:
        active_mismatch = []
        for f, file_title, old_new_name in mismatch_candidates:
            if not f.exists():
                ctx.log_debug(f'Файл {f.name} больше не существует, пропускаю')
                continue

            tags = repo.get_tags(f)
            if not tags:
                continue

            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            current_new_name = build_safe_filename(artist, title, f.suffix)

            if current_new_name == f.name:
                ctx.log_debug(f'Файл {f.name} уже имеет корректное имя, пропускаю')
                continue

            file_title = extract_title_from_filename(f.stem)
            if titles_match(file_title, title):
                ctx.log_debug(
                    f'Файл {f.name} теперь совпадает с тегами, переношу в обычную обработку'
                )
                safe_candidates.append((f, current_new_name))
                continue

            active_mismatch.append((f, file_title, current_new_name))

        mismatch_candidates = active_mismatch

    if mismatch_candidates:
        print('\n  [Подэтап 3.5] Обработка расхождений имён и тегов')
        print(
            f'\n  Найдено {len(mismatch_candidates)} файлов с расхождением названий (файл vs теги):'
        )

        categorized: dict[str, list[Any]] = {
            'A': [],
            'B': [],
            'C': [],
            'D': [],
            'E': [],
        }

        for f, file_title, new_name in mismatch_candidates:
            tags = repo.get_tags(f)
            if not tags:
                continue
            tag_artist, tag_title = tags.artist, tags.title
            category = categorize_mismatch(f, file_title, tag_title, tag_artist)
            categorized[category].append((f, file_title, new_name, tag_artist, tag_title))

        category_info = {
            'A': {
                'name': 'Файлы с номерами треков',
                'hint': '💡 В имени нет исполнителя',
                'recommendation': 'Переименовать по тегам',
                'default_action': 't',
            },
            'B': {
                'name': 'Файлы без тире',
                'hint': '💡 Имя не в формате Artist - Title',
                'recommendation': 'Переименовать по тегам',
                'default_action': 't',
            },
            'C': {
                'name': 'Файлы с feat в имени',
                'hint': '💡 В имени есть "feat" вне скобок',
                'recommendation': 'Переименовать по тегам',
                'default_action': 't',
            },
            'D': {
                'name': 'Проблемы с кодировкой',
                'hint': '💡 Теги содержат кракозябры',
                'recommendation': 'Обновить теги из имён',
                'default_action': 'f',
            },
            'E': {
                'name': 'Спорные случаи',
                'hint': '💡 Имя и теги различаются',
                'recommendation': 'Ручное решение',
                'default_action': None,
            },
        }

        for cat_key in ['A', 'B', 'C', 'D', 'E']:
            if not categorized[cat_key]:
                continue

            info = category_info[cat_key]
            print(f'\n{"=" * 60}')
            print(f'Группа {cat_key}: {info["name"]} - {len(categorized[cat_key])} файлов')
            print(f'{"=" * 60}')
            print(info['hint'])
            print(f'→ Рекомендация: {info["recommendation"]}')
            print()

            for i, (f, file_title, new_name, tag_artist, tag_title) in enumerate(
                categorized[cat_key], 1
            ):
                tag_str = f'{tag_artist} - {tag_title}'
                print(f'  {i}. {f.name}')

                if cat_key == 'D':
                    parsed = parse_artist_title(f.stem)
                    if parsed:
                        cleaned_artist, cleaned_title = clean_extracted_tags(parsed[0], parsed[1])
                        new_tag_str = f'{cleaned_artist} - {cleaned_title}'
                        print('      Файл останется без изменений')
                        print(f'      Теги: {tag_str}')
                        print(f'         → {new_tag_str}')
                    else:
                        new_tag_str = file_title or f.stem
                        print('      Файл останется без изменений')
                        print(f'      Теги: {tag_str}')
                        print(f'         → {new_tag_str}')
                elif cat_key == 'E':
                    print(f'      Файл: {file_title or "(нет)"}')
                    print(f'      Теги: {tag_str}')
                    print(f'      → {new_name}')
                else:
                    print(f'      → {new_name}')

            if cat_key != 'E':
                default = info['default_action']
                action_name = (
                    'переименовать по тегам' if default == 't' else 'обновить теги из имён'
                )

                choice = (
                    input(f"\nПрименить '{action_name}' ко всей группе? (y/n) [y]: ")
                    .strip()
                    .lower()
                )

                if choice in ('', 'y', 'yes', 'д', 'да'):
                    for f, file_title, new_name, tag_artist, tag_title in categorized[cat_key]:
                        if default == 't':
                            new_path = f.with_name(new_name)
                            status, result_path = safe_rename(f, new_path, ctx, repo)
                            handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')
                        else:
                            parsed = parse_artist_title(f.stem)
                            if parsed:
                                cleaned_artist, cleaned_title = clean_extracted_tags(
                                    parsed[0], parsed[1]
                                )
                                if ctx.dry_run:
                                    ctx.log_debug(f'[DRY RUN] Обновил бы теги из имени: {f.name}')
                                    repo.simulate_update_tags(f, cleaned_artist, cleaned_title)
                                    ctx.increment_stat('tags_written')
                                elif repo.update_tags(f, cleaned_artist, cleaned_title):
                                    ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                                    ctx.increment_stat('tags_written')
                else:
                    print(f'  Группа {cat_key} пропущена.')

            else:
                print('\nДля каждого файла выберите действие:')
                print('  t - использовать данные из тега (переименовать файл)')
                print('  f - использовать данные из файла (обновить тег)')
                print('  n - пропустить')

                last_action = 'n'

                for i, (f, file_title, old_new_name, tag_artist, tag_title) in enumerate(
                    categorized[cat_key], 1
                ):
                    print(f'\n  {i}. {f.name}')
                    print(f'      Файл: {file_title or "(нет)"}')
                    print(f'      Теги: {tag_artist} - {tag_title}')

                    raw_name = f'{tag_artist} - {tag_title}'
                    raw_name = normalize_title_spacing(raw_name)
                    cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
                    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                    current_new_name = f'{cleaned}{f.suffix}'

                    if last_action == 't':
                        print(f'      → Новое имя (t): {current_new_name}')
                    elif last_action == 'f':
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            print(f'      → Новые теги (f): {parsed[0]} - {parsed[1]}')
                        else:
                            print(f'      → Новые теги (f): {file_title or f.stem}')
                    else:
                        print(f'      → Если t: {current_new_name}')
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            print(f'      → Если f: {parsed[0]} - {parsed[1]}')

                    choice = input(f'      Действие [t/f/n] ({last_action}): ').strip().lower()

                    if not choice:
                        choice = last_action
                        print(f'      [Использовано предыдущее действие: {choice}]')
                    else:
                        last_action = choice

                    if choice == 't':
                        new_path = f.with_name(current_new_name)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')
                    elif choice == 'f':
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Обновил бы теги из имени: {f.name}')
                                repo.simulate_update_tags(f, parsed[0], parsed[1])
                                ctx.increment_stat('tags_written')
                            elif repo.update_tags(f, parsed[0], parsed[1]):
                                ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                                ctx.increment_stat('tags_written')
                    else:
                        print(f'      Пропущен: {f.name}')

    print('\n' + '=' * 60)
    print('  Этап нормализации тегов и имён завершён.')
    print('  Переход к поиску дубликатов.')
    print('=' * 60)

    # --- Шаг 4: Запись тегов из имени файла ---
    stage(4, 'Запись тегов из имени файла', ctx)
    tags_to_write = []

    for f in files:
        if not f.exists():
            continue
        parsed = parse_artist_title(f.stem)
        if parsed:
            tags = repo.get_tags(f)
            if not tags:
                artist, title = parsed
                artist = remove_junk(artist)
                title = remove_junk(title)
                artist, title = normalize_feat_pair(artist, title)
                artist = remove_feat_guests_from_artist(artist, title)
                title = normalize_title_spacing(title)
                tags_to_write.append((f, artist, title))

    if tags_to_write:
        bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')
        files_with_annotations = []

        for i, (f, artist, title) in enumerate(tags_to_write):
            matches = bracket_pattern.findall(title)
            real_matches = [m for m in matches if not FEAT_VARIANTS_PATTERN.match(m)]
            if real_matches:
                files_with_annotations.append((i, f, artist, title, real_matches))

        if files_with_annotations:
            print(
                f'\n  Внимание: {len(files_with_annotations)} файлов содержат уточнения в скобках:'
            )
            for i, f, artist, title, matches in files_with_annotations:
                print(f'    {f.name}')
                print(f'      Уточнения: {", ".join(f"({m})" for m in matches)}')

            choice = input('\n  Удалить уточнения из тегов и имён? (y/n) [y]: ').strip().lower()

            if choice in ('', 'y', 'yes', 'д', 'да'):
                for i, f, artist, title, matches in files_with_annotations:
                    new_title = title
                    for ann in matches:
                        new_title = remove_annotation_from_text(new_title, ann)
                    tags_to_write[i] = (f, artist, new_title)
                print('  Уточнения будут удалены перед записью.')
            else:
                print('  Уточнения будут сохранены.')

        labels = []
        for c in tags_to_write:
            f, artist, title = c
            new_filename = build_safe_filename(artist, title, f.suffix)

            if new_filename != f.name:
                labels.append(f'{f.name} → {new_filename} (artist: {artist!r}, title: {title!r})')
            else:
                labels.append(f'{f.name} (artist: {artist!r}, title: {title!r})')

        selected_indices = select_indices(
            labels,
            f'Найдено {len(tags_to_write)} файлов с корректным именем, но без тегов. Записать теги?',
        )

        written_count = 0
        renamed_count = 0

        for i in selected_indices:
            f, artist, title = tags_to_write[i]

            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Записал бы теги: {f.name}')
                repo.simulate_update_tags(f, artist, title)
                written_count += 1
            else:
                if repo.update_tags(f, artist, title):
                    ctx.log_debug(f'Записаны теги: {f.name}')
                    ctx.increment_stat('tags_written')
                    written_count += 1
                    new_filename = build_safe_filename(artist, title, f.suffix)

                    if new_filename != f.name:
                        new_path = f.with_name(new_filename)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        if status in ('renamed', 'kept_existing'):
                            renamed_count += 1
                            ctx.increment_stat('renamed_by_tags')

        if written_count:
            print(f'  Записано тегов: {written_count}')
        if renamed_count:
            print(f'  Переименовано файлов: {renamed_count}')

    # --- Шаг 5: Поиск дубликатов ---
    stage(5, 'Поиск и объединение дубликатов', ctx)
    files = get_audio_files(target_dir)
    files = process_duplicates(files, ctx, repo)

    # --- Шаг 6: Нормализация имён исполнителей ---
    stage(6, 'Нормализация имён исполнителей', ctx)
    files = get_audio_files(target_dir)
    ctx.log_debug(f'Пересканировано файлов перед этапом 6: {len(files)}')
    artist_variations = find_artist_variations(files, repo)

    unique_normalized = set()

    if artist_variations:
        print(f'\nНайдено {len(artist_variations)} исполнителей с разными вариантами написания.')
        for key, entries in artist_variations.items():
            artist_counts: dict[str, int] = {}
            for _, _, individual_artist in entries:
                artist_counts[individual_artist] = artist_counts.get(individual_artist, 0) + 1

            sorted_artists = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)

            print(f'\nИсполнитель: {key}')
            print('Варианты написания:')
            for i, (artist, count) in enumerate(sorted_artists, 1):
                print(f'  {i}. {artist} ({count} файлов)')

            choice = interactor.prompt('Выберите номер варианта (n - пропустить) [1]: ')
            if choice in ('n', 'no', 'н', 'нет'):
                continue

            if choice in ('', '1'):
                selected_idx = 0
            else:
                try:
                    selected_idx = int(choice) - 1
                    if not (0 <= selected_idx < len(sorted_artists)):
                        print('Некорректный номер, пропускаю.')
                        continue
                except ValueError:
                    print('Некорректный ввод, выбираю вариант 1.')
                    selected_idx = 0

            canonical = sorted_artists[selected_idx][0]
            ctx.log_debug(f'Обрабатываю исполнителя {key!r}, канонический вариант: {canonical!r}')

            processed_files: set[Path] = set()
            for f, full_artist, individual_artist in entries:
                if not f.exists():
                    ctx.log_debug(f'Файл {f.name} уже не существует, пропускаю')
                    continue
                if f in processed_files:
                    continue

                if individual_artist != canonical:
                    new_full_artist = re.sub(
                        r'\b' + re.escape(individual_artist) + r'\b',
                        canonical,
                        full_artist,
                        flags=re.IGNORECASE,
                    )

                    tags = repo.get_tags(f)
                    if tags:
                        if ctx.dry_run:
                            print(
                                f'  [DRY RUN] Обновил бы тег: {f.name} ({full_artist} -> {new_full_artist})'
                            )
                            repo.simulate_update_tags(f, new_full_artist, tags.title)
                            ctx.increment_stat('artists_normalized')
                            unique_normalized.add(key)
                        elif repo.update_tags(f, new_full_artist, tags.title):
                            ctx.log_debug(
                                f'Обновлено: {f.name} ({full_artist} -> {new_full_artist})'
                            )
                            ctx.increment_stat('artists_normalized')
                            unique_normalized.add(key)

                    parsed = parse_artist_title(f.stem)
                    if parsed and parsed[0] != new_full_artist:
                        raw_name = f'{new_full_artist} - {parsed[1]}'

                        if INVALID_FILENAME_CHARS.search(raw_name):
                            base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                            if base_name == 'SKIP':
                                ctx.log_debug(f'Пропущен из-за недопустимых символов: {f.name}')
                                continue
                        else:
                            base_name = raw_name

                        new_name = f'{base_name}{f.suffix}'
                        new_path = f.with_name(new_name)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')

                    processed_files.add(f)

        ctx.stats['artists_normalized_unique'] = len(unique_normalized)

    # --- Шаг 7: Ручной ввод для файлов без метаданных ---
    stage(7, 'Ручной ввод для файлов без метаданных', ctx)

    files = get_audio_files(target_dir)
    still_missing = []

    actually_missing = [f for f in missing if f.exists() and repo.get_tags(f) is None]
    ctx.log_debug(f'Файлов без тегов после Этапа 6: {len(actually_missing)} (было {len(missing)})')

    if actually_missing:
        labels = [f.name for f in actually_missing]
        selected_indices = select_indices(
            labels,
            f'Найдено {len(actually_missing)} файлов без тегов и формата. Ввести исполнителя вручную?',
        )
        for i in selected_indices:
            f = actually_missing[i]
            if not f.exists():
                continue
            title = extract_title_from_filename(f.stem)
            print(f'\n  Файл: {f.name}')
            print(f'  Предполагаемое название: {title}')
            artist = input('  Введите исполнителя (Enter - пропустить): ').strip()
            if not artist:
                still_missing.append(f)
                continue

            raw_name = f'{artist} - {title}'

            if INVALID_FILENAME_CHARS.search(raw_name):
                base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                if base_name == 'SKIP':
                    print(f'  Оставлен без изменений: {f.name}')
                    continue
            else:
                base_name = raw_name

            new_name = f'{base_name}{f.suffix}'
            new_path = f.with_name(new_name)
            status, result_path = safe_rename(f, new_path, ctx, repo)
            handle_rename_result(status, f, result_path, ctx, 'manual_input')

    # --- Финальная статистика ---
    print('\n' + '=' * 60)
    print('=== Статистика обработки ===')
    print('=' * 60)
    print(f'Очистка от мусора (имена): {ctx.stats["junk_removed"]}')
    print(f'Очистка от мусора (теги): {ctx.stats["tags_cleaned"]}')
    print(f'Нормализация тире и пробелов: {ctx.stats["dash_normalized"]}')
    print(f'Удалено дубликатов: {ctx.stats["duplicates_removed"]}')
    print(f'Переименовано по тегам: {ctx.stats["renamed_by_tags"]}')
    print(f'Записано тегов: {ctx.stats["tags_written"]}')
    print(
        f'Нормализовано исполнителей: '
        f'{ctx.stats["artists_normalized"]} файлов '
        f'({ctx.stats.get("artists_normalized_unique", 0)} уникальных)'
    )
    print(f'Ручной ввод: {ctx.stats["manual_input"]}')

    if still_missing:
        print(f'\nВнимание! {len(still_missing)} файлов осталось без метаданных:')
        for f in still_missing:
            print(f'  - {f.name}')
        print('Рекомендация: AcoustID / MusicBrainz или вручную.')

    print('\nОбработка завершена.')


if __name__ == '__main__':
    main()
