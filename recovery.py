"""Подтверждение незавершённого одношагового переименования по диску."""

import hashlib
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from context import SessionContext
from file_state import FileIdentity, regular_file_info
from interactor import UserInteractor
from recovery_store import DIRECTORY_NAME, RecoveryStore
from report_events import log_operation


@dataclass(frozen=True)
class Fingerprint:
    volume: int
    file_id: int
    created_ns: int
    size: int
    modified_ns: int
    sha256: str

    @staticmethod
    def _attributes(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            getattr(info, 'st_birthtime_ns', 0),
            info.st_size,
            info.st_mtime_ns,
        )

    @classmethod
    def capture(cls, path: Path, identity: FileIdentity) -> 'Fingerprint':
        before = cls._attributes(regular_file_info(path))
        if before[:2] != identity:
            raise OSError(f'физический объект подменён: {path}')
        with path.open('rb') as stream:
            if cls._attributes(os.fstat(stream.fileno())) != before:
                raise OSError(f'файл изменился перед чтением: {path}')
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if (
                cls._attributes(os.fstat(stream.fileno())) != before
                or cls._attributes(regular_file_info(path)) != before
            ):
                raise OSError(f'файл изменился во время чтения: {path}')
        return cls(*before, digest)

    @classmethod
    def decode(cls, data: object) -> 'Fingerprint':
        if not isinstance(data, dict):
            raise ValueError('отсутствует отпечаток оригинала')

        def number(key: str) -> int:
            value = data.get(key)
            if type(value) is not int:
                raise ValueError('повреждён отпечаток оригинала')
            return value

        digest = data.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('повреждён отпечаток оригинала')
        return cls(
            number('volume'),
            number('file_id'),
            number('created_ns'),
            number('size'),
            number('modified_ns'),
            digest,
        )


def _valid_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.endswith(('.', ' '))
        or value.casefold() == DIRECTORY_NAME.casefold()
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in value)
        or (sys.platform == 'win32' and os.path.isreserved(value))
    ):
        raise ValueError('небезопасное имя в записи восстановления')
    return value


@dataclass(frozen=True)
class RenameIntent:
    source: str
    target: str
    original: Fingerprint
    outcome: str | None = None

    @classmethod
    def decode(
        cls, store: RecoveryStore, record: Path, data: dict[str, object]
    ) -> 'RenameIntent':
        if (
            type(data.get('version')) is not int
            or data.get('version') != 1
            or data.get('kind') != 'rename'
            or data.get('operation') != record.stem
            or (os.name == 'nt' and data.get('root') != list(store.root_id))
        ):
            raise ValueError('неизвестная или чужая запись восстановления')
        source, target = (
            _valid_name(data.get('source')),
            _valid_name(data.get('target')),
        )
        outcome = data.get('outcome')
        if source == target or (
            outcome is not None and outcome not in ('original', 'renamed')
        ):
            raise ValueError('некорректное состояние rename')
        return cls(source, target, Fingerprint.decode(data.get('original')), outcome)

    def encode(
        self, store: RecoveryStore, record: Path, outcome: str | None
    ) -> dict[str, object]:
        return {
            'version': 1,
            'kind': 'rename',
            'operation': record.stem,
            'root': list(store.root_id),
            'source': self.source,
            'target': self.target,
            'original': asdict(self.original),
            'outcome': outcome,
        }

    def inspect(self, root: Path) -> tuple[str | None, str, bool]:
        names = {entry.name for entry in root.iterdir()}

        def observe(name: str, other: str) -> str:
            if name not in names:
                if any(
                    entry != other and entry.casefold() == name.casefold()
                    for entry in names
                ):
                    return 'неожиданное написание имени'
                return 'отсутствует'
            try:
                path = root / name
                info = regular_file_info(path)
                identity = info.st_dev, info.st_ino
                if not info.st_ino:
                    return 'нет идентификатора файла'
                if identity != (self.original.volume, self.original.file_id):
                    return 'другой объект'
                if Fingerprint.capture(path, identity) == self.original:
                    return 'подтверждён'
                return 'содержимое или атрибуты изменились'
            except OSError as exc:
                return f'проверка не удалась ({exc})'

        source = observe(self.source, self.target)
        target = observe(self.target, self.source)
        detail = f'{self.source}: {source}, {self.target}: {target}'
        outcome = None
        if source == 'подтверждён' and target in ('отсутствует', 'другой объект'):
            outcome = 'original'
        if target == 'подтверждён' and source in ('отсутствует', 'другой объект'):
            outcome = 'renamed'
        if self.outcome is not None and self.outcome != outcome:
            return None, f'{detail}. Итог расходится с записью', True
        return (
            outcome,
            detail,
            outcome is None or (outcome == 'original' and target == 'другой объект'),
        )


def begin_rename(
    store: RecoveryStore, source: Path, target: Path, identity: FileIdentity
) -> tuple[Path, RenameIntent]:
    if source.absolute().parent != store.root or target.absolute().parent != store.root:
        raise OSError('переименование вне каталога восстановления')
    intent = RenameIntent(
        _valid_name(source.name),
        _valid_name(target.name),
        Fingerprint.capture(source, identity),
    )
    record = store.directory / f'{uuid4().hex}.json'
    store.write(record, intent.encode(store, record, None), initial=True)
    return record, intent


def settle_rename(
    store: RecoveryStore,
    record: Path,
    intent: RenameIntent,
    ctx: SessionContext,
    interactor: UserInteractor,
    *,
    diagnostic: bool = False,
    expected: str | None = None,
) -> str | None:
    outcome, detail, blocked = intent.inspect(store.root)
    if diagnostic:
        detail = f'диагностика без блокировки, снимок может меняться. {detail}'
    if expected is not None and outcome != expected:
        outcome, blocked = None, True
    message = f'Восстановление rename ({record.stem}): {detail}'
    interactor.display(message)
    log_operation(
        'recovery',
        store.root / intent.source,
        outcome or 'неопределённость',
        message,
        target=store.root / intent.target,
        proposed=diagnostic,
    )
    if (blocked and (outcome is None or expected is None)) or diagnostic:
        for name in (intent.source, intent.target):
            ctx.block_path(store.root / name, message)
    if outcome is not None and not diagnostic:
        if intent.outcome is None:
            store.write(record, intent.encode(store, record, outcome), initial=False)
        store.remove(record)
    return outcome


def recover_renames(
    store: RecoveryStore,
    ctx: SessionContext,
    interactor: UserInteractor,
    *,
    diagnostic: bool = False,
) -> None:
    from tag_publication import TagIntent, settle_tags

    records: list[tuple[Path, RenameIntent | TagIntent]] = []
    for record in store.records():
        try:
            data = store.read(record)
            if data.get('kind') == 'tags':
                tags = TagIntent.decode(store, record, data)
                tags.paths(store, record)
                records.append((record, tags))
            else:
                records.append((record, RenameIntent.decode(store, record, data)))
        except (OSError, ValueError) as exc:
            raise OSError(f'{record}: границы операции неизвестны ({exc})') from exc
    for record, intent in records:
        try:
            if isinstance(intent, TagIntent):
                settle_tags(store, record, intent, diagnostic=diagnostic)
            else:
                settle_rename(
                    store, record, intent, ctx, interactor, diagnostic=diagnostic
                )
        except OSError as exc:
            message = f'Восстановление {record}: {exc}'
            interactor.display_error(message)
            names = (
                (intent.source,)
                if isinstance(intent, TagIntent)
                else (intent.source, intent.target)
            )
            for name in names:
                ctx.block_path(store.root / name, message)
