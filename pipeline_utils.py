"""Утилиты для оркестрации пайплайна: безопасное переименование и интерактивный выбор."""

import ctypes
import os
import re
import stat
import sys
from pathlib import Path

from config import FEAT_VARIANTS_PATTERN, INVALID_FILENAME_CHARS, LEADING_TRACK_PATTERN
from context import SessionContext
from duplicate_analysis import Candidate, compare_candidates, find_candidate_groups
from file_state import FileIdentity, entry_info, inspect_file
from interactor import UserInteractor, prompt_command
from parsing import parse_artist_title
from report_events import log_operation
from tag_repository import TagRepository


def select_indices(
    items: list[str], description: str, interactor: UserInteractor
) -> list[int]:
    """Показывает нумерованный список и возвращает индексы выбранных элементов."""
    interactor.display(f'\n{description}:')
    for i, item in enumerate(items, 1):
        interactor.display(f'  [✓] {i}. {item}')

    prompt = (
        '(Enter - применить ко всем / номера через пробел или запятую - '
        'ИСКЛЮЧИТЬ из обработки / n - отменить все): '
    )
    choice = prompt_command(interactor, prompt)

    if not choice or choice in ('y', 'yes', 'д', 'да'):
        return list(range(len(items)))
    if choice in ('n', 'no', 'н', 'нет'):
        return []

    try:
        excluded = {int(part) for part in re.split(r'\s*,\s*|\s+', choice)}
    except ValueError:
        interactor.display_error(
            'Введите номера через пробел или запятую. Группа пропущена.'
        )
        return []
    if any(not 1 <= number <= len(items) for number in excluded):
        interactor.display_error(
            f'Допустимые номера: от 1 до {len(items)}. Группа пропущена.'
        )
        return []

    interactor.display(f'Исключено {len(excluded)} файлов.')
    return [i for i in range(len(items)) if i + 1 not in excluded]


def files_are_same(old_path: Path, new_path: Path) -> bool:
    """Проверяет, являются ли два пути одним и тем же файлом на диске."""
    try:
        return old_path.samefile(new_path)
    except (OSError, FileNotFoundError, ValueError):
        return False


WSL_MODIFICATION_DISABLED = (
    'Модификация на монтированных файловых системах WSL временно отключена '
    'из соображений безопасности'
)


def windows_local_ntfs(path: Path) -> bool:
    """Подтверждает локальный NTFS; неизвестные тома не допускаются."""
    if sys.platform != 'win32':
        return False
    volume = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    kernel = ctypes.windll.kernel32
    if not kernel.GetVolumePathNameW(str(path.absolute()), volume, len(volume)):
        return False
    if kernel.GetDriveTypeW(volume.value) != 3:
        return False
    if not kernel.GetVolumeInformationW(
        volume.value, None, 0, None, None, None, filesystem, len(filesystem)
    ):
        return False
    return filesystem.value == 'NTFS'


def modification_allowed(
    path: Path, ctx: SessionContext, interactor: UserInteractor
) -> bool:
    """Проверяет блокировку, платформу, ссылки и родителей без resolve()."""
    reason = ctx.get_block_reason(path)
    if reason is not None:
        interactor.display(f'  Пропущен: {path.name}: {reason}')
        return False
    try:
        if os.name != 'nt':
            raise OSError(WSL_MODIFICATION_DISABLED + ' (POSIX не подтверждён)')
        absolute = path.absolute()
        if ctx.target_dir is not None and absolute.parent != ctx.target_dir.absolute():
            raise OSError('файл находится вне выбранного каталога')
        identity = inspect_file(absolute)
        if not windows_local_ntfs(absolute.parent):
            raise OSError(
                'модификация поддерживается только на локальном NTFS в Windows'
            )
        record = ctx.registry.track(path)
        if record.identity is None:
            raise OSError('исходная физическая идентичность файла неизвестна')
        if identity != record.identity:
            raise OSError('физический объект подменён после сканирования')
    except OSError as exc:
        ctx.block_path(path, str(exc))
        interactor.display_error(f'{path.name}: {exc}')
        return False
    return True


