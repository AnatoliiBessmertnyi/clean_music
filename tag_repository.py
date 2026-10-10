"""Репозиторий для работы с тегами аудиофайлов с кэшированием."""

from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis


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
        success = self._write_to_disk(filepath, artist, title)
        if success:
            self._cache[filepath] = AudioTags(artist, title)
        return success

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
        """Читает теги непосредственно с диска."""
        ext = filepath.suffix.lower()
        try:
            if ext == '.mp3':
                audio = MP3(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('TPE1')
                title = audio.tags.get('TIT2')
                if artist and title:
                    return AudioTags(str(artist), str(title))
            elif ext in ('.m4a', '.mp4'):
                audio = MP4(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('©ART')
                title = audio.tags.get('©nam')
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.flac':
                audio = FLAC(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('artist')
                title = audio.tags.get('title')
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.ogg':
                audio = OggVorbis(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('artist')
                title = audio.tags.get('title')
                if artist and title:
                    return AudioTags(artist[0], title[0])
            elif ext == '.wma':
                audio = ASF(filepath)  # type: ignore[assignment]
                if audio.tags is None:
                    return None
                artist = audio.tags.get('Author')
                title = audio.tags.get('Title')
                if artist and title:
                    return AudioTags(artist[0], title[0])
        except (MutagenError, OSError, ValueError):
            pass
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
            print(f'  Ошибка записи тегов: {e}')
        return False
