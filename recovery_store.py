"""Служебные записи операций и блокировка выбранного каталога."""

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from file_state import FileIdentity, check_directory, regular_file_info

DIRECTORY_NAME = '.clean_music-recovery'
_MARKER = b'clean_music recovery v1\n'
_RECORD_NAME = re.compile(r'[0-9a-f]{32}\.json')
_DRAFT_NAME = re.compile(r'[0-9a-f]{32}\.pending')
_ARTIFACT_NAME = re.compile(
    r'([0-9a-f]{32})\.(?:original|work\.(?:mp3|m4a|mp4|flac|ogg|wma))'
)


def _identity(info: os.stat_result) -> FileIdentity:
    if not info.st_ino:
        raise OSError('физический идентификатор недоступен')
    return info.st_dev, info.st_ino


def _directory_identity(path: Path) -> FileIdentity:
    check_directory(path)
    return _identity(path.lstat())


def _encode(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('ascii')


def _write_new(path: Path, data: bytes) -> None:
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_regular(path: Path) -> bytes:
    expected = _identity(regular_file_info(path))
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != expected:
            raise OSError(f'файл подменён: {path}')
        return stream.read()


class RecoveryStore:
    def __init__(self, root: Path, *, diagnostic: bool = False) -> None:
        self.root = root.absolute()
        self.directory = self.root / DIRECTORY_NAME
        self.root_id = _directory_identity(self.root)
        self._diagnostic = diagnostic
        self._directory_id: FileIdentity | None = None
        self._locked = False

    def _open_directory(self) -> bool:
        try:
            self.directory.lstat()
        except FileNotFoundError:
            if self._diagnostic:
                return False
            try:
                self.directory.mkdir()
            except FileExistsError:
                pass
            else:
                _write_new(self.directory / 'owner', _MARKER)
        directory_id = _directory_identity(self.directory)
        if _read_regular(self.directory / 'owner') != _MARKER:
            raise OSError('каталог восстановления не опознан')
        self._directory_id = directory_id
        self.check()
        return True

    def check(self) -> None:
        if (
            _directory_identity(self.root) != self.root_id
            or _directory_identity(self.directory) != self._directory_id
        ):
            raise OSError('каталог восстановления или коллекции подменён')

    @contextmanager
    def session(self) -> Iterator[bool]:
        """В обычном режиме удерживает блокировку до закрытия дескриптора."""
        if self._diagnostic:
            yield self._open_directory()
            return
        if sys.platform != 'win32':
            raise OSError('запись состояния восстановления доступна только в Windows')
        import msvcrt

        self._open_directory()
        lock = self.directory / 'lock'
        try:
            with lock.open('xb'):
                pass
        except FileExistsError:
            pass
        expected = _identity(regular_file_info(lock))
        with lock.open('r+b') as stream:
            if _identity(os.fstat(stream.fileno())) != expected:
                raise OSError('файл блокировки подменён')
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OSError(
                    'не удалось получить блокировку каталога (возможен активный запуск)'
                ) from exc
            self.check()
            self._locked = True
            try:
                yield True
            finally:
                self._locked = False

    def records(self) -> list[Path]:
        self.check()
        records = []
        entries = list(self.directory.iterdir())
        names = {entry.name for entry in entries}
        for entry in entries:
            if _RECORD_NAME.fullmatch(entry.name):
                records.append(entry)
            elif entry.name in ('owner', 'lock') or _DRAFT_NAME.fullmatch(entry.name):
                regular_file_info(entry)
            elif (
                artifact := _ARTIFACT_NAME.fullmatch(entry.name)
            ) and f'{artifact[1]}.json' in names:
                continue
            else:
                raise OSError(f'неизвестный объект в каталоге восстановления: {entry}')
        return sorted(records)

    def read(self, path: Path) -> dict[str, object]:
        self.check()
        envelope = json.loads(_read_regular(path))
        if not isinstance(envelope, dict):
            raise ValueError('неверный формат записи')
        data = envelope.get('data')
        if (
            not isinstance(data, dict)
            or envelope.get('sha256') != hashlib.sha256(_encode(data)).hexdigest()
        ):
            raise ValueError('неверная контрольная сумма записи')
        return data

    def _require_lock(self) -> None:
        if not self._locked:
            raise OSError('нет блокировки каталога восстановления')
        self.check()

    def write(self, path: Path, data: dict[str, object], *, initial: bool) -> None:
        self._require_lock()
        draft = self.directory / f'{uuid4().hex}.pending'
        _write_new(
            draft,
            _encode(
                {'data': data, 'sha256': hashlib.sha256(_encode(data)).hexdigest()}
            ),
        )
        self.check()
        if initial:
            os.rename(draft, path)
        else:
            regular_file_info(path)
            draft.replace(path)

    def remove(self, path: Path) -> None:
        self._require_lock()
        regular_file_info(path)
        path.unlink()
