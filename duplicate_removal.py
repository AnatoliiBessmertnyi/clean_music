"""Подтверждение и исполнение решения о точных дублях на Windows/NTFS."""

import os
import stat
from collections.abc import Generator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from duplicate_analysis import (
    ByteStatus,
    CandidateGroup,
    compare_candidates,
    compare_streams,
    decide_copies,
)
from file_state import FileRegistry, OperationStatus
from tag_ntfs import (
    delete_open_file,
    guarded_directory,
    guarded_file,
    require_no_ads,
    require_path_binding,
    require_single_live_link,
)
from tag_repository import TagRepository

type Stamp = tuple[int, int, int, int]


def file_stamp(info: os.stat_result) -> Stamp:
    # В CPython на Windows lstat и fstat могут возвращать разные st_ctime
    # для одного объекта, тогда как st_mtime сравним между этими вызовами.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _verify(path: Path, opened: tuple[int, BinaryIO], expected: Stamp) -> None:
    handle, stream = opened
    info = os.fstat(stream.fileno())
    require_path_binding(path, handle)
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_ino
        or file_stamp(info) != expected
    ):
        raise OSError(f'изменился или не допущен файловый объект: {path}')
    require_single_live_link(handle)
    require_no_ads(
        handle,
        'удаление дубля остановлено: при сравнении учитывается только основной поток '
        f'(файл с именованным потоком: {path.name})',
    )


def remove_exact_copies(
    group: CandidateGroup,
    winner: Path,
    copies: tuple[Path, ...],
    expected: Mapping[Path, Stamp],
    registry: FileRegistry,
    repo: TagRepository,
) -> Generator[tuple[Path, OperationStatus, str]]:
    """Держит защиту всей группы и прекращает удаление при первом сбое."""
    folder = winner.parent.absolute()
    opened: dict[Path, tuple[int, BinaryIO]] = {}
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(guarded_directory(folder))
            for candidate in group.candidates:
                path = candidate.path
                record = registry.track(path)
                if (
                    path.parent.absolute() != folder
                    or not record.present
                    or record.path != path
                    or record.modeled_content
                    or record.identity != expected[path][:2]
                ):
                    raise OSError(f'изменилось состояние участника группы: {path}')
                redundant = path in copies
                handles = stack.enter_context(
                    guarded_file(path.absolute(), delete=redundant, exclusive=redundant)
                )
                opened[path] = handles
                _verify(path, handles, expected[path])
                if repo.read_open_tags(path, handles[1]) != candidate.metadata:
                    raise OSError(f'данные анализа изменились: {path}')
            if len({stamp[:2] for stamp in expected.values()}) != len(opened):
                raise OSError('в группе обнаружены алиасы одного файла')

            pairs = list(
                compare_candidates(
                    group,
                    registry,
                    compare=lambda left, right: compare_streams(
                        opened[left][1], opened[right][1]
                    ),
                )
            )
            if any(
                pair.byte_status in (ByteStatus.ERROR, ByteStatus.UNKNOWN)
                for pair in pairs
            ):
                raise OSError('сравнение всех участников группы не завершено')
            fresh = tuple(
                (item.keep, item.redundant)
                for item in decide_copies(group, pairs)
                if item.redundant is not None
            )
            if fresh != tuple((winner, copy) for copy in copies):
                raise OSError('решение о сохраняемой копии изменилось')
        except OSError as exc:
            yield copies[0], OperationStatus.ERROR, str(exc)
            return

        for copy in copies:
            handle, stream = opened[copy]
            try:
                status, reason = compare_streams(opened[winner][1], stream)
                if status is not ByteStatus.IDENTICAL:
                    raise OSError(reason or str(status))
                require_path_binding(folder, directory, directory=True)
                _verify(winner, opened[winner], expected[winner])
                _verify(copy, opened[copy], expected[copy])
                delete_open_file(handle)
            except OSError as exc:
                yield copy, OperationStatus.ERROR, str(exc)
                return

            try:
                stream.close()
                try:
                    copy.lstat()
                except FileNotFoundError as exc:
                    if getattr(exc, 'winerror', None) != 2:
                        raise
                else:
                    raise OSError(f'имя кандидата осталось занято: {copy}')
                require_path_binding(folder, directory, directory=True)
                _verify(winner, opened[winner], expected[winner])
            except OSError as exc:
                yield copy, OperationStatus.UNCERTAIN, str(exc)
                return
            yield copy, OperationStatus.SUCCESS, 'удаление по дескриптору подтверждено'
