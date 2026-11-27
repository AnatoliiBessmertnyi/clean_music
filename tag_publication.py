"""Публикация проверенной рабочей копии и сверка после остановки процесса."""

import hashlib
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from audio_verification import audio_signature
from file_state import FileIdentity, inspect_file
from publication_recovery import (
    ObservedFile,
    PublicationDecision,
    PublicationPhase,
    reconcile_publication,
)
from recovery import Fingerprint, _valid_name
from recovery_store import RecoveryStore
from tag_ntfs import (
    Attributes,
    copy_protected,
    delete_open_file,
    guarded_file,
    move_open_file,
    read_attributes,
    restore_attributes,
)
from tag_repository import TagRepository


def _fingerprint(stream: BinaryIO) -> Fingerprint:
    before = Fingerprint._attributes(os.fstat(stream.fileno()))
    stream.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    if Fingerprint._attributes(os.fstat(stream.fileno())) != before:
        raise OSError('файл изменился при проверке')
    return Fingerprint(*before, digest.hexdigest())


def _attributes(data: object) -> Attributes:
    if not isinstance(data, dict):
        raise ValueError('отсутствуют атрибуты NTFS')
    security, flags, created, modified = (
        data.get(key) for key in ('security', 'flags', 'created', 'modified')
    )
    if not isinstance(security, str) or not all(
        isinstance(value, int) for value in (flags, created, modified)
    ):
        raise ValueError('неверные атрибуты NTFS')
    assert (
        isinstance(flags, int)
        and isinstance(created, int)
        and isinstance(modified, int)
    )
    return Attributes(security, flags, created, modified)


@dataclass(frozen=True)
class TagIntent:
    source: str
    original: Fingerprint
    attributes: Attributes
    phase: PublicationPhase = PublicationPhase.COPYING
    work_id: FileIdentity | None = None
    ready: Fingerprint | None = None

    @classmethod
    def decode(
        cls, store: RecoveryStore, record: Path, data: dict[str, object]
    ) -> 'TagIntent':
        if (
            data.get('version') != 1
            or data.get('kind') != 'tags'
            or data.get('operation') != record.stem
            or (os.name == 'nt' and data.get('root') != list(store.root_id))
        ):
            raise ValueError('неизвестная или чужая запись тегов')
        try:
            phase = PublicationPhase[str(data['phase'])]
        except KeyError as exc:
            raise ValueError('неизвестная фаза записи тегов') from exc
        source = _valid_name(data.get('source'))
        original = Fingerprint.decode(data.get('original'))
        attributes = _attributes(data.get('attributes'))
        raw_id = data.get('work_id')
        work_id = None
        if raw_id is not None:
            if (
                not isinstance(raw_id, list)
                or len(raw_id) != 2
                or not all(type(n) is int for n in raw_id)
                or raw_id[1] <= 0
            ):
                raise ValueError('неверный ID рабочей копии')
            work_id = (raw_id[0], raw_id[1])
            if work_id[0] != original.volume or work_id[1] == original.file_id:
                raise ValueError('рабочая копия не является независимой')
        ready = None if data.get('ready') is None else Fingerprint.decode(data['ready'])
        if ready is not None and (ready.volume, ready.file_id) != work_id:
            raise ValueError('отпечаток относится к другому файлу')
        if (
            phase
            in (
                PublicationPhase.READY,
                PublicationPhase.PUBLISHING,
                PublicationPhase.CONFIRMED,
            )
            and ready is None
        ):
            raise ValueError('отсутствует отпечаток готовой копии')
        return cls(source, original, attributes, phase, work_id, ready)

    def write(
        self, store: RecoveryStore, record: Path, *, initial: bool = False
    ) -> None:
        store.write(
            record,
            {
                'version': 1,
                'kind': 'tags',
                'operation': record.stem,
                'root': list(store.root_id),
                'source': self.source,
                'original': asdict(self.original),
                'attributes': asdict(self.attributes),
                'phase': self.phase.name,
                'work_id': self.work_id,
                'ready': asdict(self.ready) if self.ready else None,
            },
            initial=initial,
        )

    def paths(self, store: RecoveryStore, record: Path) -> tuple[Path, Path, Path]:
        ext = Path(self.source).suffix.lower()
        if ext not in ('.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.wma'):
            raise ValueError('неподдерживаемый путь записи тегов')
        return (
            store.root / self.source,
            store.directory / f'{record.stem}.work{ext}',
            store.directory / f'{record.stem}.original',
        )

    def expected(self, fingerprint: Fingerprint) -> Attributes:
        return replace(self.attributes, modified=fingerprint.modified_ns)

    def observe(
        self, path: Path, *, work: bool = False, diagnostic: bool = False
    ) -> ObservedFile:
        try:
            identity = inspect_file(path)
        except FileNotFoundError:
            return ObservedFile.ABSENT
        except OSError:
            return ObservedFile.UNKNOWN
        try:
            if work and self.ready is None and identity == self.work_id:
                return ObservedFile.OWNED_INCOMPLETE_COPY
            if diagnostic and os.name != 'nt':
                return ObservedFile.UNKNOWN
            with guarded_file(path) as (handle, stream):
                actual = _fingerprint(stream)
                if (
                    actual == self.original
                    and read_attributes(handle, stream) == self.attributes
                ):
                    return ObservedFile.ORIGINAL
                if (
                    self.ready is not None
                    and actual == self.ready
                    and read_attributes(handle, stream) == self.expected(self.ready)
                ):
                    return ObservedFile.VERIFIED_COPY
        except OSError:
            pass
        return ObservedFile.UNKNOWN


