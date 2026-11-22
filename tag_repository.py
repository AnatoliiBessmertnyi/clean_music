"""Репозиторий для работы с тегами аудиофайлов с кэшированием."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto
from os import stat_result
from pathlib import Path
from typing import Any, BinaryIO

from mutagen import FileType, MutagenError
from mutagen.asf import ASF, ASFBaseAttribute
from mutagen.flac import FLAC
from mutagen.id3 import TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from file_state import OperationResult, OperationStatus
from report_events import log_operation

logger = logging.getLogger('clean_music')


@dataclass(frozen=True)
class AudioTags:
    """Теги аудиофайла (исполнитель и название).

    Attributes:
        artist: Имя исполнителя
        title: Название трека
    """

    artist: str
    title: str


class ReadStatus(Enum):
    COMPLETE = auto()
    PARTIAL = auto()
    EMPTY = auto()
    ERROR = auto()


@dataclass(frozen=True)
class AudioInfo:
    format: str
    codec: str | None
    duration: float | None
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    bits_per_sample: int | None


@dataclass(frozen=True)
class MetadataRead:
    """Результат чтения. Пустые строки обозначают отсутствующие поля."""

    tags: AudioTags | None
    info: AudioInfo | None = None
    error: str | None = None

    @property
    def status(self) -> ReadStatus:
        if self.tags is None:
            return ReadStatus.ERROR
        if self.tags.artist and self.tags.title:
            return ReadStatus.COMPLETE
        if self.tags.artist or self.tags.title:
            return ReadStatus.PARTIAL
        return ReadStatus.EMPTY

    @property
    def complete_tags(self) -> AudioTags | None:
        return self.tags if self.status is ReadStatus.COMPLETE else None


def _positive[T: (int, float)](value: T | None) -> T | None:
    return value if value is not None and value > 0 else None


def _fingerprint(stat: stat_result) -> tuple[int, ...]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _tag_value(value: object) -> object:
    """Сохраняет типы, кратность и свойства значений Mutagen."""
    if isinstance(value, dict):
        return {key: _tag_value(item) for key, item in value.items()}
    if isinstance(value, ASFBaseAttribute):
        # В профиле без таблицы языков ASF нулевые индексы эквивалентны
        # отсутствующему языку и привязке ко всему файлу.
        return type(value), value.value, value.language or 0, value.stream or 0
    if isinstance(value, Enum):
        return type(value), value.name
    if isinstance(value, (str, int, bool, float)):
        return type(value), value
    fields = getattr(value, '__dict__', None)
    state = _tag_value(fields) if fields else None
    if isinstance(value, (list, tuple)):
        payload: object = tuple(_tag_value(item) for item in value)
    elif isinstance(value, bytes):
        payload = bytes(value)
    else:
        payload = repr(value) if state is None else None
    return type(value), payload, state


class TagRepository:
    """Читает метаданные и хранит модельные изменения dry-run."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[int, ...] | None, MetadataRead]] = {}
        self._model_sources: dict[Path, Path] = {}

    def get_tags(self, filepath: Path) -> MetadataRead:
        key = filepath.absolute()
        source = self._model_sources.get(key, key)
        try:
            stamp = _fingerprint(source.stat())
        except FileNotFoundError:
            stamp = None
        except OSError as exc:
            self._cache.pop(key, None)
            return MetadataRead(None, error=str(exc))

        cached = self._cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        self._cache.pop(key, None)
        result = self.read_tags_uncached(source)
        if stamp is not None and result.tags is not None:
            try:
                if _fingerprint(source.stat()) == stamp:
                    self._cache[key] = (stamp, result)
                    return result
            except OSError as exc:
                return MetadataRead(None, error=str(exc))
            return MetadataRead(None, error='файл изменился во время чтения')
        return result

    def apply_tags(
        self,
        filepath: Path,
        artist: str,
        title: str,
        *,
        simulated: bool,
        publish: Callable[[str | None, str | None], tuple[OperationStatus, str]]
        | None = None,
    ) -> OperationResult:
        """Применяет запись тегов и публикует её результат."""
        tags = AudioTags(artist, title)
        key = filepath.absolute()
        before = self.get_tags(filepath)
        if before.tags == tags:
            return OperationResult(OperationStatus.NOOP, filepath, 'теги не изменились')
        if simulated and before.tags is not None:
            self._update_model(filepath, tags, before)
            return OperationResult(
                OperationStatus.SIMULATED, filepath, 'запись запланирована'
            )

        if before.tags is None:
            status = OperationStatus.ERROR
            reason = before.error or 'чтение метаданных не подтверждено'
        elif publish is None:
            status, reason = OperationStatus.ERROR, 'требуется исполнитель публикации'
        else:
            status, reason = publish(
                artist if artist != before.tags.artist else None,
                title if title != before.tags.title else None,
            )
        if status in (OperationStatus.SUCCESS, OperationStatus.UNCERTAIN):
            self._cache.pop(key, None)
        if status is OperationStatus.SUCCESS:
            logger.info(
                'Теги записаны: %s; artist=%r; title=%r',
                filepath,
                artist,
                title,
                extra={'duplicate_record': filepath.is_absolute()},
            )
        log_operation(
            'tags',
            filepath,
            'успех' if status is OperationStatus.SUCCESS else 'отказ',
            reason,
            artist=artist,
            title=title,
            proposed=simulated,
        )
        return OperationResult(status, filepath, reason)

    def read_tags_uncached(self, filepath: Path) -> MetadataRead:
        """Читает физический файл в обход кэша и модели."""
        return self._read_tags(filepath, filepath)

    def read_open_tags(self, filepath: Path, stream: BinaryIO) -> MetadataRead:
        """Читает теги защищённого объекта, используя имя для выбора формата."""
        stream.seek(0)
        return self._read_tags(filepath, stream)

    @staticmethod
    def _read_tags(filepath: Path, source: Path | BinaryIO) -> MetadataRead:
        ext = filepath.suffix.lower()
        parsers = {
            '.mp3': (MP3, 'MPEG audio', 'TPE1', 'TIT2'),
            '.m4a': (MP4, 'MP4', '©ART', '©nam'),
            '.mp4': (MP4, 'MP4', '©ART', '©nam'),
            '.flac': (FLAC, 'FLAC', 'artist', 'title'),
            '.ogg': (OggVorbis, 'Ogg', 'artist', 'title'),
            '.wma': (ASF, 'ASF', 'Author', 'Title'),
        }
        if ext not in parsers:
            return MetadataRead(None, error='формат файла не поддерживается')
        parser, format_name, artist_key, title_key = parsers[ext]
        try:
            audio = parser(source)
            artist = audio.tags.get(artist_key) if audio.tags is not None else None
            title = audio.tags.get(title_key) if audio.tags is not None else None
            if ext == '.mp3':
                tags = AudioTags(
                    str(artist) if artist else '', str(title) if title else ''
                )
            else:
                tags = AudioTags(
                    str(artist[0]) if artist else '', str(title[0]) if title else ''
                )

            info = getattr(audio, 'info', None)
            codec = None
            if ext == '.mp3':
                layer = getattr(info, 'layer', None)
                if layer is not None:
                    codec = f'MPEG audio layer {layer}'
            elif ext == '.flac':
                codec = 'FLAC'
            elif ext == '.ogg':
                codec = 'Vorbis'
            elif ext == '.wma':
                codec = getattr(info, 'codec_type', None) or getattr(
                    info, 'codec_name', None
                )
            else:
                codec = getattr(info, 'codec', None)
            return MetadataRead(
                tags,
                AudioInfo(
                    format_name,
                    codec or None,
                    _positive(getattr(info, 'length', None)),
                    _positive(getattr(info, 'bitrate', None)),
                    _positive(getattr(info, 'sample_rate', None)),
                    _positive(getattr(info, 'channels', None)),
                    _positive(getattr(info, 'bits_per_sample', None)),
                ),
            )
        except (MutagenError, OSError, ValueError) as exc:
            return MetadataRead(None, error=str(exc) or type(exc).__name__)

    def invalidate(self, filepath: Path) -> None:
        key = filepath.absolute()
        self._cache.pop(key, None)
        self._model_sources.pop(key, None)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._model_sources.clear()

    @staticmethod
    def _tag_parser(path: Path) -> tuple[type[FileType], str, str]:
        return {
            '.mp3': (MP3, 'TPE1', 'TIT2'),
            '.m4a': (MP4, '©ART', '©nam'),
            '.mp4': (MP4, '©ART', '©nam'),
            '.flac': (FLAC, 'artist', 'title'),
            '.ogg': (OggVorbis, 'artist', 'title'),
            '.wma': (ASF, 'Author', 'Title'),
        }[path.suffix.lower()]

    def tag_snapshot(
        self, path: Path, artist: str | None, title: str | None, *, result: bool = False
    ) -> tuple[object, ...]:
        parser, artist_key, title_key = self._tag_parser(path)
        try:
            audio = parser(path)
            tags = audio.tags
            values: dict[str, Any] = dict(tags.items()) if tags is not None else {}
            for key, proposed in ((artist_key, artist), (title_key, title)):
                if proposed is None:
                    continue
                if result:
                    value = values.get(key)
                    if path.suffix.lower() == '.mp3':
                        actual = getattr(value, 'text', None)
                    else:
                        actual = (
                            [str(item) for item in value] if value is not None else None
                        )
                    if actual != [proposed]:
                        raise OSError(f'записанное поле {key} отличается')
                values.pop(key, None)
            pictures = [picture.write() for picture in getattr(audio, 'pictures', [])]
            unknown = tuple(getattr(tags, 'unknown_frames', ()))
            return _tag_value(values), tuple(pictures), unknown
        except (MutagenError, ValueError) as exc:
            raise OSError('не удалось перечитать теги для проверки') from exc

    def save_work_tags(
        self, path: Path, stream: BinaryIO, artist: str | None, title: str | None
    ) -> None:
        """Редактирует открытый дескриптор отдельной рабочей копии."""
        parser, artist_key, title_key = self._tag_parser(path)
        try:
            stream.seek(0)
            audio = parser(stream)
            if audio.tags is None:
                audio.add_tags()
            if audio.tags is None:
                raise OSError('запись тегов недоступна для контейнера')
            for key, value, frame in (
                (artist_key, artist, TPE1),
                (title_key, title, TIT2),
            ):
                if value is not None:
                    audio.tags[key] = (
                        frame(encoding=3, text=[value]) if parser is MP3 else [value]
                    )
            stream.seek(0)
            audio.save(stream)
        except (MutagenError, ValueError) as exc:
            raise OSError('Mutagen не смог сохранить рабочую копию') from exc

    def simulate_update_tags(self, filepath: Path, artist: str, title: str) -> None:
        """Имитирует обновление тегов для dry-run режима."""
        self._update_model(filepath, AudioTags(artist, title), self.get_tags(filepath))

    def _update_model(
        self, filepath: Path, tags: AudioTags, before: MetadataRead
    ) -> None:
        key = filepath.absolute()
        cached = self._cache.get(key)
        source = self._model_sources.get(key, key)
        try:
            stamp = cached[0] if cached is not None else _fingerprint(source.stat())
        except OSError:
            stamp = None
        self._cache[key] = (stamp, replace(before, tags=tags, error=None))
        log_operation(
            'tags',
            filepath,
            'ПРЕДЛОЖЕНИЕ',
            'план записи выбранных artist/title; диск не изменён',
            artist=tags.artist,
            title=tags.title,
        )

    def simulate_rename(self, old_path: Path, new_path: Path) -> None:
        """Имитирует переименование файла для dry-run режима.

        Переносит кэшированные теги со старого пути на новый.

        Args:
            old_path: Старый путь к файлу
            new_path: Новый путь к файлу
        """
        old_key, new_key = old_path.absolute(), new_path.absolute()
        source = self._model_sources.pop(old_key, old_key)
        cached = self._cache.pop(old_key, None)
        self.invalidate(new_key)
        self._model_sources[new_key] = source
        if cached is not None:
            self._cache[new_key] = cached

    def simulate_unlink(self, filepath: Path) -> None:
        """Имитирует удаление файла для dry-run режима.

        Удаляет файл из кэша.

        Args:
            filepath: Путь к удаляемому файлу
        """
        self.invalidate(filepath)
