from pathlib import Path
from typing import BinaryIO, Literal

import pytest

from duplicate_analysis import (
    ByteStatus,
    MatchSource,
    compare_candidates,
    find_candidate_groups,
)
from file_state import FileRegistry
from tag_repository import AudioInfo, AudioTags, MetadataRead, ReadStatus, TagRepository


class MetadataRepository(TagRepository):
    def __init__(self, reads: dict[Path, MetadataRead]) -> None:
        super().__init__()
        self.reads = reads

    def get_tags(self, filepath: Path) -> MetadataRead:
        return self.reads[filepath]


def test_versions_and_technical_differences(tmp_path: Path) -> None:
    titles = (
        'Песня',
        'Песня (Live at Wembley)',
        'Песня (DJ Ada Remix)',
        'Песня [Demo X]',
    )
    names = (titles[0], 'Песня (DJ Bob Remix)', titles[2], titles[3])
    paths = [tmp_path / f'Artist - {name}.mp3' for name in names]
    reads = {}
    for i, (path, title) in enumerate(zip(paths, titles, strict=True)):
        path.write_bytes(bytes([i]))
        info = (
            AudioInfo('FLAC', 'FLAC', 180.0, None, 44100, 2, 16)
            if i == 1
            else AudioInfo(
                'MPEG audio', 'MPEG audio layer 3', 175.0, 320000, 44100, 2, None
            )
        )
        reads[path] = MetadataRead(AudioTags('Artist', title), info)
    [group] = find_candidate_groups(paths, MetadataRepository(reads))
    assert group.source is MatchSource.TAGS and group.key == ('artist', 'песня')
    assert [item.tag_qualifiers for item in group.candidates] == [
        (),
        ('Live at Wembley',),
        ('DJ Ada Remix',),
        ('Demo X',),
    ]
    studio, live = group.candidates[:2]
    assert not studio.qualifier_disagreement
    assert live.name_qualifiers == ('DJ Bob Remix',) and live.qualifier_disagreement
    first = next(compare_candidates(group, FileRegistry()))
    assert (first.left, first.right) == (studio, live)
    assert first.byte_status is ByteStatus.DIFFERENT
    assert {'название', 'формат', 'кодек', 'длительность (с)'} <= set(first.differences)
    assert first.missing == ('битрейт (бит/с)', 'разрядность (бит)')
    assert live.metadata.info is not None and live.metadata.info.format == 'FLAC'


def test_name_fallback_preserves_partial_empty_and_error(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ('Song.mp3', 'Song (Live).ogg', 'Song.flac')]
    for path in paths:
        path.write_bytes(b'\0' * (1024 * 1024) + b'end')
    reads = dict(
        zip(
            paths,
            (
                MetadataRead(AudioTags('', 'Song')),
                MetadataRead(AudioTags('', '')),
                MetadataRead(None, error='access denied'),
            ),
            strict=True,
        )
    )
    [group] = find_candidate_groups(paths, MetadataRepository(reads))
    assert group.source is MatchSource.NAME
    assert [item.metadata.status for item in group.candidates] == [
        ReadStatus.PARTIAL,
        ReadStatus.EMPTY,
        ReadStatus.ERROR,
    ]
    assert group.candidates[0].metadata.tags == AudioTags('', 'Song')
    assert group.candidates[2].metadata.error == 'access denied'
    pairs = list(compare_candidates(group, FileRegistry()))
    assert len(pairs) == 3
    assert all(pair.byte_status is ByteStatus.IDENTICAL for pair in pairs)
    assert all('формат' in pair.missing for pair in pairs)


def test_access_denied_is_a_byte_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / name for name in ('first.mp3', 'second.mp3')]
    for path in paths:
        path.write_bytes(b'identical')
    read = MetadataRead(AudioTags('Artist', 'Song'))
    [group] = find_candidate_groups(
        paths, MetadataRepository(dict.fromkeys(paths, read))
    )
    original_open = Path.open

    def open_file(path: Path, mode: Literal['rb']) -> BinaryIO:
        if path == paths[1]:
            raise PermissionError('access denied')
        return original_open(path, mode)

    monkeypatch.setattr(Path, 'open', open_file)
    [pair] = compare_candidates(group, FileRegistry())
    assert pair.byte_status is ByteStatus.ERROR and pair.byte_error == 'access denied'


def test_dry_run_comparison_reads_physical_paths(tmp_path: Path) -> None:
    old, other, planned = [
        tmp_path / name for name in ('old.mp3', 'other.mp3', 'new.mp3')
    ]
    old.write_bytes(b'physical')
    other.write_bytes(b'physical')
    registry = FileRegistry()
    registry.update_path(registry.register(old).id, planned, simulated=True)
    read = MetadataRead(AudioTags('Artist', 'Song'))
    [group] = find_candidate_groups(
        [planned, other], MetadataRepository({planned: read, other: read})
    )
    [pair] = compare_candidates(group, registry)
    assert pair.left.path == planned and pair.byte_status is ByteStatus.IDENTICAL
    assert old.exists() and not planned.exists()
