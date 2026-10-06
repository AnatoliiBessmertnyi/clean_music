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

DRY_RUN = '--dry-run' in sys.argv
DEBUG = '--debug' in sys.argv

# Список мусорных ключевых слов для очистки из имён файлов и тегов.
# Добавляйте новые паттерны сюда. Точку в доменах экранируйте: 'site\.ru'
JUNK_KEYWORDS = [
    r'vksaver',
    r'muzmo\.ru',
    r'zaycev\.net',
    r'myzuka\.club',
]

# Динамически строим паттерн из списка ключевых слов
_junk_keywords_regex = '|'.join(JUNK_KEYWORDS)
JUNK_PATTERN = re.compile(
    r'\s*[\(\[][^)\]]*(?:' + _junk_keywords_regex + r')[^)\]]*[\)\]]',
    re.IGNORECASE
)
NAME_PATTERN = re.compile(r'^(.+?)\s+[-–—]\s+(.+)$')
TRACK_NUMBER_PATTERN = re.compile(
    r'^(track\s*)?\d{1,3}\.?\s*$', re.IGNORECASE
)
LEADING_TRACK_PATTERN = re.compile(
    r'^(?:track\s*)?\d{1,3}(?:\.\s*|\s*[-–—]+\s*)(.+)$',
    re.IGNORECASE
)
INVALID_FILENAME_CHARS = re.compile(r'[\\:*?"<>|/]')
FEAT_VARIANTS_PATTERN = re.compile(
    r'\b(?:feat(?:uring)?|ft)\b\.?\s*',
    re.IGNORECASE
)
SUPPORTED_EXTENSIONS = {'.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.wma'}

TOTAL_STAGES = 8
_remembered_sanitize_choices: dict[frozenset, int] = {}


def dbg(msg: str) -> None:
    """Выводит отладочное сообщение, если включён DEBUG."""
    if DEBUG:
        print(f"  [DEBUG] {msg}")


def get_choice_with_default(prompt: str, default: str = '1') -> str:
    """Запрашивает выбор у пользователя и помечает выбор по умолчанию."""
    choice = input(prompt).strip().lower()
    if not choice:
        print(f"  [Выбран вариант {default} по умолчанию]")
        return default
    return choice


def has_encoding_issues(text: str) -> bool:
    """Проверяет, содержит ли текст признаки неправильной кодировки."""
    # Типичные артефакты двойной кодировки (кириллица в Latin-1)
    mojibake_pattern = re.compile(r'[À-ÿ]{2,}')
    return bool(mojibake_pattern.search(text))


