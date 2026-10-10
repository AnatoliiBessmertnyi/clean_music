"""Функции для работы с именами файлов и нормализации текста."""

import re
import unicodedata

from config import (
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
    pattern = re.compile(r'\s*[\(\[]\s*' + re.escape(annotation) + r'\s*[\)\]]\s*', re.IGNORECASE)
    return pattern.sub('', text).strip()