def _matches_rename_entry(path: Path, identity: FileIdentity) -> bool:
    """Проверяет точное написание записи и идентичность исходного файла."""
    return any(
        entry.name == path.name and inspect_file(entry) == identity
        for entry in path.parent.iterdir()
    )


def _observed_rename_path(
    old_path: Path, new_path: Path, identity: FileIdentity
) -> Path | None:
    for candidate in (new_path, old_path):
        try:
            if _matches_rename_entry(candidate, identity):
                return candidate.absolute()
        except OSError:
            continue
    return None


def simple_rename(
    old_path: Path,
    new_path: Path,
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
) -> tuple[str, Path | None]:
    """Переименовывает без замены; конфликт передаёт для отображения."""
    return safe_rename(old_path, new_path, ctx, repo, interactor)


def simulate_rename(
    old_path: Path, new_path: Path, repo: TagRepository, interactor: UserInteractor
) -> None:
    interactor.display(
        f'  [DRY RUN] Переименовал бы: {old_path.name} → {new_path.name}'
    )
    repo.simulate_rename(old_path, new_path)
    log_operation(
        'rename',
        old_path,
        'ПРЕДЛОЖЕНИЕ',
        'выбранное конвейером имя; диск не изменён',
        target=new_path,
    )


def _rename_source_allowed(
    old_path: Path, new_path: Path, ctx: SessionContext, interactor: UserInteractor
) -> bool:
    if modification_allowed(old_path, ctx, interactor):
        return True
    log_operation(
        'rename',
        old_path,
        'блокировка',
        ctx.get_block_reason(old_path) or 'модификация не разрешена',
        target=new_path,
        proposed=ctx.dry_run,
    )
    return False


