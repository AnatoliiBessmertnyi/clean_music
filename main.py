"""Скрипт для очистки и переименования аудиофайлов в указанной директории."""

import sys
import re
from pathlib import Path
from collections import Counter
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.asf import ASF
from mutagen.id3 import ID3NoHeaderError, TIT2, TPE1

# Сухой режим: если True, скрипт только показывает, что сделает,
# но не переименовывает и не удаляет файлы.
DRY_RUN = '--dry-run' in sys.argv
DEBUG = '--debug' in sys.argv

JUNK_PATTERN = re.compile(
    r'\s*[\(\[][^)\]]*vksaver[^)\]]*[\)\]]', re.IGNORECASE
)
NAME_PATTERN = re.compile(r'^(.+?)\s+[-–—]\s+(.+)$')
TRACK_NUMBER_PATTERN = re.compile(
    r'^(track\s*)?\d{1,3}\.?\s*$', re.IGNORECASE
)
LEADING_TRACK_PATTERN = re.compile(
    r'^\d{1,3}\.?\s*[-–—]?\s*(.+)$'
)
SUPPORTED_EXTENSIONS = {'.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.wma'}


def dbg(msg: str) -> None:
    """Выводит отладочное сообщение, если включён DEBUG."""
    if DEBUG:
        print(f"  [DEBUG] {msg}")


def get_valid_path() -> Path:
    """Запрашивает у пользователя путь и проверяет его корректность."""
    while True:
        raw = input("Введите путь к папке: ").strip().strip('"')
        path_obj = Path(raw)
        if path_obj.is_dir():
            return path_obj
        print("Ошибка: путь не является папкой. Попробуйте снова.")


