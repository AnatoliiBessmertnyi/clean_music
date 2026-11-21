"""Чтение синтетических аудиофайлов и срок действия кэша."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from file_state import OperationStatus
from tag_repository import AudioTags, ReadStatus, TagRepository


def write_mp3(path: Path, *, artist: str = '', title: str = '') -> Path:
    path.write_bytes((b'\xff\xfb\x90\x64' + bytes(413)) * 5)
    tags = ID3()
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist or title:
        tags.save(path)
    return path


@pytest.mark.parametrize(
    ('artist', 'title', 'status'),
    [
        ('AC/DC', 'Track', ReadStatus.COMPLETE),
        ('AC/DC', '', ReadStatus.PARTIAL),
        ('', 'Track', ReadStatus.PARTIAL),
        ('', '', ReadStatus.EMPTY),
    ],
)
def test_read_mp3(tmp_path: Path, artist: str, title: str, status: ReadStatus) -> None:
    path = write_mp3(tmp_path / 'track.mp3', artist=artist, title=title)
    result = TagRepository().get_tags(path)
    assert result.status is status and result.tags == AudioTags(artist, title)
    assert result.info is not None
    assert result.info.format == 'MPEG audio'
    assert result.info.codec == 'MPEG audio layer 3'
    assert result.info.sample_rate == 44_100
    assert result.info.bitrate == 128_000
    assert result.info.bits_per_sample is None


@pytest.mark.parametrize('denied', [False, True])
def test_failed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    path = tmp_path / 'track.mp3'
    path.write_bytes(b'broken mp3')
    if denied:
        monkeypatch.setattr(
            'tag_repository.MP3', Mock(side_effect=PermissionError('denied'))
        )
    result = TagRepository().get_tags(path)
    assert result.status is ReadStatus.ERROR
    assert result.tags is None and result.info is None
    assert result.error
    if denied:
        assert result.error == 'denied'


@pytest.mark.parametrize('codec', ['alac', None])
def test_mp4_uses_reported_codec_and_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codec: str | None
) -> None:
    path = tmp_path / 'track.m4a'
    path.touch()
    audio = SimpleNamespace(
        tags={'©ART': ['AC/DC']},
        info=SimpleNamespace(codec=codec, length=12.5, bitrate=0, sample_rate=48_000),
    )
    monkeypatch.setattr('tag_repository.MP4', Mock(return_value=audio))
    result = TagRepository().get_tags(path)
    assert result.tags == AudioTags('AC/DC', '')
    assert result.info is not None
    assert result.info.format == 'MP4' and result.info.codec == codec
    assert result.info.duration == 12.5 and result.info.sample_rate == 48_000
    assert result.info.bitrate is None and result.info.bits_per_sample is None


def test_external_change_expires_cache(tmp_path: Path) -> None:
    path = write_mp3(tmp_path / 'track.mp3', artist='AC/DC')
    repo = TagRepository()
    assert repo.get_tags(path).tags == AudioTags('AC/DC', '')
    write_mp3(path, title='Track')
    # A size change ensures the fingerprint differs even on coarse clocks.
    with path.open('ab') as stream:
        stream.write(bytes(17))
    assert repo.get_tags(path).tags == AudioTags('', 'Track')
    path.write_bytes(b'broken')
    assert repo.get_tags(path).status is ReadStatus.ERROR


def test_filling_title_keeps_all_existing_artist_values(tmp_path: Path) -> None:
    path = write_mp3(tmp_path / 'track.mp3')
    tags = ID3()
    artists = ['AC/DC', 'Guest']
    tags.add(TPE1(encoding=3, text=artists))
    tags.save(path)

    repo = TagRepository()
    before = repo.get_tags(path).tags
    assert before is not None
    result = repo.apply_tags(path, before.artist, 'Track', simulated=False)
    assert result.status is OperationStatus.SUCCESS
    after = ID3(path)
    assert after['TPE1'].text == artists
    assert str(after['TIT2']) == 'Track'


@pytest.mark.parametrize('uncertain', [False, True])
def test_write_failure_cache_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    path = write_mp3(tmp_path / 'track.mp3', artist='AC/DC', title='Track')
    repo = TagRepository()
    original = repo.get_tags(path)
    writer: SimpleNamespace | OSError
    if uncertain:
        audio = SimpleNamespace(tags={}, save=Mock(side_effect=OSError('save failed')))
        writer = audio
    else:
        writer = OSError('open failed')
    # The stat fields stay identical. The parser exposes different data on a new read.
    parser = Mock(side_effect=[writer, SimpleNamespace(tags={'TPE1': 'Changed'})])
    monkeypatch.setattr('tag_repository.MP3', parser)
    result = repo.apply_tags(path, 'Other', 'New track', simulated=False)
    assert result.status is (
        OperationStatus.UNCERTAIN if uncertain else OperationStatus.ERROR
    )
    if uncertain:
        assert repo.get_tags(path).tags == AudioTags('Changed', '')
        assert parser.call_count == 2
    else:
        assert repo.get_tags(path) is original
        assert parser.call_count == 1


def test_model_rename_reads_physical_source_and_detects_change(tmp_path: Path) -> None:
    old = write_mp3(tmp_path / 'track.mp3', artist='AC/DC')
    new = tmp_path / 'virtual.mp3'
    repo = TagRepository()
    original = repo.get_tags(old)
    repo.simulate_rename(old, new)
    assert not new.exists()
    assert repo.get_tags(new) is original
    assert original.tags == AudioTags('AC/DC', '') and original.info is not None
    repo.simulate_update_tags(new, 'AC/DC', 'Proposed')
    assert repo.get_tags(new).info == original.info
    assert repo.get_tags(new).tags == AudioTags('AC/DC', 'Proposed')
    write_mp3(old)
    assert repo.get_tags(new).status is ReadStatus.EMPTY