def _verify(
    handle: int, stream: BinaryIO, fingerprint: Fingerprint, expected: Attributes
) -> None:
    if _fingerprint(stream) != fingerprint:
        raise OSError('содержимое или физическая идентичность файла изменились')
    observed = read_attributes(handle, stream)
    if observed != expected:
        fields = ', '.join(
            field
            for field in ('security', 'flags', 'created', 'modified')
            if getattr(observed, field) != getattr(expected, field)
        )
        raise OSError(f'атрибуты NTFS отличаются: {fields}')


def settle_tags(
    store: RecoveryStore, record: Path, intent: TagIntent, *, diagnostic: bool = False
) -> bool:
    source, work, saved = intent.paths(store, record)
    store.check()
    observed_work = intent.observe(work, work=True, diagnostic=diagnostic)
    observed_source = intent.observe(source, diagnostic=diagnostic)
    observed_saved = intent.observe(saved, diagnostic=diagnostic)
    decision = reconcile_publication(
        intent.phase, observed_source, observed_work, observed_saved
    )
    if decision is PublicationDecision.STOP:
        states = {
            ObservedFile.ABSENT: 'отсутствует',
            ObservedFile.ORIGINAL: 'оригинал подтверждён',
            ObservedFile.VERIFIED_COPY: 'копия проверена',
            ObservedFile.OWNED_INCOMPLETE_COPY: 'незавершённая копия с подтверждённым ID',
            ObservedFile.UNKNOWN: 'принадлежность не подтверждена',
        }
        work_id = 'не зафиксирован' if intent.work_id is None else str(intent.work_id)
        raise OSError(
            f'STOP: требуется ручная проверка восстановления тегов. '
            f'Фаза {intent.phase.name}, ID рабочей копии в журнале: {work_id}. '
            f'Исходный путь {source} — {states[observed_source]}. '
            f'Рабочая копия {work} — {states[observed_work]}. '
            f'Резервный путь {saved} — {states[observed_saved]}.'
        )
    if diagnostic:
        return False
    if decision is PublicationDecision.RESTORE_ORIGINAL:
        with guarded_file(saved, delete=True) as (handle, stream):
            _verify(handle, stream, intent.original, intent.attributes)
            store.check()
            move_open_file(handle, source)
        return settle_tags(store, record, intent)
    if decision in (
        PublicationDecision.CANCEL,
        PublicationDecision.CANCEL_AND_CLEAN_COPY,
    ):
        with guarded_file(source) as (origin, original_stream):
            _verify(origin, original_stream, intent.original, intent.attributes)
            if intent.phase is not PublicationPhase.CANCELLED:
                intent = replace(intent, phase=PublicationPhase.CANCELLED)
                intent.write(store, record)
            if decision is PublicationDecision.CANCEL_AND_CLEAN_COPY:
                with guarded_file(work, delete=True) as (handle, stream):
                    info = os.fstat(stream.fileno())
                    if (info.st_dev, info.st_ino) != intent.work_id:
                        raise OSError('рабочая копия подменена')
                    if intent.ready is not None:
                        _verify(
                            handle, stream, intent.ready, intent.expected(intent.ready)
                        )
                    store.check()
                    delete_open_file(handle)
    if decision is PublicationDecision.CONFIRM_AND_CLEAN_ORIGINAL:
        ready = intent.ready
        assert ready is not None
        expected = intent.expected(ready)
        with guarded_file(source) as (published, result):
            _verify(published, result, ready, expected)
            with guarded_file(saved, delete=True) as (handle, stream):
                _verify(handle, stream, intent.original, intent.attributes)
                if intent.phase is not PublicationPhase.CONFIRMED:
                    intent = replace(intent, phase=PublicationPhase.CONFIRMED)
                    intent.write(store, record)
                _verify(handle, stream, intent.original, intent.attributes)
                if read_attributes(published, result) != expected:
                    raise OSError(
                        'права или атрибуты результата изменились перед очисткой'
                    )
                store.check()
                delete_open_file(handle)
    store.remove(record)
    return intent.phase is PublicationPhase.CONFIRMED


