"""Поиск кандидатов и сравнение исходных признаков без изменения файлов."""

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from file_state import FileRegistry
from naming import normalize_filename_stem, normalize_unicode
from tag_repository import MetadataRead, TagRepository

_QUALIFIER = re.compile(r'\s*(?:\(([^()\[\]]+)\)|\[([^()\[\]]+)\])$')
_COPY_SUFFIX = re.compile(r' \([1-9][0-9]*\)')
_CHUNK_SIZE = 1024 * 1024


class MatchSource(StrEnum):
    TAGS = 'нормализованные исполнитель и название из тегов'
    NAME = 'нормализованное имя файла'


class ByteStatus(StrEnum):
    IDENTICAL = 'Побайтовое совпадение подтверждено'
    DIFFERENT = 'Байты различаются. Совпадение музыкальной записи не установлено'
    ERROR = 'Побайтовое сравнение не завершено'
    UNKNOWN = 'Будущие размер и байты неизвестны после запланированной записи тегов'


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


@dataclass(frozen=True)
class CopyDecision:
    pair: PairAnalysis
    reason: str
    keep: Path | None = None
    redundant: Path | None = None


def decide_copies(
    group: CandidateGroup, pairs: Sequence[PairAnalysis]
) -> Iterator[CopyDecision]:
    """Опирается на прямое сравнение каждой предлагаемой пары."""
    canonical = []
    for candidate in group.candidates:
        tags = candidate.metadata.complete_tags
        if tags and candidate.path.stem == f'{tags.artist} - {tags.title}':
            canonical.append(candidate)

    equal: dict[Path, set[Path]] = {item.path: set() for item in group.candidates}
    for pair in pairs:
        if pair.byte_status is ByteStatus.IDENTICAL and pair.byte_error is None:
            equal[pair.left.path].add(pair.right.path)
            equal[pair.right.path].add(pair.left.path)

    contradictory: set[Path] = set()
    for pair in pairs:
        if pair.byte_status is ByteStatus.DIFFERENT:
            common = equal[pair.left.path] & equal[pair.right.path]
            if common:
                contradictory.update(common | {pair.left.path, pair.right.path})

    winner = canonical[0] if len(canonical) == 1 else None
    copies: set[Path] = set()
    if winner is not None:
        copies = {
            candidate.path
            for candidate in group.candidates
            if candidate.metadata.error is None
            and candidate.metadata.complete_tags == winner.metadata.complete_tags
            and candidate.path.suffix == winner.path.suffix
            and candidate.path.stem.startswith(winner.path.stem)
            and _COPY_SUFFIX.fullmatch(candidate.path.stem[len(winner.path.stem) :])
        }

    for pair in pairs:
        left, right = pair.left, pair.right
        if pair.byte_status is not ByteStatus.IDENTICAL or pair.byte_error:
            reason = 'полное совпадение байтов не подтверждено'
        elif {left.path, right.path} & contradictory:
            reason = 'результаты побайтовых сравнений противоречат друг другу'
        elif any(
            item.metadata.error or item.metadata.complete_tags is None
            for item in (left, right)
        ):
            reason = 'для выбора имени нужны полные теги исполнителя и названия'
        elif winner is None:
            reason = 'в группе нет единственного имени, соответствующего тегам'
        elif winner not in (left, right):
            reason = 'в этой паре нет имени, выбранного по тегам'
        elif equal[winner.path] - copies:
            reason = 'имена или исходные теги совпавших файлов расходятся'
        else:
            redundant = right if left == winner else left
            yield CopyDecision(
                pair,
                'имя соответствует тегам, второе отличается только номером копии',
                winner.path,
                redundant.path,
            )
            continue
        yield CopyDecision(pair, reason)


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
            matched = 0
            while True:
                first_chunk = first.read(_CHUNK_SIZE)
                second_chunk = second.read(_CHUNK_SIZE)
                if first_chunk != second_chunk:
                    status = ByteStatus.DIFFERENT
                    break
                if not first_chunk:
                    if any(stat.st_size != matched for stat in before):
                        return ByteStatus.ERROR, 'прочитан не весь файл'
                    status = ByteStatus.IDENTICAL
                    break
                matched += len(first_chunk)
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
    records = {
        candidate.path: registry.track(candidate.path) for candidate in group.candidates
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
        left_record, right_record = records[left.path], records[right.path]
        if left_record.modeled_content or right_record.modeled_content:
            status, error = ByteStatus.UNKNOWN, None
        else:
            status, error = _compare_bytes(
                registry.physical_path(left_record),
                registry.physical_path(right_record),
            )
        yield PairAnalysis(
            left, right, tuple(differences), tuple(missing), status, error
        )
