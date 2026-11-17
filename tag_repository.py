"""Репозиторий для работы с тегами аудиофайлов с кэшированием."""

import logging
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

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


class TagRepository:
    """Репозиторий для работы с тегами аудиофайлов.

    Кэширует прочитанные теги для избежания многократного чтения с диска.
    Автоматически инвалидирует кэш при записи новых тегов.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, AudioTags | None] = {}
        self._write_error: str | None = None

    def get_tags(self, filepath: Path) -> AudioTags | None:
        """Возвращает теги файла, используя кэш при наличии.

        Args:
            filepath: Путь к аудиофайлу

        Returns:
            AudioTags с исполнителем и названием, или None если теги не найдены
        """
        if filepath not in self._cache:
            self._cache[filepath] = self._read_from_disk(filepath)
        return self._cache[filepath]

    def update_tags(self, filepath: Path, artist: str, title: str) -> bool:
        """Записывает новые теги и обновляет кэш.

        Args:
            filepath: Путь к аудиофайлу
            artist: Новое имя исполнителя
            title: Новое название трека

        Returns:
            True если запись успешна, False в противном случае
        """
        self._write_error = None
        success = self._write_to_disk(filepath, artist, title)
        if success:
            self._cache[filepath] = AudioTags(artist, title)
            logger.info(
                'Теги записаны: %s; artist=%r; title=%r', filepath, artist, title
            )
        log_operation(
            'tags',
            filepath,
            'успех' if success else 'отказ',
            'запись выбранных artist/title'
            if success
            else (self._write_error or 'запись тегов не подтверждена'),
            artist=artist,
            title=title,
        )
        return success

    def read_tags_uncached(self, filepath: Path) -> AudioTags | None:
        """Читает для отчёта с диска; ошибки не превращаются в отсутствие тегов."""
        return self._read_tags(filepath, report_errors=True)

    def invalidate(self, filepath: Path) -> None:
        """Удаляет файл из кэша.

        Args:
            filepath: Путь к файлу для удаления из кэша
        """
        self._cache.pop(filepath, None)

    def clear_cache(self) -> None:
        """Очищает весь кэш."""
        self._cache.clear()

    def _read_from_disk(self, filepath: Path) -> AudioTags | None:
        """Читает теги для конвейера с прежней обработкой ошибок."""
        return self._read_tags(filepath)

    def _read_tags(
        self, filepath: Path, *, report_errors: bool = False
    ) -> AudioTags | None:
        """Читает теги непосредственно с диска."""
        ext = filepath.suffix.lower()
        try:
            if ext == '.mp3':
                audio = MP3(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('TPE1')
                title = audio.tags.get('TIT2')
                if report_errors:
                    return AudioTags(
                        str(artist) if artist else '', str(title) if title else ''
                    )
                if artist and title:
                    return AudioTags(str(artist), str(title))
            elif ext in ('.m4a', '.mp4'):
                audio = MP4(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('©ART')
                title = audio.tags.get('©nam')
                if report_errors:
                    return AudioTags(
                        str(artist[0]) if artist else '', str(title[0]) if title else ''
                    )
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.flac':
                audio = FLAC(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('artist')
                title = audio.tags.get('title')
                if report_errors:
                    return AudioTags(
                        str(artist[0]) if artist else '', str(title[0]) if title else ''
                    )
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.ogg':
                audio = OggVorbis(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('artist')
                title = audio.tags.get('title')
                if report_errors:
                    return AudioTags(
                        str(artist[0]) if artist else '', str(title[0]) if title else ''
                    )
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.wma':
                audio = ASF(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('Author')
                title = audio.tags.get('Title')
                if report_errors:
                    return AudioTags(
                        str(artist[0]) if artist else '', str(title[0]) if title else ''
                    )
                if artist and title:
                    return AudioTags(artist[0], title[0])
        except (MutagenError, OSError, ValueError):
            if report_errors:
                raise
        return None

    def _write_to_disk(self, filepath: Path, artist: str, title: str) -> bool:
        """Записывает теги непосредственно на диск."""
        ext = filepath.suffix.lower()
        try:
            if ext == '.mp3':
                try:
                    audio = MP3(filepath)  # type: ignore[assignment]
                except ID3NoHeaderError:
                    audio = MP3(filepath, ID3=ID3)  # type: ignore[assignment]
                if audio.tags is None:
                    audio.add_tags()
                if audio.tags is not None:
                    audio.tags['TPE1'] = TPE1(encoding=3, text=artist)
                    audio.tags['TIT2'] = TIT2(encoding=3, text=title)
                    audio.save()
                    return True
            elif ext in ('.m4a', '.mp4'):
                audio = MP4(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    audio.add_tags()
                if audio.tags is not None:
                    audio.tags['©ART'] = [artist]
                    audio.tags['©nam'] = [title]
                    audio.save()
                    return True
            elif ext == '.flac':
                audio = FLAC(filepath)  # type: ignore[assignment]
                audio['artist'] = [artist]
                audio['title'] = [title]
                audio.save()
                return True
            elif ext == '.ogg':
                audio = OggVorbis(filepath)  # type: ignore[assignment]
                audio['artist'] = [artist]
                audio['title'] = [title]
                audio.save()
                return True
            elif ext == '.wma':
                audio = ASF(filepath)  # type: ignore[assignment]
                audio['Author'] = [artist]
                audio['Title'] = [title]
                audio.save()
                return True
        except (MutagenError, OSError, ValueError) as e:
            self._write_error = str(e)
            logger.error('Ошибка записи тегов %s: %s', filepath, e)
        return False

    def simulate_update_tags(self, filepath: Path, artist: str, title: str) -> None:
        """Имитирует обновление тегов для dry-run режима.

        Обновляет кэш без записи на диск, чтобы следующие этапы
        видели "новые" теги при сухом прогоне.

        Args:
            filepath: Путь к файлу
            artist: Новое имя исполнителя
            title: Новое название трека
        """
        self._cache[filepath] = AudioTags(artist, title)
        log_operation(
            'tags',
            filepath,
            'ПРЕДЛОЖЕНИЕ',
            'план записи выбранных artist/title; диск не изменён',
            artist=artist,
            title=title,
        )

    def simulate_rename(self, old_path: Path, new_path: Path) -> None:
        """Имитирует переименование файла для dry-run режима.

        Переносит кэшированные теги со старого пути на новый.

        Args:
            old_path: Старый путь к файлу
            new_path: Новый путь к файлу
        """
        if old_path in self._cache:
            self._cache[new_path] = self._cache.pop(old_path)

    def simulate_unlink(self, filepath: Path) -> None:
        """Имитирует удаление файла для dry-run режима.

        Удаляет файл из кэша.

        Args:
            filepath: Путь к удаляемому файлу
        """
        self._cache.pop(filepath, None)