def sanitize_filename_interactive(
    name: str, current_filename: str
) -> str | None:
    """Интерактивно очищает имя файла от недопустимых символов.
    
    Запоминает выбор пользователя для каждого набора символов.
    """
    invalid_chars = set(INVALID_FILENAME_CHARS.findall(name))
    if not invalid_chars:
        return name
    
    chars_key = frozenset(invalid_chars)
    chars_str = ''.join(sorted(invalid_chars))
    
    # Формируем опции (как раньше)
    options = []
    
    opt1 = name
    for c in invalid_chars:
        opt1 = opt1.replace(c, '')
    opt1 = re.sub(r'\s+', ' ', opt1).strip()
    options.append((f"Удалить символы → {opt1}", opt1))
    
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
    
    options.append(("Ввести имя вручную", "manual"))
    options.append((f"Оставить как есть: {current_filename}", "SKIP"))
    
    # Проверяем, есть ли запомненный выбор
    if chars_key in _remembered_sanitize_choices:
        remembered_idx = _remembered_sanitize_choices[chars_key]
        if 0 <= remembered_idx < len(options):
            _, value = options[remembered_idx]
            dbg(
                f"Применяю запомненный выбор #{remembered_idx+1} "
                f"для символов '{chars_str}'"
            )
            # Для "manual" и "SKIP" не применяем запомненный выбор
            if value not in ("manual", "SKIP"):
                return value
    
    # Выводим меню
    print(f"\n  Внимание! Недопустимые символы: {chars_str}")
    print(f"  Предлагаемое имя: {name}")
    print(f"  Текущий файл:     {current_filename}")
    
    for i, (label, _) in enumerate(options, 1):
        marker = " [по умолчанию]" if i == 1 else ""
        remembered = " [запомнено]" if (
            chars_key in _remembered_sanitize_choices 
            and _remembered_sanitize_choices[chars_key] == i - 1
        ) else ""
        print(f"  {i}. {label}{marker}{remembered}")
    
    print(f"  a. Запомнить и применить вариант 1 ко всем файлам с '{chars_str}'")
    
    # Проверяем, сколько раз уже выбирали этот символ
    if chars_key not in _remembered_sanitize_choices:
        # Считаем, сколько раз уже спрашивали про этот символ
        if not hasattr(sanitize_filename_interactive, '_ask_count'):
            sanitize_filename_interactive._ask_count = {}
        
        if chars_key not in sanitize_filename_interactive._ask_count:
            sanitize_filename_interactive._ask_count[chars_key] = 0
        sanitize_filename_interactive._ask_count[chars_key] += 1
        
        count = sanitize_filename_interactive._ask_count[chars_key]
        if count >= 3:
            print(f"  [Подсказка: вы уже {count} раз выбрали этот вариант. "
                  f"Нажмите 'a', чтобы запомнить.]")
    
    choice = get_choice_with_default(
        "  Выберите вариант [1]: ", default='1'
    )
    
    # Обработка "запомнить для всех"
    if choice == 'a':
        _remembered_sanitize_choices[chars_key] = 0
        print(f"  Запомнено: применять 'Удалить символы' для всех '{chars_str}'")
        _, value = options[0]
        return value
    
    if choice in ('n', 'no', 'н', 'нет'):
        return "SKIP"
    
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            _, value = options[idx]
            if value == "manual":
                custom = input("  Введите имя (без расширения): ").strip()
                if not custom:
                    return "SKIP"
                if INVALID_FILENAME_CHARS.search(custom):
                    print(
                        "  Имя всё ещё содержит недопустимые "
                        "символы, оставляю как есть."
                    )
                    return "SKIP"
                return custom
            return value
    except ValueError:
        pass
    
    return "SKIP"


def stage(number: int, name: str) -> None:
    """Печатает заголовок этапа обработки."""
    print(f"\n{'='*60}")
    print(f"[Этап {number}/{TOTAL_STAGES}] {name}")
    print(f"{'='*60}")
    dbg(f"Начинаю этап {number}/{TOTAL_STAGES}: {name}")


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


def find_tag_annotations(
    files: list[Path],
) -> dict[str, list[Path]]:
    """Находит уникальные скобки в тегах названий (кроме известного мусора)."""
    annotations = {}
    bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')
    
    for f in files:
        if not f.exists():
            continue
        tags = read_audio_tags(f)
        if tags:
            title = tags[1]
            matches = bracket_pattern.findall(title)
            for match in matches:
                match = match.strip()
                if not match:
                    continue
                # Пропускаем известный мусор (уже обработан)
                if JUNK_PATTERN.search(f"({match})"):
                    continue
                if match not in annotations:
                    annotations[match] = []
                if f not in annotations[match]:
                    annotations[match].append(f)
    
    return annotations


def remove_annotation_from_text(text: str, annotation: str) -> str:
    """Удаляет конкретное уточнение в скобках из текста."""
    pattern = re.compile(
        r'\s*[\(\[]\s*' + re.escape(annotation) + r'\s*[\)\]]\s*',
        re.IGNORECASE
    )
    return pattern.sub('', text).strip()


