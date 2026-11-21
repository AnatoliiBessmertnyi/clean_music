"""Поиск кандидатов и сравнение исходных признаков без изменения файлов."""

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from file_state import FileRegistry
from naming import normalize_filename_stem, normalize_unicode
from tag_repository import MetadataRead, TagRepository

_QUALIFIER = re.compile(r'\s*(?:\(([^()\[\]]+)\)|\[([^()\[\]]+)\])$')
_CHUNK_SIZE = 1024 * 1024


class MatchSource(StrEnum):
    TAGS = 'нормализованные исполнитель и название из тегов'
    NAME = 'нормализованное имя файла'


class ByteStatus(StrEnum):
    IDENTICAL = 'Побайтовое совпадение подтверждено'
    DIFFERENT = 'Байты различаются. Совпадение музыкальной записи не установлено'
    ERROR = 'Побайтовое сравнение не завершено'


@dataclass(frozen=True)
class Candidate:
    path: Path
    metadata: MetadataRead
    name_qualifiers: tuple[str, ...]
    tag_qualifiers: tuple[str, ...]

    @property
    def qualifier_disagreement(self) -> bool:
        tags = self.metadata.tags
        return bool(
            tags
            and tags.title
            and tuple(map(_normalize, self.name_qualifiers))
            != tuple(map(_normalize, self.tag_qualifiers))
        )

    def values(self) -> dict[str, str | int | float | None]:
        tags, info = self.metadata.tags, self.metadata.info
        return {
            'имя без расширения': self.path.stem,
            'исполнитель': (tags.artist or None) if tags else None,
            'название': (tags.title or None) if tags else None,
            'формат': info.format if info else None,
            'кодек': info.codec if info else None,
            'длительность (с)': info.duration if info else None,
            'битрейт (бит/с)': info.bitrate if info else None,
            'частота (Гц)': info.sample_rate if info else None,
            'каналы': info.channels if info else None,
            'разрядность (бит)': info.bits_per_sample if info else None,
        }


@dataclass(frozen=True)
class CandidateGroup:
    source: MatchSource
    key: tuple[str, ...]
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class PairAnalysis:
    left: Candidate
    right: Candidate
    differences: tuple[str, ...]
    missing: tuple[str, ...]
    byte_status: ByteStatus
    byte_error: str | None


def _normalize(text: str) -> str:
    return normalize_unicode(normalize_filename_stem(text)).casefold()


def _split_qualifiers(text: str) -> tuple[str, tuple[str, ...]]:
    base = text.rstrip()
    qualifiers = []
    while (match := _QUALIFIER.search(base)) is not None:
        qualifiers.append(match[1] or match[2])
        base = base[: match.start()].rstrip()
    return base or text, tuple(reversed(qualifiers))


def find_candidate_groups(
    files: list[Path], repo: TagRepository
) -> list[CandidateGroup]:
    groups: dict[tuple[MatchSource, tuple[str, ...]], list[Candidate]] = {}
    for path in dict.fromkeys(files):
        metadata = repo.get_tags(path)
        tags = metadata.tags
        name_base, name_qualifiers = _split_qualifiers(path.stem)
        title, tag_qualifiers = _split_qualifiers(tags.title if tags else '')
        complete = metadata.complete_tags
        key: tuple[str, ...]
        if complete:
            source = MatchSource.TAGS
            key = (_normalize(complete.artist), _normalize(title))
        else:
            source = MatchSource.NAME
            key = (_normalize(name_base),)
        groups.setdefault((source, key), []).append(
            Candidate(path, metadata, name_qualifiers, tag_qualifiers)
        )
    return [
        CandidateGroup(source, key, tuple(candidates))
        for (source, key), candidates in groups.items()
        if len(candidates) > 1
    ]


def _compare_bytes(left: Path, right: Path) -> tuple[ByteStatus, str | None]:
    try:
        with left.open('rb') as first, right.open('rb') as second:
            before = (os.fstat(first.fileno()), os.fstat(second.fileno()))
            while True:
                first_chunk = first.read(_CHUNK_SIZE)
                second_chunk = second.read(_CHUNK_SIZE)
                if first_chunk != second_chunk:
                    status = ByteStatus.DIFFERENT
                    break
                if not first_chunk:
                    status = ByteStatus.IDENTICAL
                    break
            after = (os.fstat(first.fileno()), os.fstat(second.fileno()))
            for old, new in zip(before, after, strict=True):
                if (old.st_size, old.st_mtime_ns, old.st_ctime_ns) != (
                    new.st_size,
                    new.st_mtime_ns,
                    new.st_ctime_ns,
                ):
                    return ByteStatus.ERROR, 'файл изменился во время сравнения'
    except OSError as exc:
        return ByteStatus.ERROR, str(exc)
    return status, None


def compare_candidates(
    group: CandidateGroup, registry: FileRegistry
) -> Iterator[PairAnalysis]:
    """Сравнивает только пары внутри найденной группы."""
    physical_paths = {
        candidate.path: registry.physical_path(registry.track(candidate.path))
        for candidate in group.candidates
    }
    for left, right in combinations(group.candidates, 2):
        differences = []
        missing = []
        right_values = right.values()
        for field, left_value in left.values().items():
            right_value = right_values[field]
            if left_value is None or right_value is None:
                missing.append(field)
            elif left_value != right_value:
                differences.append(field)
        status, error = _compare_bytes(
            physical_paths[left.path], physical_paths[right.path]
        )
        yield PairAnalysis(
            left, right, tuple(differences), tuple(missing), status, error
        )