def get_audio_files(directory: Path) -> list[Path]:
    """Возвращает список всех аудиофайлов в заданной директории."""
    return [
        f for f in directory.iterdir()
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def get_format_stats(files: list[Path]) -> dict[str, int]:
    """Подсчитывает количество файлов по форматам."""
    counter = Counter(f.suffix.lower() for f in files)
    return dict(counter)


def format_stats_message(stats: dict[str, int]) -> str:
    """Форматирует статистику по форматам для вывода."""
    parts = []
    for ext, count in sorted(stats.items(), key=lambda x: -x[1]):
        ext_upper = ext.upper().lstrip('.')
        parts.append(f"{count} {ext_upper}")
    return ", ".join(parts)


def format_file_size(size_bytes: int) -> str:
    """Форматирует размер файла в читаемый вид (KB, MB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def get_audio_duration(filepath: Path) -> float | None:
    """Возвращает длительность аудиофайла в секундах или None."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            return MP3(filepath).info.length
        if ext in ('.m4a', '.mp4'):
            return MP4(filepath).info.length
        if ext == '.flac':
            return FLAC(filepath).info.length
        if ext == '.ogg':
            return OggVorbis(filepath).info.length
        if ext == '.wma':
            return ASF(filepath).info.length
    except Exception:
        pass
    return None


def format_duration(seconds: float) -> str:
    """Форматирует длительность в mm:ss."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"


def file_info_line(filepath: Path) -> str:
    """Формирует строку с размером и длительностью файла."""
    size = format_file_size(filepath.stat().st_size)
    dur = get_audio_duration(filepath)
    dur_str = f", {format_duration(dur)}" if dur else ""
    return f"{size}{dur_str}"


def normalize_filename_stem(stem: str) -> str:
    """Нормализует название файла, удаляя лишние пробелы."""
    return re.sub(r'\s+', ' ', stem).strip()


def remove_junk(text: str) -> str:
    """Удаляет мусорные вставки вида (vksaver) из строки."""
    return JUNK_PATTERN.sub('', text).strip()


def normalize_dash(text: str) -> str:
    """Заменяет все виды тире на стандартный дефис."""
    return re.sub(r'[–—]', '-', text)


def is_track_number(text: str) -> bool:
    """Проверяет, является ли строка номером трека (01, 1., Track 5)."""
    return bool(TRACK_NUMBER_PATTERN.match(text.strip()))


def parse_artist_title(filename: str) -> tuple[str, str] | None:
    """Извлекает исполнителя и название из формата 'Artist - Title'."""
    match = NAME_PATTERN.match(filename)
    if match:
        artist = match.group(1).strip()
        title = match.group(2).strip()
        if is_track_number(artist):
            return None
        return artist, title
    return None


def extract_title_from_filename(filename: str) -> str:
    """Извлекает название из имени файла, отбрасывая номер трека."""
    match = LEADING_TRACK_PATTERN.match(filename)
    if match:
        return match.group(1).strip()
    return filename


def read_audio_tags(filepath: Path) -> tuple[str, str] | None:
    """Читает теги из аудиофайла и возвращает (Исполнитель, Название)."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            audio = MP3(filepath)
            artist = audio.tags.get('TPE1')
            title = audio.tags.get('TIT2')
            if artist and title:
                return str(artist), str(title)
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
            artist = audio.tags.get('©ART')
            title = audio.tags.get('©nam')
            if artist and title:
                return artist[0], title[0]
        elif ext == '.flac':
            audio = FLAC(filepath)
            artist = audio.tags.get('artist')
            title = audio.tags.get('title')
            if artist and title:
                return artist[0], title[0]
        elif ext == '.ogg':
            audio = OggVorbis(filepath)
            artist = audio.tags.get('artist')
            title = audio.tags.get('title')
            if artist and title:
                return artist[0], title[0]
        elif ext == '.wma':
            audio = ASF(filepath)
            artist = audio.tags.get('Author')
            title = audio.tags.get('Title')
            if artist and title:
                return artist[0], title[0]
    except Exception:
        pass
    return None


def write_audio_tags(filepath: Path, artist: str, title: str) -> bool:
    """Записывает теги исполнителя и названия в аудиофайл."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            try:
                audio = MP3(filepath)
            except ID3NoHeaderError:
                audio = MP3(filepath, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            # Заменяем теги вместо добавления (иначе будут дубликаты)
            audio.tags['TPE1'] = TPE1(encoding=3, text=artist)
            audio.tags['TIT2'] = TIT2(encoding=3, text=title)
            audio.save()
            return True
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
            audio.tags['©ART'] = [artist]
            audio.tags['©nam'] = [title]
            audio.save()
            return True
        elif ext == '.flac':
            audio = FLAC(filepath)
            audio['artist'] = [artist]
            audio['title'] = [title]
            audio.save()
            return True
        elif ext == '.ogg':
            audio = OggVorbis(filepath)
            audio['artist'] = [artist]
            audio['title'] = [title]
            audio.save()
            return True
        elif ext == '.wma':
            audio = ASF(filepath)
            audio['Author'] = [artist]
            audio['Title'] = [title]
            audio.save()
            return True
    except Exception as e:
        print(f"  Ошибка записи тегов: {e}")
    return False


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


def rename_case_sensitive(old_path: Path, new_path: Path) -> bool:
    """Переименовывает файл с изменением только регистра (Windows)."""
    temp_name = new_path.stem + "__temp__" + new_path.suffix
    temp_path = old_path.with_name(temp_name)
    try:
        old_path.rename(temp_path)
        temp_path.rename(new_path)
        return True
    except OSError as e:
        dbg(f"Ошибка при case-sensitive rename: {e}")
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
        print(f"  Ошибка: файл заблокирован, не могу удалить: {filepath.name}")
        return False
    except OSError as e:
        print(f"  Ошибка при удалении {filepath.name}: {e}")
        return False


def safe_rename(old_path: Path, new_path: Path) -> tuple[str, Path | None]:
    """Безопасно переименовывает файл, обрабатывая дубликаты."""
    dbg(f"rename: {old_path.name!r} -> {new_path.name!r}")
    
    if not old_path.exists():
        print(f"  Файл не найден: {old_path.name}")
        return 'skipped', None
    
    # КЛЮЧЕВАЯ ПРОВЕРКА: один ли это файл на диске?
    if files_are_same(old_path, new_path):
        dbg("Это один и тот же файл (samefile), меняем только регистр")
        if DRY_RUN:
            dbg("[DRY RUN] Пропускаю смену регистра")
            return 'renamed', new_path
        if rename_case_sensitive(old_path, new_path):
            dbg(f"Регистр успешно изменён: {new_path.name}")
            return 'renamed', new_path
        dbg("Не удалось изменить регистр, пропускаю")
        return 'skipped', None
    
    if not new_path.exists():
        if DRY_RUN:
            dbg(f"[DRY RUN] Переименовал бы: {old_path.name} -> {new_path.name}")
            return 'renamed', new_path
        old_path.rename(new_path)
        return 'renamed', new_path

    # Реальный дубликат — другой файл на диске
    old_tags = read_audio_tags(old_path)
    existing_tags = read_audio_tags(new_path)
    
    old_tags_str = (
        f"{old_tags[0]} - {old_tags[1]}" if old_tags else "(нет тегов)"
    )
    existing_tags_str = (
        f"{existing_tags[0]} - {existing_tags[1]}"
        if existing_tags else "(нет тегов)"
    )
    
    tags_match = (
        old_tags and existing_tags
        and old_tags[0].lower().strip() == existing_tags[0].lower().strip()
        and old_tags[1].lower().strip() == existing_tags[1].lower().strip()
    )
    
    print(f"\n  Обнаружен дубликат на диске: {new_path.name}")
    if tags_match:
        print("  Теги обеих композиций совпадают — это одна и та же песня.")
    
    old_info = file_info_line(old_path)
    existing_info = file_info_line(new_path)
    
    print(f"  1. {old_path.name} ({old_info}) [переименовываемый]")
    print(f"     Теги: {old_tags_str}")
    print(f"  2. {new_path.name} ({existing_info}) [уже есть на диске]")
    print(f"     Теги: {existing_tags_str}")

    if DRY_RUN:
        print("  [DRY RUN] Пропускаю выбор дубликата")
        return 'skipped', None

    choice = input(
        "Какой файл оставить? (1/2/n - пропустить) [1]: "
    ).strip().lower()

    if choice in ('n', 'no', 'н', 'нет'):
        return 'skipped', None
    if choice in ('', '1'):
        if not safe_unlink(new_path):
            return 'skipped', None
        old_path.rename(new_path)
        return 'renamed', new_path
    if choice == '2':
        if not safe_unlink(old_path):
            return 'skipped', None
        return 'kept_existing', new_path
    
    print("  Некорректный ввод, выбираю вариант 1.")
    if not safe_unlink(new_path):
        return 'skipped', None
    old_path.rename(new_path)
    return 'renamed', new_path


def handle_rename_result(
    status: str,
    old_path: Path,
    result_path: Path | None,
    stats: dict[str, int],
    stat_key: str,
) -> Path | None:
    """Обрабатывает результат safe_rename и обновляет статистику."""
    if status == 'renamed':
        print(f"  Переименован: {old_path.name} -> {result_path.name}")
        stats[stat_key] += 1
        return result_path
    if status == 'kept_existing':
        print(
            f"  Текущий файл удалён как дубликат: {old_path.name} "
            f"(файл {result_path.name} уже существовал на диске "
            f"и оставлен без изменений)"
        )
        stats[stat_key] += 1
        return result_path
    print(f"  Пропущен: {old_path.name}")
    return old_path


def select_indices(items: list[str], description: str) -> list[int]:
    """Показывает нумерованный список и возвращает индексы выбранных."""
    print(f"\n{description}:")
    for i, item in enumerate(items, 1):
        print(f"  [✓] {i}. {item}")
    prompt = "(y - все / n - отмена / номера для исключения): "
    choice = input(prompt).strip().lower()
    if not choice or choice in ('y', 'yes', 'д', 'да'):
        return list(range(len(items)))
    if choice in ('n', 'no', 'н', 'нет'):
        return []
    try:
        excluded = {
            int(p) for p in re.split(r'[,\s]+', choice) if p
        }
        selected = [
            i for i in range(len(items))
            if (i + 1) not in excluded
        ]
        skipped = len(items) - len(selected)
        if skipped:
            print(f"Исключено {skipped} файлов.")
        return selected
    except ValueError:
        print("Некорректный ввод, применяю ко всем.")
        return list(range(len(items)))


def find_duplicates(files: list[Path]) -> list[list[Path]]:
    """Находит дубликаты файлов по тегам и нормализованным именам."""
    tag_map = {}
    for f in files:
        tags = read_audio_tags(f)
        normalized_name = normalize_filename_stem(f.stem).lower()

        if tags:
            key = (
                tags[0].lower().strip(),
                tags[1].lower().strip(),
                'tags',
            )
        else:
            key = (normalized_name, '', 'name')

        if key not in tag_map:
            tag_map[key] = []
        tag_map[key].append(f)

    return [group for group in tag_map.values() if len(group) > 1]


def find_artist_variations(
    files: list[Path],
) -> dict[str, list[tuple[Path, str]]]:
    """Находит разные варианты написания одного исполнителя."""
    artist_map = {}
    for f in files:
        tags = read_audio_tags(f)
        if tags:
            artist = tags[0].strip()
            key = artist.lower()
            if key not in artist_map:
                artist_map[key] = []
            artist_map[key].append((f, artist))
    
    variations = {}
    for key, entries in artist_map.items():
        unique_artists = set(artist for _, artist in entries)
        if len(unique_artists) > 1:
            variations[key] = entries
    return variations


def process_rename_batch(
    files: list[Path],
    candidates: list[Path],
    transform,
    description: str,
    stats: dict[str, int],
    stat_key: str,
) -> list[Path]:
    """Универсальная обработка пакета переименований."""
    if not candidates:
        return files
    labels = [
        f"{f.name}  ->  {transform(f.stem)}{f.suffix}"
        for f in candidates
    ]
    idx = select_indices(labels, f"Найдено {len(candidates)} {description}")
    chosen = {candidates[i] for i in idx}
    new_files = []
    for f in files:
        if f in chosen:
            new_path = f.with_name(transform(f.stem) + f.suffix)
            status, result_path = safe_rename(f, new_path)
            result = handle_rename_result(
                status, f, result_path, stats, stat_key
            )
            new_files.append(result)
        else:
            new_files.append(f)
    return new_files


def main() -> None:
    """Запускает основной цикл обработки и переименования файлов."""
    print("=== Обработчик аудиофайлов ===")
    if DRY_RUN:
        print("*** СУХОЙ РЕЖИМ: изменения не будут применены ***")
    
    target_dir = get_valid_path()
    files = get_audio_files(target_dir)

    total = len(files)
    stats = get_format_stats(files)
    stats_msg = format_stats_message(stats)

    print(f"\nНайдено {total} аудиофайлов ({stats_msg}).")
    if not files:
        return

    processed_stats = {
        'junk_removed': 0,
        'dash_normalized': 0,
        'spaces_normalized': 0,
        'duplicates_removed': 0,
        'renamed_by_tags': 0,
        'tags_written': 0,
        'artists_normalized': 0,
        'manual_input': 0,
    }

    # --- Шаг 1: Очистка от мусора ---
    junk = [f for f in files if JUNK_PATTERN.search(f.stem)]
    files = process_rename_batch(
        files, junk, remove_junk,
        "файлов с мусором (vksaver)",
        processed_stats, 'junk_removed',
    )

    # --- Шаг 2: Нормализация тире ---
    dash = [f for f in files if '–' in f.stem or '—' in f.stem]
    files = process_rename_batch(
        files, dash, normalize_dash,
        "файлов с нестандартными тире",
        processed_stats, 'dash_normalized',
    )

    # --- Шаг 2.5: Нормализация пробелов ---
    spaces = [
        f for f in files
        if normalize_filename_stem(f.stem) != f.stem
    ]
    files = process_rename_batch(
        files, spaces, normalize_filename_stem,
        "файлов с лишними пробелами",
        processed_stats, 'spaces_normalized',
    )

    # --- Шаг 3: Поиск дубликатов ---
    duplicates = find_duplicates(files)
    if duplicates:
        print(f"\nНайдено {len(duplicates)} групп дубликатов.")
        for group_idx, group in enumerate(duplicates, 1):
            print(f"\n--- Группа {group_idx} ---")
            sorted_group = sorted(
                group, key=lambda p: p.stat().st_size, reverse=True
            )
            print("Какой файл оставить? (остальные будут удалены):")
            for i, f in enumerate(sorted_group, 1):
                info = file_info_line(f)
                print(f"  {i}. {f.name} ({info})")
            
            if DRY_RUN:
                print("  [DRY RUN] Пропускаю выбор дубликата")
                continue
            
            choice = input(
                "Введите номер файла для сохранения "
                "(n - пропустить) [1]: "
            ).strip().lower()
            if choice in ('n', 'no', 'н', 'нет'):
                continue
            if choice in ('', '1'):
                keep_idx = 0
            elif choice == '2':
                keep_idx = 1
            else:
                try:
                    keep_idx = int(choice) - 1
                    if not (0 <= keep_idx < len(sorted_group)):
                        print("Некорректный номер, пропускаю группу.")
                        continue
                except ValueError:
                    print("Некорректный ввод, выбираю вариант 1.")
                    keep_idx = 0
            for i, f in enumerate(sorted_group):
                if i != keep_idx:
                    if safe_unlink(f):
                        print(f"  Удалён: {f.name}")
                        processed_stats['duplicates_removed'] += 1
            files = [f for f in files if f.exists()]

    # --- Шаг 4: Переименование по тегам с проверкой ---
    safe_candidates = []
    mismatch_candidates = []
    missing = []

    for f in files:
        if not f.exists():
            continue
        if parse_artist_title(f.stem):
            continue

        file_title = extract_title_from_filename(f.stem)
        tags = read_audio_tags(f)

        if tags:
            artist = remove_junk(str(tags[0]))
            title = remove_junk(str(tags[1]))
            new_name = f"{artist} - {title}{f.suffix}"

            if new_name == f.name:
                continue

            if titles_match(file_title, title):
                safe_candidates.append((f, new_name))
            else:
                mismatch_candidates.append((f, file_title, new_name))
        else:
            missing.append(f)

    if safe_candidates:
        labels = [f"{c[0].name}  ->  {c[1]}" for c in safe_candidates]
        idx = select_indices(
            labels,
            f"Найдено {len(safe_candidates)} файлов "
            f"для переименования по тегам (названия совпадают)",
        )
        for i in idx:
            old_path, new_name = safe_candidates[i]
            new_path = old_path.with_name(new_name)
            status, result_path = safe_rename(old_path, new_path)
            handle_rename_result(
                status, old_path, result_path,
                processed_stats, 'renamed_by_tags',
            )

    if mismatch_candidates:
        labels = [
            f"{c[0].name}\n      Файл: {c[1] or '(нет)'}\n"
            f"      Теги: {c[2]}"
            for c in mismatch_candidates
        ]
        idx = select_indices(
            labels,
            f"Найдено {len(mismatch_candidates)} файлов "
            f"с расхождением названий (файл vs теги)",
        )
        for i in idx:
            old_path, file_title, new_name = mismatch_candidates[i]
            new_path = old_path.with_name(new_name)
            status, result_path = safe_rename(old_path, new_path)
            handle_rename_result(
                status, old_path, result_path,
                processed_stats, 'renamed_by_tags',
            )

    # --- Шаг 5: Запись тегов из имени файла ---
    tags_to_write = []
    for f in files:
        if not f.exists():
            continue
        parsed = parse_artist_title(f.stem)
        if parsed:
            tags = read_audio_tags(f)
            if not tags:
                tags_to_write.append((f, parsed[0], parsed[1]))

    if tags_to_write:
        labels = [
            f"{c[0].name} (artist: {c[1]}, title: {c[2]})"
            for c in tags_to_write
        ]
        idx = select_indices(
            labels,
            f"Найдено {len(tags_to_write)} файлов с корректным именем, "
            f"но без тегов. Записать теги?",
        )
        for i in idx:
            f, artist, title = tags_to_write[i]
            if DRY_RUN:
                print(f"  [DRY RUN] Записал бы теги: {f.name}")
                continue
            if write_audio_tags(f, artist, title):
                print(f"  Записаны теги: {f.name}")
                processed_stats['tags_written'] += 1

    # --- Шаг 5.5: Нормализация имён исполнителей ---
    artist_variations = find_artist_variations(files)
    if artist_variations:
        print(
            f"\nНайдено {len(artist_variations)} исполнителей "
            f"с разными вариантами написания."
        )
        for key, entries in artist_variations.items():
            artist_counts = {}
            for _, artist in entries:
                artist_counts[artist] = artist_counts.get(artist, 0) + 1
            
            sorted_artists = sorted(
                artist_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )
            
            print(f"\nИсполнитель: {key}")
            print("Варианты написания:")
            for i, (artist, count) in enumerate(sorted_artists, 1):
                print(f"  {i}. {artist} ({count} файлов)")
            
            choice = input(
                "Выберите номер варианта (n - пропустить) [1]: "
            ).strip().lower()
            
            if choice in ('n', 'no', 'н', 'нет'):
                continue
            
            if choice in ('', '1'):
                selected_idx = 0
            else:
                try:
                    selected_idx = int(choice) - 1
                    if not (0 <= selected_idx < len(sorted_artists)):
                        print("Некорректный номер, пропускаю.")
                        continue
                except ValueError:
                    print("Некорректный ввод, выбираю вариант 1.")
                    selected_idx = 0
            
            canonical = sorted_artists[selected_idx][0]
            dbg(f"Обрабатываю исполнителя {key!r}, "
                f"канонический вариант: {canonical!r}")
            
            for f, artist in entries:
                if not f.exists():
                    dbg(f"Файл {f.name} уже не существует, пропускаю")
                    continue
                if artist != canonical:
                    tags = read_audio_tags(f)
                    if tags:
                        if DRY_RUN:
                            print(
                                f"  [DRY RUN] Обновил бы тег: {f.name} "
                                f"({artist} -> {canonical})"
                            )
                        elif write_audio_tags(f, canonical, tags[1]):
                            print(
                                f"  Обновлено: {f.name} "
                                f"({artist} -> {canonical})"
                            )
                            processed_stats['artists_normalized'] += 1
                    
                    parsed = parse_artist_title(f.stem)
                    if parsed and parsed[0] != canonical:
                        new_name = f"{canonical} - {parsed[1]}{f.suffix}"
                        new_path = f.with_name(new_name)
                        status, result_path = safe_rename(f, new_path)
                        handle_rename_result(
                            status, f, result_path,
                            processed_stats, 'renamed_by_tags',
                        )

    # --- Шаг 6: Ручной ввод для файлов без метаданных ---
    still_missing = []
    if missing:
        labels = [f.name for f in missing]
        idx = select_indices(
            labels,
            f"Найдено {len(missing)} файлов без тегов и формата. "
            f"Ввести исполнителя вручную?",
        )
        for i in idx:
            f = missing[i]
            if not f.exists():
                continue
            title = extract_title_from_filename(f.stem)
            print(f"\n  Файл: {f.name}")
            print(f"  Предполагаемое название: {title}")
            artist = input(
                "  Введите исполнителя (Enter - пропустить): "
            ).strip()
            if not artist:
                still_missing.append(f)
                continue
            new_name = f"{artist} - {title}{f.suffix}"
            new_path = f.with_name(new_name)
            status, result_path = safe_rename(f, new_path)
            handle_rename_result(
                status, f, result_path,
                processed_stats, 'manual_input',
            )

    # --- Финальная статистика ---
    print("\n=== Статистика обработки ===")
    print(f"Очистка от мусора: {processed_stats['junk_removed']}")
    print(f"Нормализация тире: {processed_stats['dash_normalized']}")
    print(
        f"Нормализация пробелов: "
        f"{processed_stats['spaces_normalized']}"
    )
    print(
        f"Удалено дубликатов: "
        f"{processed_stats['duplicates_removed']}"
    )
    print(
        f"Переименовано по тегам: "
        f"{processed_stats['renamed_by_tags']}"
    )
    print(f"Записано тегов: {processed_stats['tags_written']}")
    print(
        f"Нормализовано исполнителей: "
        f"{processed_stats['artists_normalized']}"
    )
    print(f"Ручной ввод: {processed_stats['manual_input']}")

    if still_missing:
        print(
            f"\nВнимание! {len(still_missing)} файлов "
            f"осталось без метаданных:"
        )
        for f in still_missing:
            print(f"  - {f.name}")
        print("Рекомендация: AcoustID / MusicBrainz или вручную.")

    print("\nОбработка завершена.")


if __name__ == '__main__':
    main()