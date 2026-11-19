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


class TagRepository:
    """Репозиторий для работы с тегами аудиофайлов.

    Кэширует прочитанные теги для избежания многократного чтения с диска.
    Обновляет кэш при подтверждённой или моделируемой записи.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, AudioTags | None] = {}

    def get_tags(self, filepath: Path) -> AudioTags | None:
        """Возвращает теги файла, используя кэш при наличии.

        Args:
            filepath: Путь к аудиофайлу

        Returns:
            AudioTags с исполнителем и названием, или None если теги не найдены
        """
        key = filepath.absolute()
        if key not in self._cache:
            self._cache[key] = self._read_from_disk(filepath)
        return self._cache[key]

    def apply_tags(
        self, filepath: Path, artist: str, title: str, *, simulated: bool
    ) -> OperationResult:
        """Применяет запись тегов и публикует её результат."""
        tags = AudioTags(artist, title)
        key = filepath.absolute()
        if self._cache.get(key) == tags:
            return OperationResult(OperationStatus.NOOP, filepath, 'теги не изменились')
        if simulated:
            self.simulate_update_tags(filepath, artist, title)
            return OperationResult(
                OperationStatus.SIMULATED, filepath, 'запись запланирована'
            )

        status, reason = self._write_to_disk(filepath, artist, title)
        if status is OperationStatus.SUCCESS:
            self._cache[key] = tags
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
        )
        return OperationResult(status, filepath, reason)

    def update_tags(self, filepath: Path, artist: str, title: str) -> bool:
        """Записывает теги для вызывающих мест вне конвейера."""
        result = self.apply_tags(filepath, artist, title, simulated=False)
        return result.status in (OperationStatus.SUCCESS, OperationStatus.NOOP)

    def read_tags_uncached(self, filepath: Path) -> AudioTags | None:
        """Читает для отчёта с диска; ошибки не превращаются в отсутствие тегов."""
        return self._read_tags(filepath, report_errors=True)

    def invalidate(self, filepath: Path) -> None:
        """Удаляет файл из кэша.

        Args:
            filepath: Путь к файлу для удаления из кэша
        """
        self._cache.pop(filepath.absolute(), None)

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

    def _write_to_disk(
        self, filepath: Path, artist: str, title: str
    ) -> tuple[OperationStatus, str]:
        """Записывает теги непосредственно на диск."""
        ext = filepath.suffix.lower()
        saving = False
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
                    saving = True
                    audio.save()
                    return OperationStatus.SUCCESS, 'запись выбранных artist/title'
            elif ext in ('.m4a', '.mp4'):
                audio = MP4(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    audio.add_tags()
                if audio.tags is not None:
                    audio.tags['©ART'] = [artist]
                    audio.tags['©nam'] = [title]
                    saving = True
                    audio.save()
                    return OperationStatus.SUCCESS, 'запись выбранных artist/title'
            elif ext == '.flac':
                audio = FLAC(filepath)  # type: ignore[assignment]
                audio['artist'] = [artist]
                audio['title'] = [title]
                saving = True
                audio.save()
                return OperationStatus.SUCCESS, 'запись выбранных artist/title'
            elif ext == '.ogg':
                audio = OggVorbis(filepath)  # type: ignore[assignment]
                audio['artist'] = [artist]
                audio['title'] = [title]
                saving = True
                audio.save()
                return OperationStatus.SUCCESS, 'запись выбранных artist/title'
            elif ext == '.wma':
                audio = ASF(filepath)  # type: ignore[assignment]
                audio['Author'] = [artist]
                audio['Title'] = [title]
                saving = True
                audio.save()
                return OperationStatus.SUCCESS, 'запись выбранных artist/title'
        except (MutagenError, OSError, ValueError) as e:
            logger.error('Ошибка записи тегов %s: %s', filepath, e)
            status = OperationStatus.UNCERTAIN if saving else OperationStatus.ERROR
            return status, str(e) or 'запись тегов не подтверждена'
        return OperationStatus.ERROR, 'запись тегов недоступна для файла'

    def simulate_update_tags(self, filepath: Path, artist: str, title: str) -> None:
        """Имитирует обновление тегов для dry-run режима.

        Обновляет кэш без записи на диск, чтобы следующие этапы
        видели "новые" теги при сухом прогоне.

        Args:
            filepath: Путь к файлу
            artist: Новое имя исполнителя
            title: Новое название трека
        """
        self._cache[filepath.absolute()] = AudioTags(artist, title)
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
        old_key, new_key = old_path.absolute(), new_path.absolute()
        if old_key in self._cache:
            self._cache[new_key] = self._cache.pop(old_key)
        elif old_key != new_key:
            self.invalidate(new_path)

    def simulate_unlink(self, filepath: Path) -> None:
        """Имитирует удаление файла для dry-run режима.

        Удаляет файл из кэша.

        Args:
            filepath: Путь к удаляемому файлу
        """
        self.invalidate(filepath)
