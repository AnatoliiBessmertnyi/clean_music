"""Функции для работы с именами файлов и нормализации текста."""

import re
import unicodedata

from config import (
    FEAT_VARIANTS_PATTERN,
    INVALID_FILENAME_CHARS,
    JUNK_PATTERN,
)

_BRACKET_GROUP = re.compile(r'\(([^()\[\]]*)\)|\[([^()\[\]]*)\]')
COPY_SUFFIX = re.compile(r' \([1-9][0-9]*\)')
_BRACKET_CHAR = re.compile(r'[()\[\]]')


def normalize_filename_stem(stem: str) -> str:
    """Нормализует название файла, удаляя лишние пробелы."""
    return re.sub(r'\s+', ' ', stem).strip()


def normalize_unicode(text: str) -> str:
    """Нормализует Unicode: убирает диакритические знаки для сравнения.

    Примеры:
    - 'Voilà' → 'Voila'
    - 'Björk' → 'Bjork'
    - 'Motörhead' → 'Motorhead'
    - 'Mötley Crüe' → 'Motley Crue'
    """
    # NFKD decomposes characters: é → e + ´
    normalized = unicodedata.normalize('NFKD', text)
    # Убираем комбинирующие символы (акценты)
    return ''.join(c for c in normalized if not unicodedata.combining(c))


def remove_junk(text: str) -> str:
    """Удаляет мусорные вставки вида (vksaver) из строки."""

    def remove_group(match: re.Match[str]) -> str:
        group = match.group()
        return '' if JUNK_PATTERN.fullmatch(group) else group

    return re.sub(r'\s*(?:\([^()\[\]]*\)|\[[^()\[\]]*\])', remove_group, text).strip()


def normalize_dash(text: str) -> str:
    """Заменяет все виды тире на стандартный дефис."""
    return re.sub(r'[–—]', '-', text)