def normalize_dash(text: str) -> str:
    """Заменяет все виды тире на стандартный дефис."""
    return re.sub(r'[–—]', '-', text)


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
            base = base[:match.start()] + base[end:]
        cleaned_artist = re.sub(r'\s+', ' ', base).strip()
    
    # 2. Извлекаем feat-гостей из title
    title_guests = []
    cleaned_title = title
    real_title_part = ""  # часть, которая оказалась названием (после тире в feat)
    
    # Ищем feat в скобках
    bracket_feat_pattern = re.compile(
        r'[\(\[]\s*(?:feat(?:uring)?|ft)\b\.?\s*([^\)\]]+)[\)\]]',
        re.IGNORECASE
    )
    matches = list(bracket_feat_pattern.finditer(title))
    if matches:
        result_parts = []
        last_end = 0
        for match in matches:
            result_parts.append(title[last_end:match.start()])
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
            base_parts.append(cleaned_title[last_end:match.start()])
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
                guest_only = guest_part[:dash_match.start()].strip()
                title_part = guest_part[dash_match.end():].strip()
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
        final_title = f"{cleaned_title} (feat. {guests_str})"
    else:
        final_title = cleaned_title
    
    return cleaned_artist, final_title


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
        print(
            f"  Ошибка: файл заблокирован, "
            f"не могу удалить: {filepath.name}"
        )
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
            dbg(f"[DRY RUN] Переименовал бы: "
                f"{old_path.name} -> {new_path.name}")
            return 'renamed', new_path
        old_path.rename(new_path)
        return 'renamed', new_path

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
        print("  Теги обеих композиций совпадают — одна и та же песня.")
    
    old_info = file_info_line(old_path)
    existing_info = file_info_line(new_path)
    
    print(f"  1. {old_path.name} ({old_info}) [переименовываемый]")
    print(f"     Теги: {old_tags_str}")
    print(f"  2. {new_path.name} ({existing_info}) [уже есть на диске]")
    print(f"     Теги: {existing_tags_str}")

    if DRY_RUN:
        print("  [DRY RUN] Пропускаю выбор дубликата")
        return 'skipped', None

    choice = get_choice_with_default(
        "Какой файл оставить? (1/2/n - пропустить) [1]: "
    )

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

    prompt = (
        "(Enter - применить ко всем / "
        "номера - ИСКЛЮЧИТЬ из обработки / "
        "n - отменить все): "
    )
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


def process_tag_junk_cleaning(
    files: list[Path], stats: dict[str, int]
) -> None:
    """Очищает мусор из тегов исполнителя и названия."""
    candidates = []
    for f in files:
        if not f.exists():
            continue
        tags = read_audio_tags(f)
        if not tags:
            continue
        artist, title = tags
        new_artist = remove_junk(str(artist))
        new_title = remove_junk(str(title))
        if new_artist != artist or new_title != title:
            candidates.append((f, artist, title, new_artist, new_title))
    
    if not candidates:
        dbg("Файлов с мусором в тегах не найдено.")
        return
    
    print(f"\nНайдено {len(candidates)} файлов с мусором в тегах:")
    for i, (f, artist, title, new_artist, new_title) in enumerate(
        candidates, 1
    ):
        print(f"  {i}. {f.name}")
        if new_artist != artist:
            print(f"      Исполнитель: {artist!r} → {new_artist!r}")
        if new_title != title:
            print(f"      Название:    {title!r} → {new_title!r}")
    
    idx = select_indices(
        [f.name for f, *_ in candidates],
        "Какие файлы очистить?",
    )
    
    cleaned_count = 0
    for i in idx:
        f, _, _, new_artist, new_title = candidates[i]
        if DRY_RUN:
            dbg(f"[DRY RUN] Очистил бы теги: {f.name}")
            cleaned_count += 1
        elif write_audio_tags(f, new_artist, new_title):
            print(f"  Теги очищены: {f.name}")
            cleaned_count += 1
    
    if cleaned_count:
        stats['tags_cleaned'] = cleaned_count
        print(f"  Очищено тегов: {cleaned_count}")


