"""Только цепочки изменений, затронутые защитой Issue #9."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

import stages
from context import SessionContext
from tests.test_pipeline_utils import Interactor, MemoryRepository, windows


@windows
def test_junk_rename_failure_blocks_tag_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'Artist - Title (vksaver).mp3'
    path.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.simulate_update_tags(path, 'Artist', 'Title (vksaver)')
    monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))
    stages.JunkCleaningStage().execute([path], ctx, repo, ui)
    assert ctx.is_path_blocked(path)
    assert not repo.writes and not ctx.get_stat('junk_removed')
    assert path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('answer', ['', 'y', 'yes'])
def test_tag_rename_conflict_keeps_both(tmp_path: Path, answer: str) -> None:
    old, new = tmp_path / 'Title.mp3', tmp_path / 'Artist - Title.mp3'
    old.write_bytes(b'old')
    new.write_bytes(b'new target')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor(answer)
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(new, 'Artist', 'Title')
    stages.TagRenamingStage()._rename_by_tags([old, new], ctx, repo, ui)
    assert old.read_bytes() == b'old' and new.read_bytes() == b'new target'
    assert ctx.is_path_blocked(old) and not ctx.get_stat('renamed_by_tags')
    assert 'Объединение отключено' in '\n'.join(ui.messages)
    assert not any('Объединить' in prompt for prompt in ui.prompts)


@pytest.mark.parametrize('dry_run', [False, True])
def test_duplicate_stage_preserves_files_and_cache(
    tmp_path: Path, dry_run: bool
) -> None:
    files = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    repo, ctx, ui = (
        MemoryRepository(),
        SessionContext(dry_run, False),
        Interactor('yes'),
    )
    for path, content in zip(files, [b'A', b'BBBB'], strict=True):
        path.write_bytes(content)
        repo.simulate_update_tags(path, 'Artist', 'Title')
    assert stages.DuplicateResolutionStage().execute(files, ctx, repo, ui) == files
    assert [path.read_bytes() for path in files] == [b'A', b'BBBB']
    assert all(repo.get_tags(path) is not None for path in files)
    assert not ctx.get_stat('duplicates_removed') and not ui.prompts


@windows
@pytest.mark.parametrize('write_succeeds', [False, True])
def test_tag_writing_before_failed_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_succeeds: bool
) -> None:
    path = tmp_path / 'Artist - Title (vksaver).mp3'
    path.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.succeed = write_succeeds
    rename = Mock(side_effect=PermissionError('locked'))
    monkeypatch.setattr(Path, 'rename', rename)
    stages.TagWritingStage().execute([path], ctx, repo, ui)
    assert len(repo.writes) == 1
    assert rename.call_count == int(write_succeeds)
    assert ctx.get_stat('tags_written') == int(write_succeeds)
    assert ctx.get_stat('renamed_by_tags') == 0 and ctx.is_path_blocked(path)
    assert path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('blocked_before', [False, True])
def test_artist_rescan_and_write_failure_block_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_before: bool
) -> None:
    canonical, variant = tmp_path / 'Artist - One.mp3', tmp_path / 'artist - Two.mp3'
    canonical.write_bytes(b'one')
    variant.write_bytes(b'two')
    repo = MemoryRepository()
    repo.simulate_update_tags(canonical, 'Artist', 'One')
    repo.simulate_update_tags(variant, 'artist', 'Two')
    repo.succeed = False
    ctx, ui = SessionContext(False, False, target_dir=tmp_path), Interactor()
    if blocked_before:
        ctx.block_path(variant, '')
    rename = Mock(side_effect=AssertionError('dependent rename'))
    monkeypatch.setattr(Path, 'rename', rename)
    stages.ArtistNormalizationStage().execute([], ctx, repo, ui)
    assert len(repo.writes) == (0 if blocked_before else 1)
    assert not rename.called and ctx.is_path_blocked(variant)
    assert canonical.read_bytes() == b'one' and variant.read_bytes() == b'two'


def test_manual_rescan_keeps_blocked_missing_file(tmp_path: Path) -> None:
    path = tmp_path / 'Track.mp3'
    path.write_bytes(b'original')
    ctx = SessionContext(False, False, target_dir=tmp_path)
    ctx.missing_files = [path]
    ctx.block_path(path, '')
    ui, repo = Interactor(), MemoryRepository()
    stages.ManualInputStage().execute([], ctx, repo, ui)
    assert ctx.missing_files == [path]
    assert not any('Введите исполнителя' in prompt for prompt in ui.prompts)
    assert not repo.writes and path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_tag_writing_rejects_hardlink(tmp_path: Path, dry_run: bool) -> None:
    path = tmp_path / 'Artist - Title.mp3'
    path.write_bytes(b'original')
    alias = tmp_path / 'alias.mp3'
    alias.hardlink_to(path)
    repo, ctx = MemoryRepository(), SessionContext(dry_run, False)
    stages.TagWritingStage().execute([path], ctx, repo, Interactor())
    assert not repo.writes and repo.get_tags(path) is None
    assert ctx.is_path_blocked(path)
    assert path.read_bytes() == alias.read_bytes() == b'original'


@pytest.mark.skipif(
    os.name == 'nt' or not str(Path.cwd()).startswith('/mnt/'),
    reason='Требуется запуск из WSL на /mnt/...',
)
@pytest.mark.parametrize('dry_run', [False, True])
def test_wsl_mounted_tag_write_refused(dry_run: bool) -> None:
    # Только созданный этим тестом временный каталог на смонтированном томе.
    with TemporaryDirectory(prefix='.issue9-test-', dir=Path.cwd()) as directory:
        path = Path(directory) / 'Artist - Title.mp3'
        path.write_bytes(b'original')
        repo, ctx, ui = MemoryRepository(), SessionContext(dry_run, False), Interactor()
        stages.TagWritingStage().execute([path], ctx, repo, ui)
        assert ctx.is_path_blocked(path)
        assert not repo.writes and repo.get_tags(path) is None
        assert not ctx.get_stat('tags_written')
        assert 'Модификация на монтированных файловых системах WSL' in '\n'.join(
            ui.messages
        )
        assert path.read_bytes() == b'original'
