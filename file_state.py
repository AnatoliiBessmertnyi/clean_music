"""Результаты операций и текущее состояние файлов в сессии."""

import os
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path


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


class FileRegistry:
    """Хранит пути файлов и их сессионные идентификаторы в памяти."""

    def __init__(self) -> None:
        self._records: dict[int, tuple[FileRecord, Path]] = {}
        self._ids_by_path: dict[Path, int] = {}

    @staticmethod
    def _path_key(path: Path) -> Path:
        return Path(os.path.normcase(path.absolute()))

    def register(self, path: Path) -> FileRecord:
        key = self._path_key(path)
        file_id = self._ids_by_path.get(key)
        if file_id is not None:
            return self._records[file_id][0]

        record = FileRecord(len(self._records) + 1, path, present=True)
        self._records[record.id] = (record, key)
        self._ids_by_path[key] = record.id
        return record

    def id_for(self, path: Path) -> int | None:
        return self._ids_by_path.get(self._path_key(path))

    def path_of(self, file_id: int) -> Path:
        return self._records[file_id][0].path

    def update_path(self, file_id: int, new_path: Path) -> None:
        record, old_key = self._records[file_id]
        new_key = self._path_key(new_path)
        owner = self._ids_by_path.get(new_key)
        if owner is not None and owner != file_id:
            raise ValueError(f'Путь уже зарегистрирован: {new_path}')

        updated = replace(record, path=new_path)
        if new_key != old_key:
            self._ids_by_path[new_key] = file_id
            del self._ids_by_path[old_key]
        self._records[file_id] = (updated, new_key)

    def mark_missing(self, file_id: int) -> None:
        record, key = self._records[file_id]
        self._records[file_id] = (replace(record, present=False), key)

    def current_paths(self) -> list[Path]:
        return [record.path for record, _ in self._records.values() if record.present]
