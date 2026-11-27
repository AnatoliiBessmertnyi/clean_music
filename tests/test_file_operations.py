"""Контракт исполнителя переименования и интеграция с Windows rename."""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest

import pipeline_utils
from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationResult, OperationStatus, inspect_file
from tag_repository import AudioTags
from tests.test_pipeline_utils import Interactor, MemoryRepository, admit, windows


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
    assert (
        repo.get_tags(old).complete_tags == tags
        and repo.get_tags(new).complete_tags is None
    )


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
    record = admit(ctx, old)
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
    assert (
        repo.get_tags(middle).complete_tags == tags
        and repo.get_tags(new).complete_tags is None
    )
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


def test_dry_run_conflict_in_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, target = (
        tmp_path / name for name in ('first.mp3', 'second.mp3', 'target.mp3')
    )
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    monkeypatch.setattr(pipeline_utils, 'modification_allowed', Mock(return_value=True))
    log_operation = Mock()
    monkeypatch.setattr(pipeline_utils, 'log_operation', log_operation)
    executor = FileExecutor()
    assert (
        executor.rename(first, target, ctx, repo, ui).status
        is OperationStatus.SIMULATED
    )
    source = ctx.registry.register(second.absolute())
    target_id = ctx.registry.id_for(target)
    before = ctx.registry.current_paths()
    log_operation.reset_mock()
    ui.messages.clear()

    result = executor.rename(second, target, ctx, repo, ui)

    reason = 'конфликт целевого имени; оба файла сохранены'
    assert result == OperationResult(OperationStatus.CONFLICT, target, reason)
    assert ctx.get_block_reason(second) == reason
    assert ui.messages == ['  Конфликт имени: target.mp3; оба файла сохранены.']
    log_operation.assert_called_once_with(
        'rename', second, 'конфликт', reason, target=target, proposed=True
    )
    assert ctx.registry.current_paths() == before
    assert ctx.registry.id_for(second) == source.id
    assert ctx.registry.id_for(target) == target_id


@windows
def test_dry_run_reuses_vacated_source_and_keeps_each_identity(tmp_path: Path) -> None:
    first, second, renamed, final = (
        tmp_path / name
        for name in ('First.mp3', 'Second.mp3', 'Renamed.mp3', 'End.mp3')
    )
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    first_record = admit(ctx, first)
    second_record = admit(ctx, second)
    executor = FileExecutor()

    assert executor.rename(first, renamed, ctx, repo, ui).is_model_change
    assert executor.rename(second, first, ctx, repo, ui).is_model_change
    assert executor.write_tags(first, 'Second', 'Song', ctx, repo, ui).is_model_change
    assert executor.write_tags(renamed, 'First', 'Song', ctx, repo, ui).is_model_change
    assert executor.rename(
        renamed, renamed.with_name('renamed.mp3'), ctx, repo, ui
    ).is_model_change
    assert executor.rename(renamed, final, ctx, repo, ui).is_model_change
    assert ctx.present_path(first_record.id) == final
    assert ctx.present_path(second_record.id) == first
    assert repo.get_tags(final).complete_tags == AudioTags('First', 'Song')
    assert repo.get_tags(first).complete_tags == AudioTags('Second', 'Song')
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        first.name: b'first',
        second.name: b'second',
    }


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
@pytest.mark.parametrize('new_name', ['track.mp3', 'Track.MP3', 'Other.mp3'])
def test_default_executor_reads_tags_from_renamed_file(
    tmp_path: Path, new_name: str
) -> None:
    old, new = tmp_path / 'Track.mp3', tmp_path / new_name
    old.write_bytes(b'audio')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    source = admit(ctx, old)
    assert repo.update_tags(old, 'artist', 'title')

    result = FileExecutor().rename(old, new, ctx, repo, ui)

    assert result.status is OperationStatus.SUCCESS, ui.messages
    assert result.path is not None and result.path.name == new_name
    assert [entry.name for entry in tmp_path.iterdir() if entry.is_file()] == [new_name]
    assert not list((tmp_path / '.clean_music-recovery').glob('*.json'))
    assert new.read_bytes() == b'audio'
    assert inspect_file(new) == source.identity
    updated = ctx.registry.record_of(source.id)
    assert updated.path.name == new_name
    assert updated.identity == source.identity
    assert repo.get_tags(new).complete_tags == AudioTags('artist', 'title')


