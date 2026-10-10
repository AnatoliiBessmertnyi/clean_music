"""Конфигурация скрипта очистки аудиофайлов."""

import re

# Список мусорных ключевых слов для очистки из имён файлов и тегов.
# Добавляйте новые паттерны сюда. Точку в доменах экранируйте: 'site\.ru'
JUNK_KEYWORDS: list[str] = [
    r'vksaver',
    r'muzmo\.ru',
    r'zaycev\.net',
    r'myzuka\.club',
]

# Динамически строим паттерн из списка ключевых слов
_junk_keywords_regex = '|'.join(JUNK_KEYWORDS)
JUNK_PATTERN = re.compile(
    r'\s*[\(\[][^)\]]*(?:' + _junk_keywords_regex + r')[^)\]]*[\)\]]', re.IGNORECASE
)

NAME_PATTERN = re.compile(r'^(.+?)\s+[-–—]\s+(.+)$')
TRACK_NUMBER_PATTERN = re.compile(r'^(track\s*)?\d{1,3}\.?\s*$', re.IGNORECASE)
LEADING_TRACK_PATTERN = re.compile(
    r'^(?:track\s*)?\d{1,3}(?:\.\s*|\s*[-–—]+\s*)(.+)$', re.IGNORECASE
)
INVALID_FILENAME_CHARS = re.compile(r'[\\:*?"<>|/]')
FEAT_VARIANTS_PATTERN = re.compile(r'\b(?:feat(?:uring)?|ft)\b\.?\s*', re.IGNORECASE)

SUPPORTED_EXTENSIONS: set[str] = {'.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.wma'}
ARTIST_NAME_EXCEPTIONS: list[str] = ['AC/DC']
TOTAL_STAGES = 7