def safe_rename(
    old_path: Path,
    new_path: Path,
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
) -> tuple[str, Path | None]:
    """Один Windows rename без replace, удаления и временных путей."""
    if not _rename_source_allowed(old_path, new_path, ctx, interactor):
        return 'skipped', None
    if ctx.is_path_blocked(new_path):
        ctx.block_path(old_path, 'целевой путь заблокирован')
        interactor.display_error(f'Целевой путь заблокирован: {new_path}')
        log_operation(
            'rename',
            old_path,
            'блокировка',
            'целевой путь заблокирован',
            target=new_path,
            proposed=ctx.dry_run,
        )
        return 'skipped', None
    if old_path.absolute().parent != new_path.absolute().parent:
        ctx.block_path(old_path, 'переименование между каталогами не поддерживается')
        interactor.display_error('Переименование между каталогами не поддерживается')
        log_operation(
            'rename',
            old_path,
            'отказ',
            'переименование между каталогами не поддерживается',
            target=new_path,
            proposed=ctx.dry_run,
        )
        return 'skipped', None
    if str(old_path.absolute()) == str(new_path.absolute()):
        return 'unchanged', old_path
    try:
        try:
            os.lstat(new_path)
        except FileNotFoundError:
            occupied = False
        else:
            occupied = True
        case_change = old_path.name.casefold() == new_path.name.casefold()
        if case_change and occupied:
            try:
                target_info = entry_info(new_path.absolute())
            except OSError:
                case_change = False
            else:
                case_change = (
                    stat.S_ISREG(target_info.st_mode)
                    and target_info.st_nlink == 1
                    and (target_info.st_dev, target_info.st_ino)
                    == ctx.registry.track(old_path).identity
                )
        if occupied and not case_change:
            ctx.block_path(old_path, 'конфликт целевого имени; оба файла сохранены')
            interactor.display(
                f'  Конфликт имени: {new_path.name}; оба файла сохранены.'
            )
            log_operation(
                'rename',
                old_path,
                'конфликт',
                'конфликт целевого имени; оба файла сохранены',
                target=new_path,
                proposed=ctx.dry_run,
            )
            return 'duplicate_exists', new_path
        if not _rename_source_allowed(old_path, new_path, ctx, interactor):
            return 'skipped', None
        if ctx.dry_run:
            simulate_rename(old_path, new_path, repo, interactor)
            return 'planned', new_path
        identity = ctx.registry.track(old_path).identity
        if identity is None:
            raise OSError('исходная физическая идентичность файла неизвестна')
    except OSError as exc:
        ctx.block_path(old_path, str(exc))
        interactor.display_error(f'{old_path.name}: {exc}')
        log_operation(
            'rename', old_path, 'отказ', str(exc), target=new_path, proposed=ctx.dry_run
        )
        return 'skipped', None

    try:
        # Windows отказывает при появлении цели, в том числе после lstat.
        old_path.rename(new_path)
    except FileExistsError:
        ctx.block_path(old_path, 'целевой путь занят; переименование отклонено')
        interactor.display_error(f'Целевой путь занят: {new_path.name}')
        log_operation(
            'rename',
            old_path,
            'конфликт',
            'цель занята при выполнении rename',
            target=new_path,
        )
        return 'duplicate_exists', new_path
    except OSError as exc:
        ctx.block_path(old_path, str(exc))
        ctx.block_path(new_path, str(exc))
        interactor.display_error(f'Переименование {old_path.name} отклонено: {exc}')
        log_operation('rename', old_path, 'отказ', str(exc), target=new_path)
        return 'skipped', None

    try:
        if not _matches_rename_entry(new_path, identity):
            raise OSError('целевое имя или физическая идентичность не подтверждены')
    except OSError as exc:
        actual = _observed_rename_path(old_path, new_path, identity)
        observed = str(actual) if actual is not None else 'не установлено'
        reason = (
            f'Переименование не подтверждено ({exc}). '
            f'Запрошено: {old_path.absolute()} → {new_path.absolute()}. '
            f'Фактическое местонахождение исходного файла: {observed}'
        )
        ctx.block_path(old_path, reason)
        ctx.block_path(new_path, reason)
        interactor.display_error(reason)
        log_operation('rename', old_path, 'отказ', reason, target=new_path)
        return 'failed', None
    if ctx.report is not None:
        ctx.report.record_rename(old_path, new_path)
    repo.invalidate(old_path)
    repo.invalidate(new_path)
    return 'renamed', new_path


def handle_rename_result(
    status: str,
    old_path: Path,
    result_path: Path | None,
    ctx: SessionContext,
    stat_key: str,
) -> Path | None:
    """Учитывает только подтверждённое переименование; прогноз не успех."""
    if status == 'renamed' and result_path is not None:
        ctx.increment_stat(stat_key)
        return result_path
    if status in ('unchanged', 'planned') and result_path is not None:
        return result_path
    ctx.block_path(old_path, 'переименование не подтверждено')
    return None


def get_audio_duration(filepath: Path) -> float | None:
    """Возвращает длительность аудиофайла в секундах или None."""
    info = TagRepository().read_tags_uncached(filepath).info
    return info.duration if info is not None else None


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

    if FEAT_VARIANTS_PATTERN.search(parsed[0]) or FEAT_VARIANTS_PATTERN.search(
        parsed[1]
    ):
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
            if (
                chars_key in ctx.remembered_choices
                and ctx.remembered_choices[chars_key] == i - 1
            )
            else ''
        )
        interactor.display(f'  {i}. {label}{marker}{remembered}')

    interactor.display(
        f"  a. Запомнить и применить вариант 1 ко всем файлам с '{chars_str}'"
    )

    # Подсказка при повторении
    if chars_key not in ctx.ask_counts:
        ctx.ask_counts[chars_key] = 0
    ctx.ask_counts[chars_key] += 1

    count = ctx.ask_counts[chars_key]
    if count >= 3:
        interactor.display(
            f"  [Подсказка: вы уже {count} раз выбрали этот вариант. Нажмите 'a', чтобы запомнить.]"
        )

    choice = prompt_command(interactor, '  Выберите вариант [1]: ', default='1')

    if choice == 'a':
        ctx.remembered_choices[chars_key] = 0
        interactor.display(
            f"  Запомнено: применять 'Удалить символы' для всех '{chars_str}'"
        )
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
                    interactor.display(
                        '  Имя всё ещё содержит недопустимые символы, оставляю как есть.'
                    )
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