@windows
@pytest.mark.parametrize('observe_actual', [False, True])
def test_unconfirmed_real_rename_blocks_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observe_actual: bool
) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'original')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    source = admit(ctx, old)
    executor = FileExecutor()
    real_iterdir = Path.iterdir
    calls = Mock()

    def inspect_directory(path: Path) -> Iterator[Path]:
        if path == tmp_path:
            calls()
            if not observe_actual or calls.call_count == 1:
                raise PermissionError('сбой чтения каталога после rename')
        return real_iterdir(path)

    monkeypatch.setattr(Path, 'iterdir', inspect_directory)
    rename_call = create_autospec(Path.rename, side_effect=Path.rename)
    monkeypatch.setattr(Path, 'rename', rename_call)
    rename_event = Mock()
    monkeypatch.setattr(pipeline_utils, 'log_operation', rename_event)

    result = executor.rename(old, new, ctx, repo, ui)

    assert result.status is OperationStatus.UNCERTAIN
    assert result.path is None
    assert f'Запрошено: {old} → {new}' in result.reason
    observed = str(new) if observe_actual else 'не установлено'
    assert f'Фактическое местонахождение исходного файла: {observed}' in result.reason
    assert result.reason in ui.messages
    rename_event.assert_called_once_with(
        'rename', old, 'отказ', result.reason, target=new
    )
    assert ctx.registry.record_of(source.id) == source
    assert ctx.is_path_blocked(old) and ctx.is_path_blocked(new)
    assert not ctx.get_stat('renamed_by_tags')

    for path in (old, new):
        outcome = executor.rename(path, tmp_path / 'retry.mp3', ctx, repo, ui)
        assert outcome.status is OperationStatus.ERROR
    rename_call.assert_called_once_with(old, new)
    assert [entry for entry in real_iterdir(tmp_path) if entry.is_file()] == [new]
    assert len(list((tmp_path / '.clean_music-recovery').glob('*.json'))) == 1
    assert inspect_file(new) == source.identity
    assert new.read_bytes() == b'original'


@windows
def test_default_executor_conflict_keeps_both_files(tmp_path: Path) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'original')
    new.write_bytes(b'occupied')
    ctx = SessionContext(False, False)

    admit(ctx, old)
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

    admit(ctx, path)
    result = FileExecutor().rename(path, path, ctx, MemoryRepository(), Interactor())

    assert result.status is OperationStatus.NOOP
    assert result.path == path
    assert path.read_bytes() == b'audio'
    assert ctx.registry.current_paths() == [path]


def test_tag_write_blocked_without_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / 'Track.mp3'
    source.write_bytes(b'synthetic')
    ctx = SessionContext(False, False)
    ctx.block_path(source, '')
    with caplog.at_level('INFO', logger='clean_music'):
        result = FileExecutor().write_tags(
            source, 'Artist', 'Title', ctx, MemoryRepository(), Interactor()
        )

    reason = 'запись тегов заблокирована, причина не указана'
    assert result == OperationResult(OperationStatus.ERROR, source, reason)
    [operation] = [
        record.operation
        for record in caplog.records
        if hasattr(record, 'operation') and record.operation.kind == 'tags'
    ]
    assert (operation.path, operation.result, operation.reason) == (
        str(source),
        'блокировка',
        reason,
    )
    assert ctx.is_path_blocked(source)
    assert source.read_bytes() == b'synthetic'
