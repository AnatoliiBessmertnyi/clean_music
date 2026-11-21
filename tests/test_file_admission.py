"""Допуск объектов через штатное сканирование и защита файловых операций."""

import logging
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest
from mutagen.mp3 import MP3

import tag_repository
from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationStatus
from main import get_audio_files
from pipeline_utils import WSL_MODIFICATION_DISABLED
from report_events import Operation
from tag_repository import AudioTags, TagRepository
from tests.test_pipeline_utils import Interactor, windows
from tests.test_tag_repository import write_mp3


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f'Создание symlink недоступно: {exc}')


@pytest.mark.parametrize(
    'kind,reason',
    [
        ('directory', 'не является обычным файлом'),
        ('hardlink', 'ровно одна жёсткая ссылка'),
        ('symlink', 'ссылка или reparse point'),
        ('dangling', 'ссылка или reparse point'),
    ],
)
def test_scan_rejects_unsupported_entry(tmp_path: Path, kind: str, reason: str) -> None:
    # На WSL создаём объекты на том же смонтированном томе, что и checkout.
    mount = Path('/mnt/d') if Path('/mnt/d').is_mount() else Path.cwd()
    base = mount if os.name != 'nt' and mount.is_relative_to('/mnt') else tmp_path
    with TemporaryDirectory(prefix='issue14-', dir=base) as directory:
        folder = Path(directory)
        regular = folder / 'regular.mp3'
        regular.write_bytes(b'ordinary')
        other = folder / 'data.bin'
        candidate = folder / 'candidate.mp3'
        if kind != 'dangling':
            other.write_bytes(b'preserved')
        if kind == 'directory':
            candidate.mkdir()
        elif kind == 'hardlink':
            candidate.hardlink_to(other)
        else:
            symlink(candidate, other)

        ctx, ui = SessionContext(False, False, target_dir=folder), Interactor()
        assert get_audio_files(folder, ctx, ui) == [regular], ui.messages
        assert ctx.registry.current_paths() == [regular]
        assert ctx.registry.track(regular).identity is not None
        assert any(candidate.name in msg and reason in msg for msg in ui.messages)
        assert regular.read_bytes() == b'ordinary'
        if kind == 'dangling':
            assert candidate.is_symlink() and not other.exists()
        else:
            assert other.read_bytes() == b'preserved'


@windows
@pytest.mark.parametrize('action', ['rename', 'tags', 'modeled_rename', 'modeled_tags'])
@pytest.mark.parametrize('replacement', ['file', 'hardlink'])
def test_scan_identity_refuses_changed_source(
    tmp_path: Path, action: str, replacement: str, caplog: pytest.LogCaptureFixture
) -> None:
    source = write_mp3(tmp_path / 'A.mp3', artist='Old', title='Song')
    neighbor = tmp_path / 'other.bin'
    target = tmp_path / 'C.mp3'
    original = source.read_bytes()
    ctx = SessionContext(action.startswith('modeled_'), False, target_dir=tmp_path)
    executor, repo, ui = FileExecutor(), TagRepository(), Interactor()
    assert get_audio_files(tmp_path, ctx, ui) == [source]
    current = source
    if ctx.dry_run:
        current = tmp_path / 'B.mp3'
        assert executor.rename(source, current, ctx, repo, ui).is_model_change
        assert not current.exists()
    record = ctx.registry.track(current)
    if replacement == 'file':
        source.rename(neighbor)
        substitute = write_mp3(source, artist='New', title='Song').read_bytes()
        assert substitute != original and len(substitute) == len(original)
        reason = 'физический объект подменён'
    else:
        neighbor.hardlink_to(source)
        substitute = original
        reason = 'ровно одна жёсткая ссылка'

    with caplog.at_level(logging.INFO, logger='clean_music'):
        if action.endswith('rename'):
            result = executor.rename(current, target, ctx, repo, ui)
        else:
            result = executor.write_tags(current, 'Edited', 'Title', ctx, repo, ui)
    assert result.status is OperationStatus.ERROR
    assert reason in result.reason and any(reason in msg for msg in ui.messages)
    assert ctx.registry.record_of(record.id) == record
    assert ctx.registry.current_paths() == [current]
    assert source.read_bytes() == substitute and neighbor.read_bytes() == original
    assert not target.exists() and (current == source or not current.exists())
    assert not ctx.get_stat('tags_written') and not ctx.get_stat('renamed_by_tags')
    operations = [
        operation
        for entry in caplog.records
        if isinstance(operation := getattr(entry, 'operation', None), Operation)
    ]
    assert any(reason in operation.reason for operation in operations)
    assert all(operation.result != 'успех' for operation in operations)


@windows
def test_rechecks_source_after_inspecting_rename_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'A.mp3'
    target = tmp_path / 'B.mp3'
    parked = tmp_path / 'old.bin'
    source.write_bytes(b'old')
    ctx = SessionContext(False, False, target_dir=tmp_path)
    repo, ui = TagRepository(), Interactor()
    assert get_audio_files(tmp_path, ctx, ui) == [source]
    record = ctx.registry.track(source)
    original_lstat = os.lstat

    def swap_at_target_check(path: str | Path) -> os.stat_result:
        if Path(path) == target:
            source.rename(parked)
            source.write_bytes(b'new')
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'lstat', swap_at_target_check)
        result = FileExecutor().rename(source, target, ctx, repo, ui)
    assert result.status is OperationStatus.ERROR
    assert 'физический объект подменён' in result.reason
    assert ctx.registry.record_of(record.id) == record
    assert source.read_bytes() == b'new' and parked.read_bytes() == b'old'
    assert not target.exists()


