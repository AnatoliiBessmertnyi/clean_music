import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal

import pytest
from mutagen.mp3 import MP3, BitrateMode

from duplicate_analysis import (
    ByteStatus,
    MatchSource,
    compare_candidates,
    decide_copies,
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


def test_premature_eof_does_not_confirm_equal_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / name for name in ('Artist - Song.mp3', 'Artist - Song (1).mp3')]
    for path in paths:
        path.write_bytes(b'identical')
    read = MetadataRead(AudioTags('Artist', 'Song'))
    [group] = find_candidate_groups(
        paths, MetadataRepository(dict.fromkeys(paths, read))
    )
    original_open = Path.open

    def open_file(path: Path, mode: Literal['rb']) -> BinaryIO:
        stream = original_open(path, mode)
        monkeypatch.setattr(stream, 'read', lambda _size: b'')
        return stream

    monkeypatch.setattr(Path, 'open', open_file)
    [decision] = decide_copies(group, list(compare_candidates(group, FileRegistry())))
    assert decision.pair.byte_status is ByteStatus.ERROR
    assert decision.pair.byte_error == 'прочитан не весь файл'
    assert decision.keep is None and decision.redundant is None


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

    registry.mark_content_modeled(registry.track(planned).id)
    [pair] = compare_candidates(group, registry)
    assert pair.byte_status is ByteStatus.UNKNOWN and pair.byte_error is None


@pytest.mark.parametrize(
    ('stems', 'tags', 'expected'),
    [
        (('Artist - Song', 'Artist - Song (1)'), AudioTags('Artist', 'Song'), 0),
        (
            ('Artist - Song (1)', 'Artist - Song (1) (2)'),
            AudioTags('Artist', 'Song (1)'),
            0,
        ),
        (('Artist - Song', 'Artist - Song (1)'), AudioTags('Artist', 'Song (1)'), None),
        (('Song', 'Song (1)'), AudioTags('', 'Song'), None),
        (('Artist - Song (1)', 'Artist - Song (2)'), AudioTags('Artist', 'Song'), None),
        (('Artist - Song', 'Artist - Song'), AudioTags('Artist', 'Song'), None),
    ],
)
def test_copy_choice_uses_full_tags_and_is_order_independent(
    tmp_path: Path, stems: tuple[str, str], tags: AudioTags, expected: int | None
) -> None:
    paths = [tmp_path / str(i) / f'{stem}.mp3' for i, stem in enumerate(stems)]
    for path in paths:
        path.parent.mkdir()
        path.write_bytes(b'identical')
    repo = MetadataRepository(dict.fromkeys(paths, MetadataRead(tags)))
    for order in (paths, paths[::-1]):
        [group] = find_candidate_groups(order, repo)
        [decision] = decide_copies(
            group, list(compare_candidates(group, FileRegistry()))
        )
        assert decision.pair.byte_status is ByteStatus.IDENTICAL
        assert decision.keep == (paths[expected] if expected is not None else None)
        assert decision.redundant == (paths[1] if expected == 0 else None)
        assert decision.reason


@pytest.mark.parametrize(
    'qualifier',
    [
        'Live',
        'Studio',
        'DJ Ada Remix',
        'Acoustic',
        'Instrumental',
        'Remaster',
        'Radio Edit',
        'Album Version',
        'Unknown',
        '01',
    ],
)
def test_equal_bytes_with_other_name_qualifiers_skip(
    tmp_path: Path, qualifier: str
) -> None:
    paths = [
        tmp_path / 'Artist - Song.mp3',
        tmp_path / f'Artist - Song ({qualifier}).mp3',
    ]
    for path in paths:
        path.write_bytes(b'identical')
    repo = MetadataRepository(
        dict.fromkeys(paths, MetadataRead(AudioTags('Artist', 'Song')))
    )
    [group] = find_candidate_groups(paths, repo)
    [decision] = decide_copies(group, list(compare_candidates(group, FileRegistry())))
    assert decision.pair.byte_status is ByteStatus.IDENTICAL
    assert decision.keep is None and decision.redundant is None
    assert 'имена или исходные теги' in decision.reason