def process_duplicates(
    files: list[Path], stats: dict[str, int]
) -> list[Path]:
    """Обрабатывает группы дубликатов с умным объединением имён."""
    duplicates = find_duplicates(files)
    if not duplicates:
        return files
    
    print(f"\nНайдено {len(duplicates)} групп дубликатов.")
    for group_idx, group in enumerate(duplicates, 1):
        print(f"\n--- Группа {group_idx} ---")
        sorted_group = sorted(
            group, key=lambda p: p.stat().st_size, reverse=True
        )
        best_name_file = get_best_name_from_group(group)
        
        print("Какой файл оставить по качеству? "
              "(остальные будут удалены):")
        for i, f in enumerate(sorted_group, 1):
            info = file_info_line(f)
            marker = " [лучшее имя]" if f == best_name_file else ""
            print(f"  {i}. {f.name} ({info}){marker}")
        
        if DRY_RUN:
            print("  [DRY RUN] Пропускаю выбор дубликата")
            continue
        
        choice = get_choice_with_default(
            "Введите номер файла для сохранения "
            "(n - пропустить) [1]: "
        )
        
        if choice in ('n', 'no', 'н', 'нет'):
            continue
        if choice in ('', '1'):
            keep_idx = 0
        else:
            try:
                keep_idx = int(choice) - 1
                if not (0 <= keep_idx < len(sorted_group)):
                    print("Некорректный номер, пропускаю группу.")
                    continue
            except ValueError:
                print("Некорректный ввод, выбираю вариант 1.")
                keep_idx = 0
        
        keep_file = sorted_group[keep_idx]
        
        # Удаляем все файлы, кроме выбранного
        for i, f in enumerate(sorted_group):
            if i != keep_idx:
                if safe_unlink(f):
                    print(f"  Удалён: {f.name}")
                    stats['duplicates_removed'] += 1
        
        # Переименовываем выбранный файл в лучшее имя, если нужно
        if keep_file != best_name_file and best_name_file.exists():
            # Файл с лучшим именем ещё существует — это дубликат
            # Он уже должен был быть удалён выше, но на всякий случай
            dbg("Файл с лучшим именем ещё существует, удаляю")
            safe_unlink(best_name_file)
        
        if keep_file != best_name_file:
            new_name = best_name_file.name
            new_path = keep_file.with_name(new_name)
            dbg(f"Переименовываю в лучшее имя: "
                f"{keep_file.name} -> {new_name}")
            status, result_path = safe_rename(keep_file, new_path)
            if status == 'renamed':
                print(f"  Переименован в лучшее имя: "
                      f"{keep_file.name} -> {result_path.name}")
                stats['duplicates_removed'] += 1
    
    return [f for f in files if f.exists()]