def normalize_title_spacing(text: str) -> str:
    """Нормализует пробелы вокруг скобок в названии.

    Примеры:
    - 'Waiting(feat. X)' → 'Waiting (feat. X)'
    - 'Title  (feat. X)' → 'Title (feat. X)'
    - 'Title ( feat. X )' → 'Title (feat. X)'
    """
    # Пробел перед открывающей скобкой
    text = re.sub(r'\s*\(', ' (', text)
    text = re.sub(r'\s*\[', ' [', text)
    # Убираем пробелы сразу после открывающей скобки
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\[\s+', '[', text)
    # Убираем пробелы перед закрывающей скобкой
    text = re.sub(r'\s+\)', ')', text)
    text = re.sub(r'\s+\]', ']', text)
    # Убираем множественные пробелы
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def split_qualifiers(text: str) -> tuple[str, tuple[str, ...]]:
    """Отделяет хвостовые группы, сохраняя порядок и вид парных скобок."""
    base = text.rstrip()
    groups: list[str] = []
    while base.endswith((')', ']')):
        expected = []
        for index in range(len(base) - 1, -1, -1):
            char = base[index]
            if char in ')]':
                expected.append('(' if char == ')' else '[')
            elif char in '([':
                if char != expected.pop():
                    return base, tuple(reversed(groups))
                if not expected:
                    break
        else:
            break
        group = base[index:]
        if not group[1:-1].strip():
            break
        groups.append(group)
        base = base[:index].rstrip()
    return base or text, tuple(reversed(groups))


def title_separator(title: str) -> int | None:
    """Первый слэш вне скобок. При нарушенной парности деление небезопасно."""
    expected = []
    separator = None
    for index, char in enumerate(title):
        if char in '([':
            expected.append(')' if char == '(' else ']')
        elif char in ')]':
            if not expected or expected.pop() != char:
                return None
        elif char == '/' and not expected and separator is None:
            separator = index
    return None if expected else separator


def qualifier_name_candidate(stem: str, source_stem: str) -> tuple[str, bool]:
    """Имя с написанием из тегов и признак необходимости отдельного выбора."""
    cleaned = remove_junk(source_stem)
    _, groups = split_qualifiers(cleaned)
    _, existing = split_qualifiers(stem)

    def key(group: str) -> str:
        _, normalized = normalize_feat_pair('', group)
        return normalize_filename_stem(normalized[1:-1]).casefold()

    available = [key(group) for group in existing]
    if (
        groups
        and cleaned.endswith(' ' + groups[-1])
        and COPY_SUFFIX.fullmatch(' ' + groups[-1])
    ):
        groups = groups[:-1]
    unmatched = []
    for group in groups:
        value = key(group)
        if value in available:
            available.remove(value)
        else:
            unmatched.append((group, value))

    ambiguous = False
    for group, value in unmatched:
        # Только сигнал для ручного выбора. Анализ дублей использует исходные признаки.
        alternatives = [
            other
            for other in available
            if len(other) == len(value)
            and all(
                a == b
                or (
                    unicodedata.category(a).startswith('P')
                    and unicodedata.category(b).startswith('P')
                    and not INVALID_FILENAME_CHARS.search(a + b)
                )
                for a, b in zip(value, other, strict=True)
            )
        ]
        if alternatives:
            ambiguous = True
            available.remove(alternatives[0])
        else:
            stem += ' ' + group
    return stem, ambiguous


def preserve_name_qualifiers(stem: str, source_stem: str) -> str:
    proposed, ambiguous = qualifier_name_candidate(stem, source_stem)
    return source_stem if ambiguous else proposed


def build_safe_filename(
    artist: str, title: str, extension: str, *, source_stem: str = ''
) -> str:
    """Формирует безопасное имя файла из тегов.

    Args:
        artist: Имя исполнителя
        title: Название трека
        extension: Расширение файла (с точкой, например '.mp3')

    Returns:
        Безопасное имя файла без недопустимых символов

    Examples:
        >>> build_safe_filename("Artist", "Title", ".mp3")
        'Artist - Title.mp3'
        >>> build_safe_filename("AC/DC", "Back in Black", ".mp3")
        'ACDC - Back in Black.mp3'
    """
    base, groups = split_qualifiers(title)
    if groups and '/' in title:
        suffix = ' '.join(group.replace('/', '_') for group in groups)
        title = f'{base} {suffix}' if base != title else suffix
    raw_name = normalize_title_spacing(f'{artist} - {title}')
    cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return f'{preserve_name_qualifiers(cleaned, source_stem)}{extension}'


def remove_annotation_from_text(text: str, annotation: str) -> str:
    """Удаляет конкретное уточнение в скобках из текста."""
    pattern = re.compile(
        r'\s*[\(\[]\s*' + re.escape(annotation) + r'\s*[\)\]]\s*', re.IGNORECASE
    )
    return pattern.sub('', text).strip()


def normalize_feat_pair(artist: str, title: str) -> tuple[str, str]:
    """Переносит однозначно выделенных гостей исполнителя в название."""
    artist_feat = FEAT_VARIANTS_PATTERN.search(artist)
    guests = ''
    if artist_feat:
        base_artist = artist[: artist_feat.start()].strip()
        guests = artist[artist_feat.end() :].strip()
        if (
            not base_artist
            or not guests
            or _BRACKET_CHAR.search(artist)
            or FEAT_VARIANTS_PATTERN.search(guests)
            or re.search(r'\s+[-–—]\s+', guests)
        ):
            return artist, title
    else:
        base_artist = artist

    outside_groups = _BRACKET_GROUP.sub(' ', title)
    if _BRACKET_CHAR.search(outside_groups):
        return artist, title

    title_feat = FEAT_VARIANTS_PATTERN.search(outside_groups)
    if title_feat:
        # В присутствии уточнений или тире границы списка гостей неоднозначны.
        if outside_groups != title:
            return artist, title
        base_title = title[: title_feat.start()].strip()
        title_guests = title[title_feat.end() :].strip()
        if (
            not base_title
            or not title_guests
            or FEAT_VARIANTS_PATTERN.search(title_guests)
            or re.search(r'\s+[-–—]\s+', title_guests)
        ):
            return artist, title
        title = f'{base_title} (feat. {title_guests})'

    def normalize_group(match: re.Match[str]) -> str:
        content = (match.group(1) or match.group(2) or '').strip()
        marker = FEAT_VARIANTS_PATTERN.match(content)
        if marker and content[marker.end() :]:
            return f'(feat. {content[marker.end() :]})'
        return match.group()

    title = _BRACKET_GROUP.sub(normalize_group, title)
    if guests and f'(feat. {guests})' not in title:
        title = f'{title} (feat. {guests})'
    return base_artist, title


def remove_feat_guests_from_artist(artist: str, title: str) -> str:
    """Удаляет гостей из Artist, если они уже указаны в Title через (feat.)."""
    if _BRACKET_CHAR.search(artist) or _BRACKET_CHAR.search(
        _BRACKET_GROUP.sub('', title)
    ):
        return artist

    feat_pattern = re.compile(r'\(feat\.\s*([^\)]+)\)', re.IGNORECASE)
    blocks = feat_pattern.findall(title)
    if not blocks:
        return artist

    guests = {g.strip().casefold() for block in blocks for g in block.split(',')}

    artists = [a.strip() for a in re.split(r'\s*[,;]\s*', artist)]

    filtered_artists = [a for a in artists if a.casefold() not in guests]

    if not filtered_artists or filtered_artists == artists:
        return artist

    return ', '.join(filtered_artists)


def normalize_artist_separators(artist: str) -> str:
    """Нормализует разделители исполнителей на запятую с пробелом."""
    if _BRACKET_CHAR.search(artist):
        return artist
    return re.sub(r'\s*[,;]\s*', ', ', artist)
