"""Только цепочки изменений, затронутые защитой Issue #9."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

import stages
from context import SessionContext
from pipeline import AudioProcessingPipeline
from tests.test_pipeline_utils import Interactor, MemoryRepository, windows


@windows
@pytest.mark.parametrize('conflict', [False, True])
def test_junk_rename_failure_blocks_tag_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    path = tmp_path / 'Artist - Title (vksaver).mp3'
    target = tmp_path / 'Artist - Title.mp3'
    path.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.simulate_update_tags(path, 'Artist', 'Title (vksaver)')
    files = [path]
    if conflict:
        target.write_bytes(b'occupied')
        files.append(target)
    else:
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))
    assert stages.JunkCleaningStage().execute(files, ctx, repo, ui) == files
    assert ctx.get_block_reason(path) == (
        'конфликт целевого имени; оба файла сохранены' if conflict else 'locked'
    )
    if conflict:
        assert target.read_bytes() == b'occupied'
    assert not repo.writes and not ctx.get_stat('junk_removed')
    assert path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_junk_then_normalization_preserves_file_id(
    tmp_path: Path, dry_run: bool
) -> None:
    old = tmp_path / 'Artist \u2013 Title (vksaver).mp3'
    middle = tmp_path / 'Artist \u2013 Title.mp3'
    final = tmp_path / 'Artist - Title.mp3'
    neighbor = tmp_path / 'Neighbor.mp3'
    old.write_bytes(b'original')
    neighbor.write_bytes(b'neighbor')
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    file_id = ctx.registry.register(old).id
    pipeline = AudioProcessingPipeline(
        [stages.JunkCleaningStage(), stages.NormalizationStage()]
    )

    assert pipeline.run([neighbor, old], ctx, repo, ui) == [neighbor, final]
    assert ctx.registry.path_of(file_id) == final
    assert ctx.registry.id_for(final) == file_id
    assert ctx.registry.id_for(old) is None and ctx.registry.id_for(middle) is None
    assert not middle.exists()
    assert old.exists() == dry_run
    assert final.exists() == (not dry_run)
    assert (old if dry_run else final).read_bytes() == b'original'
    assert neighbor.read_bytes() == b'neighbor'
    assert ctx.get_stat('junk_removed') == int(not dry_run)
    assert ctx.get_stat('dash_normalized') == int(not dry_run)


@windows
@pytest.mark.parametrize('outcome', ['success', 'simulated', 'denied'])
def test_tag_rename_passes_current_paths_to_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    old = tmp_path / 'Title.mp3'
    new = tmp_path / 'Artist - Title.mp3'
    neighbor = tmp_path / 'Neighbor.mp3'
    untagged = tmp_path / 'Untagged.mp3'
    old.write_bytes(b'original')
    neighbor.write_bytes(b'neighbor')
    untagged.touch()
    ctx = SessionContext(outcome == 'simulated', False)
    repo, ui = MemoryRepository(), Interactor()
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(neighbor, 'Artist', 'Other')
    file_id = ctx.registry.register(old).id
    neighbor_id = ctx.registry.register(neighbor).id
    if outcome == 'denied':
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))

    stage = stages.TagRenamingStage()
    handle_mismatches = Mock(side_effect=lambda files, *_: files)
    monkeypatch.setattr(stage, '_handle_mismatches', handle_mismatches)

    current = old if outcome == 'denied' else new
    expected = [neighbor, current, untagged]
    assert stage.execute([neighbor, old, untagged], ctx, repo, ui) == expected
    handle_mismatches.assert_called_once_with(expected, ctx, repo, ui)
    assert stage._pending_mismatches == [(neighbor, 'Neighbor', 'Artist - Other.mp3')]
    assert ctx.missing_files == [untagged]
    assert ctx.registry.path_of(file_id) == current
    assert ctx.registry.id_for(current) == file_id
    assert ctx.registry.path_of(neighbor_id) == neighbor
    assert ctx.registry.id_for(neighbor) == neighbor_id
    assert ctx.get_block_reason(old) == ('locked' if outcome == 'denied' else None)
    renamed = outcome == 'success'
    assert ctx.get_stat('renamed_by_tags') == int(renamed)
    assert old.exists() == (not renamed)
    assert new.exists() == renamed
    assert (new if renamed else old).read_bytes() == b'original'
    assert neighbor.read_bytes() == b'neighbor'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_tag_rename_applies_mismatches_in_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    individual = tmp_path / 'Artist - Original.mp3'
    safe = tmp_path / 'Matching.mp3'
    neighbor = tmp_path / 'Artist - Unchanged.mp3'
    group = tmp_path / 'Without separator.mp3'
    files = [individual, safe, neighbor, group]
    expected = [
        tmp_path / 'Artist - Corrected.mp3',
        tmp_path / 'Artist - Matching.mp3',
        neighbor,
        tmp_path / 'Artist - Grouped.mp3',
    ]
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    monkeypatch.setattr(ui, 'prompt', Mock(side_effect=['', 'y', 't']))
    titles = ['Corrected', 'Matching', 'Unchanged', 'Grouped']
    ids = []
    for source, title in zip(files, titles, strict=True):
        source.write_bytes(title.encode())
        repo.simulate_update_tags(source, 'Artist', title)
        ids.append(ctx.registry.register(source).id)

    assert stages.TagRenamingStage().execute(files, ctx, repo, ui) == expected
    assert [ctx.registry.id_for(path) for path in expected] == ids
    assert [ctx.registry.path_of(file_id) for file_id in ids] == expected
    assert ctx.get_stat('renamed_by_tags') == (0 if dry_run else 3)
    for source, target, title in zip(files, expected, titles, strict=True):
        assert (source if dry_run else target).read_bytes() == title.encode()
        if source != target:
            assert not (target if dry_run else source).exists()
            assert ctx.registry.id_for(source) is None


@windows
@pytest.mark.parametrize('outcome', ['conflict', 'denied', 'blocked'])
def test_tag_mismatch_keeps_original_path_on_rename_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    source = tmp_path / 'Without separator.mp3'
    target = tmp_path / 'Artist - Corrected.mp3'
    source.write_bytes(b'original')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor('y')
    repo.simulate_update_tags(source, 'Artist', 'Corrected')
    file_id = ctx.registry.register(source).id
    files = [source]
    if outcome == 'conflict':
        target.write_bytes(b'occupied')
        files.append(target)
        target_id = ctx.registry.register(target).id
        reason = 'конфликт целевого имени; оба файла сохранены'
    elif outcome == 'denied':
        reason = 'locked'
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError(reason)))
    else:
        reason = 'ранее заблокирован'
        ctx.block_path(source, reason)

    assert stages.TagRenamingStage().execute(files, ctx, repo, ui) == files
    assert ctx.registry.path_of(file_id) == source
    assert ctx.registry.id_for(source) == file_id
    assert ctx.get_stat('renamed_by_tags') == 0
    assert ctx.get_block_reason(source) == reason
    assert source.read_bytes() == b'original'
    if outcome == 'conflict':
        assert target.read_bytes() == b'occupied'
        assert ctx.registry.id_for(target) == target_id
    else:
        assert not target.exists()
        assert ctx.get_block_reason(target) == (reason if outcome == 'denied' else None)


@windows
@pytest.mark.parametrize('answer', ['', 'y', 'yes'])
def test_tag_rename_conflict_keeps_both(tmp_path: Path, answer: str) -> None:
    old, new = tmp_path / 'Title.mp3', tmp_path / 'Artist - Title.mp3'
    old.write_bytes(b'old')
    new.write_bytes(b'new target')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor(answer)
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(new, 'Artist', 'Title')
    assert stages.TagRenamingStage()._rename_by_tags([old, new], ctx, repo, ui) == [
        old,
        new,
    ]
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