def categorize_annotations(annotations: dict[str, list[Path]]) -> dict[str, dict]:
    """Группирует уточнения по категориям."""
    categories = {
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
        'feat': {
            'name': 'Участники (feat./Feat.)',
            'pattern': re.compile(r'(?:feat\.?|featuring)', re.I),
            'items': {},
        },
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
    
    # Удаляем пустые категории
    return {k: v for k, v in categories.items() if v['items']}


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
        'tags_cleaned': 0,
        'dash_normalized': 0,
        'spaces_normalized': 0,
        'duplicates_removed': 0,
        'renamed_by_tags': 0,
        'tags_written': 0,
        'artists_normalized': 0,
        'manual_input': 0,
    }

    # --- Шаг 1: Очистка от мусора ---
    stage(1, "Очистка от мусора")
    junk = [f for f in files if JUNK_PATTERN.search(f.stem)]
    files = process_rename_batch(
        files, junk, remove_junk,
        "файлов с мусором (vksaver)",
        processed_stats, 'junk_removed',
    )
    
    # Подэтап 1.2: Очистка мусора из тегов
    dbg("Подэтап 1.2: очистка мусора из тегов")
    process_tag_junk_cleaning(files, processed_stats)

    # --- Шаг 2: Нормализация тире ---
    stage(2, "Нормализация тире")
    dash = [f for f in files if '–' in f.stem or '—' in f.stem]
    files = process_rename_batch(
        files, dash, normalize_dash,
        "файлов с нестандартными тире",
        processed_stats, 'dash_normalized',
    )

    # --- Шаг 3: Нормализация пробелов ---
    stage(3, "Нормализация пробелов")
    spaces = [
        f for f in files
        if normalize_filename_stem(f.stem) != f.stem
    ]
    files = process_rename_batch(
        files, spaces, normalize_filename_stem,
        "файлов с лишними пробелами",
        processed_stats, 'spaces_normalized',
    )

    # --- Шаг 4: Поиск дубликатов ---
    stage(4, "Поиск и объединение дубликатов")
    files = process_duplicates(files, processed_stats)

    # --- Шаг 5: Переименование по тегам с проверкой ---
    stage(5, "Переименование по тегам")

    # --- Подэтап 5.1: Нормализация (feat. X) в тегах ---
    dbg("Подэтап 5.1: нормализация (feat. X)")
    feat_candidates = []
    
    for f in files:
        if not f.exists():
            continue
        tags = read_audio_tags(f)
        if not tags:
            continue
        
        artist, title = tags
        # Проверяем, есть ли feat в любом из тегов
        if not (FEAT_VARIANTS_PATTERN.search(artist) or 
                FEAT_VARIANTS_PATTERN.search(title)):
            continue
        
        new_artist, new_title = normalize_feat_pair(
            str(artist), str(title)
        )
        
        if new_artist != artist or new_title != title:
            # Формируем новое имя файла для показа
            raw_name = f"{new_artist} - {new_title}"
            cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            new_filename = f"{cleaned}{f.suffix}"
            
            feat_candidates.append(
                (f, artist, title, new_artist, new_title, new_filename)
            )
    
    if feat_candidates:
        print(f"\nНайдено {len(feat_candidates)} файлов "
              f"для нормализации (feat.):")
        for i, (f, artist, title, new_artist, new_title, 
                new_filename) in enumerate(feat_candidates, 1):
            print(f"\n  {i}. {f.name}")
            print(f"      БЫЛО:")
            print(f"        Исполнитель: {artist!r}")
            print(f"        Название:    {title!r}")
            print(f"      СТАНЕТ:")
            print(f"        Исполнитель: {new_artist!r}")
            print(f"        Название:    {new_title!r}")
            if new_filename != f.name:
                print(f"        Файл:        {new_filename}")
        
        idx = select_indices(
            [f.name for f, *_ in feat_candidates],
            "\nКакие файлы нормализовать?",
        )
        
        normalized_count = 0
        for i in idx:
            f, _, _, new_artist, new_title, _ = feat_candidates[i]
            if DRY_RUN:
                dbg(f"[DRY RUN] Нормализовал бы (feat.): {f.name}")
                normalized_count += 1
            elif write_audio_tags(f, new_artist, new_title):
                print(f"  Нормализован: {f.name}")
                normalized_count += 1
        
        if normalized_count:
            print(f"  Нормализовано (feat.): {normalized_count}")
    else:
        dbg("Файлов для нормализации (feat.) не найдено.")

    # --- Подэтап 5.2: Анализ уточнений в скобках ---
    annotations = find_tag_annotations(files)
    if annotations:
        # Автоматически помечаем уточнения с недопустимыми символами
        problematic = [
            ann for ann in annotations
            if INVALID_FILENAME_CHARS.search(ann)
        ]
        if problematic:
            print(f"\n  Внимание: {len(problematic)} уточнений содержат "
                  f"недопустимые символы и будут удалены автоматически:")
            for ann in problematic:
                print(f"    - ({ann})")
            # Удаляем их без вопросов
            removed_count = 0
            for ann in problematic:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = read_audio_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(tags[1], ann)
                        if new_title != tags[1]:
                            if DRY_RUN:
                                dbg(f"[DRY RUN] Автоудалил бы: ({ann}) из {f.name}")
                            elif write_audio_tags(f, tags[0], new_title):
                                dbg(f"Автоудалено: ({ann}) из {f.name}")
                                removed_count += 1
            if removed_count:
                print(f"  Автоматически удалено: {removed_count}")
                # Убираем их из списка для показа
                for ann in problematic:
                    del annotations[ann]
    
    if annotations:  # Показываем оставшиеся
        categories = categorize_annotations(annotations)
        
        print(f"\nНайдено {len(annotations)} уникальных "
              f"уточнений в {len(categories)} категориях:")
        
        for cat_key, cat_data in categories.items():
            total_files = sum(len(files) for files in cat_data['items'].values())
            print(f"\n  {cat_data['name']} ({total_files} файлов):")
            for i, (ann, file_list) in enumerate(
                sorted(cat_data['items'].items(), key=lambda x: len(x[1]), reverse=True),
                1
            ):
                print(f"    {i}. ({ann}) — {len(file_list)} файлов")
        
        print("\nКакие категории удалить из тегов?")
        print("  Полные названия: remaster, soundtrack, version, feat, other")
        print("  Сокращения:      r,        s,          v,       f,    o")
        print("  ⚠ feat — уже нормализованы. Удаление "
              "сотрёт информацию об участниках.")
        print("  (y - все, n - оставить все) [n]: ", end='')
        
        choice = input().strip().lower()
        
        # Маппинг сокращений на полные имена
        shortcuts = {
            'r': 'remaster',
            's': 'soundtrack',
            'v': 'version',
            'f': 'feat',
            'o': 'other',
        }
        
        to_remove = set()
        if choice in ('y', 'yes', 'д', 'да'):
            for cat_data in categories.values():
                to_remove.update(cat_data['items'].keys())
        elif choice not in ('n', 'no', 'н', 'нет', ''):
            selected_cats = [c.strip() for c in choice.split(',')]
            for cat_input in selected_cats:
                # Пробуем сокращение
                cat_key = shortcuts.get(cat_input, cat_input)
                if cat_key in categories:
                    to_remove.update(categories[cat_key]['items'].keys())
                else:
                    print(f"  Неизвестная категория: {cat_input}")
        
        if to_remove:
            removed_count = 0
            for ann in to_remove:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = read_audio_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(
                            tags[1], ann
                        )
                        if new_title != tags[1]:
                            if DRY_RUN:
                                dbg(f"[DRY RUN] Удалил бы из тега: "
                                    f"({ann}) из {f.name}")
                            elif write_audio_tags(f, tags[0], new_title):
                                dbg(f"Удалено из тега: ({ann}) "
                                    f"из {f.name}")
                                removed_count += 1
            print(f"  Удалено уточнений из тегов: {removed_count}")


    safe_candidates = []
    mismatch_candidates = []
    missing = []

    for f in files:
        if not f.exists():
            continue

        file_title = extract_title_from_filename(f.stem)
        tags = read_audio_tags(f)
        dbg(f"Анализ: {f.name}, теги: {tags}, "
            f"извлечённое название: {file_title!r}")

        if tags:
            artist = remove_junk(str(tags[0]))
            title = remove_junk(str(tags[1]))
            raw_name = f"{artist} - {title}"
            
            if INVALID_FILENAME_CHARS.search(raw_name):
                # Быстрая проверка: если после очистки получается
                # текущее имя файла — пропускаем без диалога
                cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if f"{cleaned}{f.suffix}" == f.name:
                    dbg(
                        f"Пропущен (имя уже корректно "
                        f"после очистки): {f.name}"
                    )
                    continue
                
                # Иначе — интерактивный выбор
                base_name = sanitize_filename_interactive(
                    raw_name, f.name
                )
                if base_name == "SKIP":
                    print(f"  Оставлен без изменений: {f.name}")
                    continue
            else:
                base_name = raw_name
            
            new_name = f"{base_name}{f.suffix}"

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
        print(f"\nНайдено {len(mismatch_candidates)} файлов "
              f"с расхождением названий (файл vs теги):")
        
        for i, (f, file_title, new_name) in enumerate(
            mismatch_candidates, 1
        ):
            tags = read_audio_tags(f)
            tag_str = (
                f"{tags[0]} - {tags[1]}" if tags else "(нет тегов)"
            )
            enc_issue = (
                has_encoding_issues(tag_str) if tags else False
            )
            marker = " [ПРОБЛЕМА КОДИРОВКИ]" if enc_issue else ""
            print(f"  {i}. {f.name}{marker}")
            print(f"      Файл: {file_title or '(нет)'}")
            print(f"      Теги: {tag_str}")
        
        print("\nВозможные действия:")
        print("  r - переименовать файлы по тегам "
              "(теги считаются правильными)")
        print("  t - обновить теги из имён файлов "
              "(имена считаются правильными)")
        print("  Пропустить - оставить как есть")
        
        rename_input = input(
            "Номера для переименования по тегам "
            "(через запятую, пусто - пропустить): "
        ).strip()
        
        tags_input = input(
            "Номера для обновления тегов из имён "
            "(через запятую, пусто - пропустить): "
        ).strip()
        
        rename_indices = set()
        if rename_input:
            try:
                rename_indices = {
                    int(p.strip()) for p in rename_input.split(',')
                }
            except ValueError:
                print("Некорректный ввод, пропускаю переименование.")
        
        tags_indices = set()
        if tags_input:
            try:
                tags_indices = {
                    int(p.strip()) for p in tags_input.split(',')
                }
            except ValueError:
                print("Некорректный ввод, пропускаю обновление тегов.")
        
        # Переименование по тегам
        for i, (old_path, file_title, new_name) in enumerate(
            mismatch_candidates, 1
        ):
            if i in rename_indices:
                new_path = old_path.with_name(new_name)
                status, result_path = safe_rename(old_path, new_path)
                handle_rename_result(
                    status, old_path, result_path,
                    processed_stats, 'renamed_by_tags',
                )
        
        # Обновление тегов из имён
        for i, (old_path, file_title, new_name) in enumerate(
            mismatch_candidates, 1
        ):
            if i in tags_indices:
                parsed = parse_artist_title(old_path.stem)
                if parsed:
                    if DRY_RUN:
                        dbg(f"[DRY RUN] Обновил бы теги из имени: "
                            f"{old_path.name}")
                    elif write_audio_tags(
                        old_path, parsed[0], parsed[1]
                    ):
                        print(f"  Теги обновлены из имени: "
                              f"{old_path.name}")
                        processed_stats['tags_written'] += 1
                else:
                    print(f"  Не удалось извлечь теги из имени: "
                          f"{old_path.name}")
        
        skipped = (
            len(mismatch_candidates) - len(rename_indices) 
            - len(tags_indices)
        )
        if skipped > 0:
            print(f"  Оставлено без изменений: {skipped}")

    # --- Шаг 6: Запись тегов из имени файла ---
    stage(6, "Запись тегов из имени файла")
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

    # --- Шаг 7: Нормализация имён исполнителей ---
    stage(7, "Нормализация имён исполнителей")
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
            
            choice = get_choice_with_default(
                "Выберите номер варианта (n - пропустить) [1]: "
            )
            
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
                        raw_name = f"{canonical} - {parsed[1]}"
                        
                        if INVALID_FILENAME_CHARS.search(raw_name):
                            base_name = sanitize_filename_interactive(raw_name, f.name)
                            if base_name == "SKIP":
                                dbg(
                                    f"Пропущен из-за недопустимых "
                                    f"символов: {f.name}"
                                )
                                continue
                        else:
                            base_name = raw_name
                        
                        new_name = f"{base_name}{f.suffix}"
                        new_path = f.with_name(new_name)
                        status, result_path = safe_rename(f, new_path)
                        handle_rename_result(
                            status, f, result_path,
                            processed_stats, 'renamed_by_tags',
                        )

    # --- Шаг 8: Ручной ввод для файлов без метаданных ---
    stage(8, "Ручной ввод для файлов без метаданных")
    still_missing = []
    
    # После Этапа 6 у некоторых файлов могли появиться теги —
    # их не нужно обрабатывать вручную
    actually_missing = [
        f for f in missing
        if f.exists() and read_audio_tags(f) is None
    ]
    dbg(f"Файлов без тегов после Этапа 6: {len(actually_missing)} "
        f"(было {len(missing)})")
    
    if actually_missing:
        labels = [f.name for f in actually_missing]
        idx = select_indices(
            labels,
            f"Найдено {len(actually_missing)} файлов без тегов и формата. "
            f"Ввести исполнителя вручную?",
        )
        for i in idx:
            f = actually_missing[i]
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
            
            raw_name = f"{artist} - {title}"
            
            if INVALID_FILENAME_CHARS.search(raw_name):
                base_name = sanitize_filename_interactive(
                    raw_name, f.name
                )
                if base_name == "SKIP":
                    print(f"  Оставлен без изменений: {f.name}")
                    # Не добавляем в still_missing, файл просто пропускается
                    continue
            else:
                base_name = raw_name
            
            new_name = f"{base_name}{f.suffix}"
            new_path = f.with_name(new_name)
            status, result_path = safe_rename(f, new_path)
            handle_rename_result(
                status, f, result_path,
                processed_stats, 'manual_input',
            )

    # --- Финальная статистика ---
    print("\n" + "="*60)
    print("=== Статистика обработки ===")
    print("="*60)
    print(f"Очистка от мусора (имена): "
          f"{processed_stats['junk_removed']}")
    print(f"Очистка от мусора (теги): "
          f"{processed_stats['tags_cleaned']}")
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