@windows
def test_rechecks_after_loading_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_mp3(tmp_path / 'A.mp3', artist='Old', title='Song')
    original = source.read_bytes()
    substitute = write_mp3(tmp_path / 'next.bin', artist='New', title='Song')
    replacement = substitute.read_bytes()
    parked = tmp_path / 'old.bin'
    ctx, ui, repo = (
        SessionContext(False, False, target_dir=tmp_path),
        Interactor(),
        TagRepository(),
    )
    assert get_audio_files(tmp_path, ctx, ui) == [source]
    assert repo.get_tags(source).tags == AudioTags('Old', 'Song')

    def replace_during_load(path: Path) -> MP3:
        loaded = MP3(path)
        path.rename(parked)
        substitute.rename(path)
        return loaded

    monkeypatch.setattr(tag_repository, 'MP3', replace_during_load)
    result = FileExecutor().write_tags(source, 'Edited', 'Song', ctx, repo, ui)
    assert result.status is OperationStatus.ERROR
    assert 'физический объект подменён' in result.reason
    assert source.read_bytes() == replacement and parked.read_bytes() == original


@windows
def test_case_rename_then_write_preserves_identity(tmp_path: Path) -> None:
    source = write_mp3(tmp_path / 'Track.mp3', artist='Old', title='Song')
    target = source.with_name('track.mp3')
    ctx, repo, ui = (
        SessionContext(False, False, target_dir=tmp_path),
        TagRepository(),
        Interactor(),
    )
    assert get_audio_files(tmp_path, ctx, ui) == [source]
    record = ctx.registry.track(source)
    executor = FileExecutor()
    assert executor.rename(source, target, ctx, repo, ui).is_confirmed_change, (
        ui.messages
    )
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == [target.name]
    assert ctx.registry.id_for(target) == record.id
    assert ctx.registry.track(target).identity == record.identity
    assert executor.write_tags(target, 'New', 'Song', ctx, repo, ui).is_confirmed_change
    assert repo.read_tags_uncached(target).tags == AudioTags('New', 'Song')


@windows
def test_unscanned_source_is_never_adopted(tmp_path: Path) -> None:
    source = write_mp3(tmp_path / 'Track.mp3', artist='Old', title='Song')
    target = source.with_name('other.mp3')
    before = source.read_bytes()
    ctx, repo, ui = (
        SessionContext(False, False, target_dir=tmp_path),
        TagRepository(),
        Interactor(),
    )
    result = FileExecutor().rename(source, target, ctx, repo, ui)
    assert result.status is OperationStatus.ERROR
    assert 'исходная физическая идентичность файла неизвестна' in result.reason
    assert ctx.registry.track(source).identity is None
    assert source.read_bytes() == before and not target.exists()


@windows
def test_dangling_target_counts_as_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_mp3(tmp_path / 'A.mp3')
    target = tmp_path / 'B.mp3'
    before = source.read_bytes()
    ctx, repo, ui = (
        SessionContext(False, False, target_dir=tmp_path),
        TagRepository(),
        Interactor(),
    )
    assert get_audio_files(tmp_path, ctx, ui) == [source]
    record = ctx.registry.track(source)
    original_lstat = os.lstat

    def dangling_lstat(path: str | Path) -> os.stat_result:
        if Path(path) == target:
            return Mock(spec=os.stat_result, st_mode=stat.S_IFLNK)
        return original_lstat(path)

    # Подмена только результата lstat цели. Реальные symlink проверяются отдельно.
    monkeypatch.setattr(os, 'lstat', dangling_lstat)
    result = FileExecutor().rename(source, target, ctx, repo, ui)
    assert result.status is OperationStatus.CONFLICT and 'конфликт' in result.reason
    assert ctx.registry.record_of(record.id) == record
    assert source.read_bytes() == before and not target.exists()


@pytest.mark.skipif(os.name == 'nt', reason='Требуется WSL с /mnt')
@pytest.mark.parametrize('dry_run', [False, True])
def test_wsl_mount_refuses_scanned_object(dry_run: bool) -> None:
    mount = Path('/mnt/d') if Path('/mnt/d').is_mount() else Path.cwd()
    if not mount.is_relative_to('/mnt'):
        pytest.skip('Checkout расположен вне /mnt, том /mnt/d не смонтирован')
    with TemporaryDirectory(prefix='issue14-', dir=mount) as directory:
        folder = Path(directory)
        source = write_mp3(folder / 'A.mp3', artist='Old', title='Song')
        before = source.read_bytes()
        target = folder / 'B.mp3'
        ctx, repo, ui = (
            SessionContext(dry_run, False, target_dir=folder),
            TagRepository(),
            Interactor(),
        )
        assert get_audio_files(folder, ctx, ui) == [source]
        record = ctx.registry.track(source)
        executor = FileExecutor()
        written = executor.write_tags(source, 'New', 'Song', ctx, repo, ui)
        renamed = executor.rename(source, target, ctx, repo, ui)
        for result in (written, renamed):
            assert result.status is OperationStatus.ERROR
            assert WSL_MODIFICATION_DISABLED in result.reason
        assert ctx.registry.record_of(record.id) == record
        assert source.read_bytes() == before and not target.exists()