def find_duplicates(files: list[Path], repo: TagRepository) -> list[list[Path]]:
    """Возвращает пути кандидатов для вызывающих мест, которым нужны только группы."""
    return [
        [candidate.path for candidate in group.candidates]
        for group in find_candidate_groups(files, repo)
    ]


def get_best_name_from_group(group: list[Path]) -> Path:
    """Выбирает файл с наиболее правильным именем из группы дубликатов."""
    return max(group, key=name_quality_score)


def _display_candidate(candidate: Candidate, interactor: UserInteractor) -> None:
    metadata = candidate.metadata
    status = metadata.status.name
    if metadata.error:
        status += f' ({metadata.error})'
    interactor.display(f'    {candidate.path} — теги {status}')
    known = ', '.join(
        f'{field}={value!r}'
        for field, value in candidate.values().items()
        if value is not None
    )
    interactor.display(f'      {known}')
    for source, qualifiers in (
        ('имя', candidate.name_qualifiers),
        ('тег названия', candidate.tag_qualifiers),
    ):
        if qualifiers:
            interactor.display(f'      Уточнения ({source}): {", ".join(qualifiers)}')
    if candidate.qualifier_disagreement:
        interactor.display('      Уточнения в имени и теге расходятся.')


def process_duplicates(
    files: list[Path],
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
) -> list[Path]:
    """Показывает основания поиска и результаты сравнения кандидатов."""
    duplicates = find_candidate_groups(files, repo)
    interactor.display(f'  Групп кандидатов в дубликаты: {len(duplicates)}')
    for i, group in enumerate(duplicates, 1):
        interactor.display(f'  Группа {i}: {group.source} {group.key!r}')
        for number, candidate in enumerate(group.candidates, 1):
            interactor.display(f'    Файл {number}')
            _display_candidate(candidate, interactor)
        numbers = {
            candidate.path: number
            for number, candidate in enumerate(group.candidates, 1)
        }
        for pair in compare_candidates(group, ctx.registry):
            left, right = numbers[pair.left.path], numbers[pair.right.path]
            interactor.display(f'    Файлы {left} и {right}: {pair.byte_status}.')
            if pair.byte_error:
                interactor.display(f'      Ошибка чтения: {pair.byte_error}')
            if pair.differences:
                interactor.display(f'      Различия: {", ".join(pair.differences)}')
            if pair.missing:
                interactor.display(
                    f'      Недостаточно данных: {", ".join(pair.missing)}'
                )
    if duplicates:
        interactor.display('  Объединение отключено; все файлы сохранены.')
    return files


def find_artist_variations(
    files: list[Path], repo: TagRepository
) -> dict[str, list[tuple[Path, str, str]]]:
    """Находит разные варианты написания одного исполнителя."""
    artist_map: dict[str, list[tuple[Path, str, str]]] = {}
    for f in files:
        tags = repo.get_tags(f).complete_tags
        if tags:
            full_artist = tags.artist.strip()
            individual_artists = [
                a.strip() for a in re.split(r'\s*[,;]\s*', full_artist)
            ]

            for artist in individual_artists:
                if not artist:
                    continue
                key = artist.lower()
                if key not in artist_map:
                    artist_map[key] = []
                if not any(
                    files_are_same(existing_f, f)
                    for existing_f, _, _ in artist_map[key]
                ):
                    artist_map[key].append((f, full_artist, artist))

    variations: dict[str, list[tuple[Path, str, str]]] = {}
    for key, entries in artist_map.items():
        unique_artists = {individual_artist for _, _, individual_artist in entries}
        if len(unique_artists) > 1:
            variations[key] = entries
    return variations
