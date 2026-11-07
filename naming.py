"""Функции для работы с именами файлов и нормализации текста."""

import re
import unicodedata

from config import (
    ARTIST_NAME_EXCEPTIONS,
    FEAT_VARIANTS_PATTERN,
    INVALID_FILENAME_CHARS,
    JUNK_PATTERN,
)


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
    return JUNK_PATTERN.sub('', text).strip()


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


def build_safe_filename(artist: str, title: str, extension: str) -> str:
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
    raw_name = f'{artist} - {title}'
    raw_name = normalize_title_spacing(raw_name)
    cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return f'{cleaned}{extension}'


def remove_annotation_from_text(text: str, annotation: str) -> str:
    """Удаляет конкретное уточнение в скобках из текста."""
    pattern = re.compile(
        r'\s*[\(\[]\s*' + re.escape(annotation) + r'\s*[\)\]]\s*', re.IGNORECASE
    )
    return pattern.sub('', text).strip()


def normalize_feat_pair(artist: str, title: str) -> tuple[str, str]:
    """Нормализует пару тегов: переносит feat из artist в title.

    Правила:
    - feat должен быть ТОЛЬКО в title
    - Из artist feat удаляется
    - Все гости объединяются в один блок (feat. X, Y, Z)
    - Если в блоке feat есть тире (feat. X - Y), то Y — это название, X — гость
    """
    # 1. Извлекаем всех feat-гостей из artist
    artist_guests: list[str] = []
    feat_matches = list(FEAT_VARIANTS_PATTERN.finditer(artist))
    if feat_matches:
        for i, match in enumerate(feat_matches):
            start = match.end()
            end = (
                feat_matches[i + 1].start()
                if i + 1 < len(feat_matches)
                else len(artist)
            )
            guest_part = artist[start:end].strip()
            for g in re.split(r'\s*[&,]\s*', guest_part):
                g = g.strip()
                if g and g not in artist_guests:
                    artist_guests.append(g)

        base = artist
        for i, match in enumerate(feat_matches):
            end = (
                feat_matches[i + 1].start()
                if i + 1 < len(feat_matches)
                else len(artist)
            )
            base = base[: match.start()] + base[end:]
        cleaned_artist = re.sub(r'\s+', ' ', base).strip()
    else:
        cleaned_artist = artist

    # 2. Извлекаем feat-гостей из title
    title_guests: list[str] = []
    real_title_part = ''

    bracket_feat_pattern = re.compile(
        r'[\(\[]\s*(?:feat(?:uring)?|ft)\b\.?\s*([^\)\]]+)[\)\]]', re.IGNORECASE
    )
    matches = list(bracket_feat_pattern.finditer(title))
    if matches:
        result_parts: list[str] = []
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
    else:
        cleaned_title = title

    title_feat_matches = list(FEAT_VARIANTS_PATTERN.finditer(cleaned_title))
    if title_feat_matches:
        base_parts: list[str] = []
        last_end = 0
        for i, match in enumerate(title_feat_matches):
            base_parts.append(cleaned_title[last_end : match.start()])
            start = match.end()
            end = (
                title_feat_matches[i + 1].start()
                if i + 1 < len(title_feat_matches)
                else len(cleaned_title)
            )
            guest_part = cleaned_title[start:end].strip()

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

        if real_title_part:
            cleaned_title = real_title_part

    # 3. Объединяем гостей
    all_guests: list[str] = []
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


def remove_feat_guests_from_artist(artist: str, title: str) -> str:
    """Удаляет гостей из Artist, если они уже указаны в Title через (feat.)."""
    feat_pattern = re.compile(r'\(feat\.\s*([^\)]+)\)', re.IGNORECASE)
    match = feat_pattern.search(title)
    if not match:
        return artist

    guests_str = match.group(1)
    guests = [g.strip() for g in re.split(r'\s*,\s*', guests_str)]

    artists = [a.strip() for a in re.split(r'\s*[,;]\s*', artist)]

    guests_lower = {g.lower() for g in guests}
    filtered_artists = [a for a in artists if a.lower() not in guests_lower]

    if not filtered_artists:
        return artist

    return ', '.join(filtered_artists)


def normalize_artist_separators(artist: str) -> str:
    """Нормализует разделители исполнителей на запятую с пробелом."""
    artist_lower = artist.lower()
    for exception in ARTIST_NAME_EXCEPTIONS:
        if exception.lower() in artist_lower:
            result = artist.replace(';', ',')
            result = re.sub(r'\s*,\s*', ', ', result)
            result = re.sub(r'\s+', ' ', result).strip()
            return result

    result = artist.replace(';', ',')
    result = re.sub(r'\s*/\s*', ', ', result)
    result = re.sub(r'\s*,\s*', ', ', result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result
