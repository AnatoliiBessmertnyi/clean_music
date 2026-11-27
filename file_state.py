"""Результаты операций и текущее состояние файлов в сессии."""

import ctypes
import os
import stat
import sys
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path

type FileIdentity = tuple[int, int]


def entry_info(path: Path) -> os.stat_result:
    """Проверяет запись каталога перед lstat, который может раскрыть reparse point."""
    if sys.platform == 'win32':
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attributes == -1:
            raise ctypes.WinError()
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f'ссылка или reparse point: {path}')
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError(f'ссылка или reparse point: {path}')
    return info


def check_directory(directory: Path) -> None:
    absolute = directory.absolute()
    for entry in (*reversed(absolute.parents), absolute):
        if not stat.S_ISDIR(entry_info(entry).st_mode):
            raise OSError(f'не является каталогом: {entry}')


def regular_file_info(path: Path) -> os.stat_result:
    absolute = path.absolute()
    check_directory(absolute.parent)
    info = entry_info(absolute)
    if not stat.S_ISREG(info.st_mode):
        raise OSError('не является обычным файлом')
    if info.st_nlink != 1:
        raise OSError('требуется ровно одна жёсткая ссылка')
    return info


def inspect_file(path: Path) -> FileIdentity:
    info = regular_file_info(path)
    if not info.st_ino:
        raise OSError('физический идентификатор файла недоступен')
    return info.st_dev, info.st_ino


class OperationStatus(Enum):
    SUCCESS = auto()
    NOOP = auto()
    SIMULATED = auto()
    CONFLICT = auto()
    ERROR = auto()
    UNCERTAIN = auto()


@dataclass(frozen=True)
class OperationResult:
    status: OperationStatus
    path: Path | None
    reason: str

    @property
    def is_confirmed_change(self) -> bool:
        return self.status is OperationStatus.SUCCESS

    @property
    def is_model_change(self) -> bool:
        return self.status is OperationStatus.SIMULATED

    @property
    def blocks_dependents(self) -> bool:
        return self.status is OperationStatus.UNCERTAIN


@dataclass(frozen=True)
class FileRecord:
    id: int
    path: Path
    present: bool
    identity: FileIdentity | None = None
    modeled_content: bool = False


class FileRegistry:
    """Хранит пути файлов и их сессионные идентификаторы в памяти."""

    def __init__(self) -> None:
        self._records: dict[int, tuple[FileRecord, Path]] = {}
        self._ids_by_path: dict[Path, int] = {}
        self._previous_ids: dict[Path, int] = {}
        self._model_sources: dict[int, Path] = {}
        self._vacated_sources: set[Path] = set()

    @staticmethod
    def _path_key(path: Path) -> Path:
        return Path(os.path.normcase(path.absolute()))

    def register(
        self, path: Path, *, identity: FileIdentity | None = None
    ) -> FileRecord:
        """Идемпотентно возвращает присутствующую запись или выдаёт новый id."""
        key = self._path_key(path)
        file_id = self._ids_by_path.get(key)
        if file_id is not None:
            return self._records[file_id][0]

        record = FileRecord(
            len(self._records) + 1, path, present=True, identity=identity
        )
        self._records[record.id] = (record, key)
        self._ids_by_path[key] = record.id
        return record

    def id_for(self, path: Path) -> int | None:
        """Возвращает id присутствующей записи по пути или None."""
        return self._ids_by_path.get(self._path_key(path))

    def track(self, path: Path) -> FileRecord:
        """Разрешает входной путь, включая прежний путь и пропавшую запись."""
        key = self._path_key(path)
        file_id = self._ids_by_path.get(key, self._previous_ids.get(key))
        if file_id is None:
            return self.register(path)
        return self._records[file_id][0]

    def record_of(self, file_id: int) -> FileRecord:
        return self._records[file_id][0]

    def physical_path(self, record: FileRecord) -> Path:
        return self._model_sources.get(record.id, record.path)

    def source_vacated(self, path: Path) -> bool:
        """Путь физического источника, освобождённый модельной операцией."""
        return self._path_key(path) in self._vacated_sources

    def mark_content_modeled(self, file_id: int) -> None:
        record, key = self._records[file_id]
        self._records[file_id] = (replace(record, modeled_content=True), key)

    def path_of(self, file_id: int) -> Path:
        """Возвращает последний известный путь или вызывает KeyError для неизвестного id."""
        return self._records[file_id][0].path

    def update_path(
        self, file_id: int, new_path: Path, *, simulated: bool = False
    ) -> None:
        """Атомарно меняет путь (KeyError для неизвестного id, ValueError при отсутствии файла или конфликте путей)."""
        record, old_key = self._records[file_id]
        if not record.present:
            raise ValueError(f'Файл помечен как пропавший: {file_id}')
        new_key = self._path_key(new_path)
        owner = self._ids_by_path.get(new_key)
        if owner is not None and owner != file_id:
            raise ValueError(f'Путь уже зарегистрирован: {new_path}')

        updated = replace(record, path=new_path)
        if new_key != old_key:
            self._ids_by_path[new_key] = file_id
            del self._ids_by_path[old_key]
            self._previous_ids[old_key] = file_id
        if simulated:
            source = self._model_sources.setdefault(file_id, record.path)
            if new_key != self._path_key(source):
                self._vacated_sources.add(self._path_key(source))
        else:
            self._model_sources.pop(file_id, None)
        self._records[file_id] = (updated, new_key)

    def confirm_replacement(self, file_id: int, identity: FileIdentity) -> None:
        record, key = self._records[file_id]
        self._records[file_id] = (replace(record, identity=identity), key)

    def mark_missing(self, file_id: int, *, simulated: bool = False) -> None:
        """Идемпотентно освобождает ключ пути (KeyError для неизвестного id)."""
        record, key = self._records[file_id]
        if record.present:
            if simulated:
                self._vacated_sources.add(self._path_key(self.physical_path(record)))
            self._records[file_id] = (replace(record, present=False), key)
            del self._ids_by_path[key]
            self._previous_ids[key] = file_id
            self._model_sources.pop(file_id, None)

    def current_paths(self) -> list[Path]:
        """Возвращает пути присутствующих файлов в порядке регистрации."""
        return [record.path for record, _ in self._records.values() if record.present]