@pytest.mark.parametrize(
    ('left_type', 'right_type', 'left_bitrate', 'right_bitrate'),
    [
        ('mp3', 'mp3', 192000, 192000),
        ('flac', 'mp3', 900000, 192000),
        ('mp3', 'ogg', 192000, 192000),
    ],
    ids=['same-profile', 'lossless-lossy', 'lossy-codecs'],
)
def test_one_different_byte_prevents_choice(
    tmp_path: Path,
    left_type: str,
    right_type: str,
    left_bitrate: int,
    right_bitrate: int,
) -> None:
    reads = {}
    for index, (extension, bitrate) in enumerate(
        [(left_type, left_bitrate), (right_type, right_bitrate)]
    ):
        directory = tmp_path / str(index)
        directory.mkdir()
        path = directory / f'Artist - Song.{extension}'
        path.write_bytes(b'A' * (1024 * 1024) + bytes([index]))
        reads[path] = MetadataRead(
            AudioTags('Artist', 'Song'),
            AudioInfo(extension, extension, 180.0, bitrate, 44100, 2, None),
        )
    [group] = find_candidate_groups(list(reads), MetadataRepository(reads))
    [decision] = decide_copies(group, list(compare_candidates(group, FileRegistry())))
    assert decision.pair.byte_status is ByteStatus.DIFFERENT
    assert decision.keep is None and decision.redundant is None
    assert 'полное совпадение байтов не подтверждено' in decision.reason


def test_cbr_and_vbr_encodings_of_same_signal_skip(tmp_path: Path) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        pytest.skip('требуется ffmpeg для синтетических CBR и VBR')
    paths: list[Path] = []
    for directory, option, value, mode in (
        ('cbr', '-b:a', '192k', BitrateMode.CBR),
        ('vbr', '-q:a', '2', BitrateMode.VBR),
    ):
        folder = tmp_path / directory
        folder.mkdir()
        path = folder / 'Artist - Song.mp3'
        subprocess.run(
            [
                ffmpeg,
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                'sine=duration=1',
                '-c:a',
                'libmp3lame',
                option,
                value,
                '-metadata',
                'artist=Artist',
                '-metadata',
                'title=Song',
                str(path),
            ],
            check=True,
        )
        info = MP3(path).info
        assert info is not None and info.bitrate_mode == mode
        paths.append(path)
    [group] = find_candidate_groups(paths, TagRepository())
    [decision] = decide_copies(group, list(compare_candidates(group, FileRegistry())))
    assert decision.pair.byte_status is ByteStatus.DIFFERENT
    assert decision.keep is None and decision.redundant is None


@pytest.mark.parametrize(
    'direct', [ByteStatus.IDENTICAL, None, ByteStatus.ERROR, ByteStatus.DIFFERENT]
)
def test_each_proposed_copy_requires_its_own_direct_comparison(
    tmp_path: Path, direct: ByteStatus | None
) -> None:
    winner, first, second = paths = [
        tmp_path / f'Artist - Song{suffix}.mp3' for suffix in ('', ' (1)', ' (2)')
    ]
    for path in paths:
        path.write_bytes(b'identical')
    repo = MetadataRepository(
        dict.fromkeys(paths, MetadataRead(AudioTags('Artist', 'Song')))
    )
    [group] = find_candidate_groups(paths, repo)
    ab, ac, bc = compare_candidates(group, FileRegistry())
    pairs = [ab, bc]
    if direct is not None:
        pairs.append(replace(ac, byte_status=direct))
    decisions = list(decide_copies(group, pairs))
    proposals = {(item.keep, item.redundant) for item in decisions if item.keep}
    if direct is ByteStatus.DIFFERENT:
        assert not proposals
        assert 'противоречат' in decisions[0].reason
    elif direct is ByteStatus.IDENTICAL:
        assert proposals == {(winner, first), (winner, second)}
    else:
        assert proposals == {(winner, first)}
