"""Функции для парсинга имён файлов и извлечения метаданных."""

import re

from config import (
    LEADING_TRACK_PATTERN,
    NAME_PATTERN,
    TRACK_NUMBER_PATTERN,
)


def is_track_number(text: str) -> bool:
    """Проверяет, является ли строка номером трека (01, 1., Track 5)."""
    return bool(TRACK_NUMBER_PATTERN.match(text.strip()))


def parse_artist_title(filename: str) -> tuple[str, str] | None:
    """Извлекает исполнителя и название из формата 'Artist - Title'.

    Args:
        filename: Имя файла без расширения (stem)

    Returns:
        Кортеж (artist, title) или None, если формат не распознан

    Examples:
        >>> parse_artist_title("Artist - Title")
        ('Artist', 'Title')
        >>> parse_artist_title("01. Title")
        None
    """
    match = NAME_PATTERN.match(filename)
    if match:
        artist = match.group(1).strip()
        title = match.group(2).strip()
        if is_track_number(artist):
            return None
        return artist, title
    return None


def extract_title_from_filename(filename: str) -> str:
    """Извлекает название из имени файла, отбрасывая номер трека.

    Args:
        filename: Имя файла без расширения (stem)

    Returns:
        Название трека

    Examples:
        >>> extract_title_from_filename("01. Title")
        'Title'
        >>> extract_title_from_filename("Artist - Title")
        'Artist - Title'
    """
    match = LEADING_TRACK_PATTERN.match(filename)
    if match:
        return match.group(1).strip()
    return filename


def clean_extracted_tags(artist: str, title: str) -> tuple[str, str]:
    """Очищает извлечённые из имени файла теги от непарных скобок.

    Args:
        artist: Имя исполнителя из имени файла
        title: Название трека из имени файла

    Returns:
        Очищенные (artist, title)
    """

    def clean_text(text: str) -> str:
        # Первый проход: удаляем непарные закрывающие скобки
        result = []
        paren_depth = 0
        bracket_depth = 0

        for ch in text:
            if ch == '(':
                paren_depth += 1
                result.append(ch)
            elif ch == ')':
                if paren_depth > 0:
                    paren_depth -= 1
                    result.append(ch)
                # else: пропускаем
            elif ch == '[':
                bracket_depth += 1
                result.append(ch)
            elif ch == ']':
                if bracket_depth > 0:
                    bracket_depth -= 1
                    result.append(ch)
            else:
                result.append(ch)

        text = ''.join(result)

        # Второй проход: удаляем непарные открывающие скобки
        # Идём справа налево
        result = []
        paren_depth = 0
        bracket_depth = 0

        for ch in reversed(text):
            if ch == ')':
                paren_depth += 1
                result.append(ch)
            elif ch == '(':
                if paren_depth > 0:
                    paren_depth -= 1
                    result.append(ch)
                # else: пропускаем
            elif ch == ']':
                bracket_depth += 1
                result.append(ch)
            elif ch == '[':
                if bracket_depth > 0:
                    bracket_depth -= 1
                    result.append(ch)
            else:
                result.append(ch)

        text = ''.join(reversed(result))

        # Нормализуем пробелы
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'\s*[-–—]\s*', ' - ', text)

        return text

    return clean_text(artist), clean_text(title)
