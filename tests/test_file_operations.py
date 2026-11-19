"""Контракт исполнителя переименования и интеграция с Windows rename."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationResult, OperationStatus
from tag_repository import AudioTags
from tests.test_pipeline_utils import Interactor, MemoryRepository, windows


@pytest.mark.parametrize(
    'raw_status,status,reason',
    [
        ('renamed', OperationStatus.SUCCESS, 'файл переименован'),
        ('unchanged', OperationStatus.NOOP, 'имя файла не изменилось'),
        ('planned', OperationStatus.SIMULATED, 'переименование запланировано'),
        (
            'duplicate_exists',
            OperationStatus.CONFLICT,
            'конфликт целевого имени; оба файла сохранены',
        ),
        ('skipped', OperationStatus.ERROR, 'переименование пропущено'),
        (
            'failed',
            OperationStatus.UNCERTAIN,
            'результат переименования не подтверждён',
        ),
    ],
)
@pytest.mark.parametrize('blocked', [False, True])
def test_status_mapping(
    raw_status: str, status: OperationStatus, reason: str, blocked: bool
) -> None:
    old, new = Path('old.mp3'), Path('new.mp3')
    ctx, repo, ui = (
        SessionContext(raw_status == 'planned', False),
        MemoryRepository(),
        Interactor(),
    )
    record = ctx.registry.register(old.absolute())
    if blocked:
        ctx.block_path(old, 'причина блокировки')
    raw_path = old if raw_status == 'unchanged' else new
    rename_fn = Mock(return_value=(raw_status, raw_path))

    result = FileExecutor(rename_fn).rename(old, new, ctx, repo, ui)

    expected_path = None if raw_status in ('failed', 'skipped') else raw_path
    assert result == OperationResult(
        status, expected_path, 'причина блокировки' if blocked else reason
    )
    rename_fn.assert_called_once_with(old, new, ctx, repo, ui)
    assert not ui.messages
    moved = raw_status in ('renamed', 'planned')
    assert ctx.registry.current_paths() == [(new if moved else old).absolute()]
    assert ctx.registry.path_of(record.id) == (new if moved else old).absolute()
    assert ctx.registry.id_for(old) == (None if moved else record.id)
    assert ctx.registry.id_for(new) == (record.id if moved else None)


def test_unknown_status_raises() -> None:
    ctx = SessionContext(False, False)
    executor = FileExecutor(Mock(return_value=('unexpected', None)))
    with pytest.raises(ValueError, match='unexpected'):
        executor.rename(
            Path('old.mp3'), Path('new.mp3'), ctx, MemoryRepository(), Interactor()
        )


@windows
def test_registered_missing_source_cannot_start_simulation(tmp_path: Path) -> None:
    old, new = tmp_path / 'a.mp3', tmp_path / 'b.mp3'
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    record = ctx.registry.register(old)
    tags = AudioTags('Artist', 'Title')
    repo.simulate_update_tags(old, tags.artist, tags.title)

    result = FileExecutor().rename(old, new, ctx, repo, ui)

    assert result.status is OperationStatus.ERROR
    assert ctx.registry.path_of(record.id) == old
    assert repo.get_tags(old) == tags and repo.get_tags(new) is None


@windows
@pytest.mark.parametrize('obstacle', ['blocked', 'disk', 'registry', 'directory'])
def test_refused_model_step_preserves_path_and_cache(
    tmp_path: Path, obstacle: str
) -> None:
    old, middle, new = (tmp_path / name for name in ('a.mp3', 'b.mp3', 'c.mp3'))
    old.write_bytes(b'original')
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    tags = AudioTags('Artist', 'Title')
    repo.simulate_update_tags(old, tags.artist, tags.title)
    executor = FileExecutor()
    record = ctx.registry.register(old)
    assert executor.rename(old, middle, ctx, repo, ui).is_model_change

    if obstacle == 'blocked':
        ctx.block_path(middle, 'test block')
    elif obstacle == 'disk':
        new.write_bytes(b'occupied')
    elif obstacle == 'registry':
        ctx.registry.register(new)
    else:
        new = tmp_path / 'other' / new.name

    result = executor.rename(middle, new, ctx, repo, ui)

    assert result.status is (
        OperationStatus.CONFLICT
        if obstacle in ('disk', 'registry')
        else OperationStatus.ERROR
    )
    assert ctx.get_block_reason(middle) == result.reason
    assert ctx.registry.path_of(record.id) == middle
    assert repo.get_tags(middle) == tags and repo.get_tags(new) is None
    assert old.read_bytes() == b'original' and not middle.exists()


def test_lazy_registration_and_rename_chain() -> None:
    old, middle, new = Path('a.mp3'), Path('b.mp3'), Path('c.mp3')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    executor = FileExecutor(
        Mock(
            side_effect=[('unchanged', old)] * 2
            + [('renamed', middle), ('renamed', new)]
        )
    )
    assert ctx.registry.current_paths() == []
    executor.rename(old, old, ctx, repo, ui)
    file_id = ctx.registry.id_for(old)
    assert file_id is not None
    executor.rename(old, old, ctx, repo, ui)
    assert ctx.registry.id_for(old) == file_id
    assert ctx.registry.current_paths() == [old.absolute()]
    executor.rename(old, middle, ctx, repo, ui)
    assert ctx.registry.id_for(middle) == file_id
    executor.rename(middle, new, ctx, repo, ui)
    assert ctx.registry.id_for(new) == file_id
    assert ctx.registry.path_of(file_id) == new.absolute()
    assert ctx.registry.current_paths() == [new.absolute()]


def test_real_success_replaces_stale_registry_owner() -> None:
    old, new = Path('old.mp3'), Path('new.mp3')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    source = ctx.registry.register(old.absolute())
    stale = ctx.registry.register(new.absolute())
    rename_fn = Mock(return_value=('renamed', new))

    result = FileExecutor(rename_fn).rename(old, new, ctx, repo, ui)

    rename_fn.assert_called_once_with(old, new, ctx, repo, ui)
    assert result.status is OperationStatus.SUCCESS
    assert ctx.registry.id_for(new) == source.id
    assert ctx.registry.path_of(stale.id) == new.absolute()
    assert ctx.registry.current_paths() == [new.absolute()]
    with pytest.raises(ValueError, match='пропавший'):
        ctx.registry.update_path(stale.id, old.absolute())


def test_dry_run_conflict_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, target = Path('first.mp3'), Path('second.mp3'), Path('target.mp3')
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    rename_fn = Mock(return_value=('planned', target))
    log_operation = Mock()
    monkeypatch.setattr('file_operations.log_operation', log_operation)
    executor = FileExecutor(rename_fn)
    assert (
        executor.rename(first, target, ctx, repo, ui).status
        is OperationStatus.SIMULATED
    )
    source = ctx.registry.register(second.absolute())
    target_id = ctx.registry.id_for(target)
    before = ctx.registry.current_paths()

    result = executor.rename(second, target, ctx, repo, ui)

    reason = 'конфликт целевого имени; оба файла сохранены'
    assert result == OperationResult(OperationStatus.CONFLICT, target, reason)
    rename_fn.assert_called_once_with(first, target, ctx, repo, ui)
    assert ctx.get_block_reason(second) == reason
    assert ui.messages == ['  Конфликт имени: target.mp3; оба файла сохранены.']
    log_operation.assert_called_once_with(
        'rename', second, 'конфликт', reason, target=target, proposed=True
    )
    assert ctx.registry.current_paths() == before
    assert ctx.registry.id_for(second) == source.id
    assert ctx.registry.id_for(target) == target_id


def test_dry_run_case_change_preserves_identity() -> None:
    old, new = Path('Track.mp3'), Path('track.mp3')
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    source = ctx.registry.register(old.absolute())
    assert ctx.registry.id_for(new) == (source.id if os.name == 'nt' else None)
    rename_fn = Mock(return_value=('planned', new))

    result = FileExecutor(rename_fn).rename(old, new, ctx, repo, ui)

    assert result.status is OperationStatus.SIMULATED
    rename_fn.assert_called_once_with(old, new, ctx, repo, ui)
    assert ctx.registry.id_for(new) == source.id
    assert ctx.registry.path_of(source.id).name == new.name


@windows
def test_default_executor_moves_file_and_tag_cache(tmp_path: Path) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'audio')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    source = ctx.registry.register(old)
    repo.simulate_update_tags(old, 'artist', 'title')

    result = FileExecutor().rename(old, new, ctx, repo, ui)

    assert result.status is OperationStatus.SUCCESS, ui.messages
    assert result.path == new
    assert new.read_bytes() == b'audio'
    assert not old.exists()
    assert ctx.registry.id_for(new) == source.id
    assert ctx.registry.id_for(old) is None
    assert repo.get_tags(new) == AudioTags('artist', 'title')
    assert repo.get_tags(old) is None


@windows
def test_default_executor_conflict_keeps_both_files(tmp_path: Path) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'original')
    new.write_bytes(b'occupied')
    ctx = SessionContext(False, False)

    result = FileExecutor().rename(old, new, ctx, MemoryRepository(), Interactor())

    assert result.status is OperationStatus.CONFLICT
    assert result.path == new
    assert old.read_bytes() == b'original'
    assert new.read_bytes() == b'occupied'
    assert ctx.registry.current_paths() == [old]


@windows
def test_default_executor_same_path_is_noop(tmp_path: Path) -> None:
    path = tmp_path / 'track.mp3'
    path.write_bytes(b'audio')
    ctx = SessionContext(False, False)

    result = FileExecutor().rename(path, path, ctx, MemoryRepository(), Interactor())

    assert result.status is OperationStatus.NOOP
    assert result.path == path
    assert path.read_bytes() == b'audio'
    assert ctx.registry.current_paths() == [path]