class PublicationError(OSError):
    def __init__(self, reason: str, confirmed: FileIdentity | None = None) -> None:
        super().__init__(reason)
        self.confirmed = confirmed


def _publish_tags(
    store: RecoveryStore,
    source: Path,
    identity: FileIdentity,
    repo: TagRepository,
    command: str,
    artist: str | None,
    title: str | None,
    record: Path,
) -> FileIdentity:
    store.check()
    with guarded_file(source) as (handle, stream):
        original = _fingerprint(stream)
        if (original.volume, original.file_id) != identity:
            raise OSError('оригинал подменён')
        intent = TagIntent(source.name, original, read_attributes(handle, stream))
        _, work, saved = intent.paths(store, record)
        intent.write(store, record, initial=True)

        def register(work_id: FileIdentity) -> None:
            nonlocal intent
            if work_id[0] != original.volume or work_id == identity or not work_id[1]:
                raise OSError('рабочая копия не является независимой')
            next_intent = replace(intent, work_id=work_id)
            next_intent.write(store, record)
            intent = next_intent

        copy_protected(stream, work, intent.attributes, register)
        audio = audio_signature(command, source)
        tags = repo.tag_snapshot(source, artist, title)
        with guarded_file(work, write=True) as (work_handle, work_stream):
            before = _fingerprint(work_stream)
            if (
                before.volume,
                before.file_id,
            ) != intent.work_id or before.sha256 != original.sha256:
                raise OSError('содержимое или идентичность рабочей копии отличается')
            repo.save_work_tags(work, work_stream, artist, title)
            work_stream.flush()
            os.fsync(work_stream.fileno())
            restore_attributes(work_handle, intent.attributes)
        with guarded_file(work) as (work_handle, work_stream):
            ready = _fingerprint(work_stream)
            if (ready.volume, ready.file_id) != intent.work_id:
                raise OSError('рабочая копия подменена')
            if repo.tag_snapshot(work, artist, title, result=True) != tags:
                raise OSError('проверка целевых и сохраняемых тегов не пройдена')
            if audio_signature(command, work) != audio:
                raise OSError('аудиоданные рабочей копии изменились')
            _verify(work_handle, work_stream, ready, intent.expected(ready))
            intent = replace(intent, phase=PublicationPhase.READY, ready=ready)
            intent.write(store, record)
    with (
        guarded_file(source, delete=True) as (original_handle, original_stream),
        guarded_file(work, delete=True) as (work_handle, work_stream),
    ):
        _verify(original_handle, original_stream, original, intent.attributes)
        _verify(work_handle, work_stream, ready, intent.expected(ready))
        intent = replace(intent, phase=PublicationPhase.PUBLISHING)
        intent.write(store, record)
        store.check()
        move_open_file(original_handle, saved)
        store.check()
        move_open_file(work_handle, source)
    try:
        settle_tags(store, record, intent)
    except OSError as exc:
        saved_intent = TagIntent.decode(store, record, store.read(record))
        confirmed = (
            intent.work_id if saved_intent.phase is PublicationPhase.CONFIRMED else None
        )
        raise PublicationError(str(exc), confirmed) from exc
    assert intent.work_id is not None
    return intent.work_id


def publish_tags(
    store: RecoveryStore,
    source: Path,
    identity: FileIdentity,
    repo: TagRepository,
    command: str,
    artist: str | None,
    title: str | None,
) -> FileIdentity:
    if source.parent != store.root:
        raise OSError('путь находится за пределами каталога восстановления')
    record = store.directory / f'{uuid4().hex}.json'
    try:
        return _publish_tags(
            store, source, identity, repo, command, artist, title, record
        )
    except PublicationError:
        raise
    except OSError as exc:
        try:
            data = store.read(record)
        except FileNotFoundError:
            raise exc from None
        except (OSError, ValueError) as read_error:
            raise PublicationError(
                f'{exc}. Не удалось прочитать журнал: {read_error}'
            ) from exc
        try:
            intent = TagIntent.decode(store, record, data)
            if settle_tags(store, record, intent):
                assert intent.work_id is not None
                return intent.work_id
        except (OSError, ValueError) as recovery_error:
            raise PublicationError(f'{exc}. Восстановление: {recovery_error}') from exc
        raise
