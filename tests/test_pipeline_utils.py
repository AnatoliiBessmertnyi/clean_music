"""Сохранность синтетических файлов при отказах и конфликтах."""

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

import pipeline_utils as utils
from context import SessionContext
from file_state import FileRecord, OperationStatus, inspect_file
from interactor import ConsoleInteractor
from stages import process_rename_batch
from tag_repository import AudioTags, MetadataRead, TagRepository


class Interactor:
    def __init__(self, answer: str = '') -> None:
        self.answer = answer
        self.messages: list[str] = []
        self.prompts: list[str] = []

    def prompt(self, message: str, default: str = '') -> str:
        self.prompts.append(message)
        return self.answer

    def display(self, message: str) -> None:
        self.messages.append(message)

    def display_error(self, message: str) -> None:
        self.messages.append(message)


class MemoryRepository(TagRepository):
    """Только кэш и журнал вызовов; Mutagen и пользовательские файлы не используются."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[Path, str, str]] = []
        self.succeed = True
        self._disk_tags: dict[tuple[int, int], AudioTags] = {}

    def read_tags_uncached(self, filepath: Path) -> MetadataRead:
        try:
            stat = filepath.stat()
        except OSError as exc:
            return MetadataRead(None, error=str(exc))
        return MetadataRead(
            self._disk_tags.get((stat.st_dev, stat.st_ino), AudioTags('', ''))
        )

    def update_tags(self, filepath: Path, artist: str, title: str) -> bool:
        result = self.apply_tags(
            filepath,
            artist,
            title,
            simulated=False,
            publish=lambda a, t: self._write_to_disk(filepath, a, t),
        )
        return result.status in (OperationStatus.SUCCESS, OperationStatus.NOOP)

    def _write_to_disk(
        self,
        filepath: Path,
        artist: str | None,
        title: str | None,
        *,
        before_save: Callable[[], None] | None = None,
    ) -> tuple[OperationStatus, str]:
        before = self.get_tags(filepath).tags
        assert before is not None
        tags = AudioTags(
            before.artist if artist is None else artist,
            before.title if title is None else title,
        )
        if before_save is not None:
            before_save()
        self.writes.append((filepath, tags.artist, tags.title))
        if self.succeed:
            stat = filepath.stat()
            self._disk_tags[(stat.st_dev, stat.st_ino)] = tags
        return (
            OperationStatus.SUCCESS if self.succeed else OperationStatus.ERROR
        ), 'test write'


def admit(ctx: SessionContext, *paths: Path) -> FileRecord:
    first, *others = paths
    record = ctx.registry.register(first, identity=inspect_file(first))
    for path in others:
        ctx.registry.register(path, identity=inspect_file(path))
    return record


windows = pytest.mark.skipif(os.name != 'nt', reason='Реальная операция Windows/NTFS')


@pytest.mark.parametrize(
    ('answer', 'selected', 'error'),
    [
        ('2', [0, 2], False),
        ('1, 3', [1], False),
        ('1 2 3', [], False),
        ('', [0, 1, 2], False),
        ('Y', [0, 1, 2], False),
        ('N', [], False),
        ('ошибка', [], True),
        ('-1', [], True),
        ('0', [], True),
        ('4', [], True),
        ('1,4', [], True),
        ('1;2', [], True),
        ('1-3', [], True),
        ('1,ошибка', [], True),
        (',', [], True),
    ],
)
def test_select_indices(answer: str, selected: list[int], error: bool) -> None:
    ui = Interactor(answer)
    assert utils.select_indices(['a', 'b', 'c'], 'Выбор файлов', ui) == selected
    assert any('Группа пропущена' in message for message in ui.messages) == error


@pytest.mark.parametrize('answer', ['N', '1,ошибка', EOFError(), KeyboardInterrupt()])
def test_batch_rename_skips_files_when_group_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | BaseException
) -> None:
    reply = Mock(side_effect=[answer])
    monkeypatch.setattr('builtins.input', reply)
    rename = Mock()
    monkeypatch.setattr('stages._file_executor.rename', rename)
    files = [tmp_path / 'a.mp3', tmp_path / 'b.mp3']

    def run_batch() -> list[Path]:
        return process_rename_batch(
            files,
            files,
            str.upper,
            'файлы на переименование',
            SessionContext(False, False),
            MemoryRepository(),
            ConsoleInteractor(),
            'renamed_by_tags',
        )

    if isinstance(answer, BaseException):
        with pytest.raises(type(answer)):
            run_batch()
    else:
        assert run_batch() == files
    reply.assert_called_once()
    rename.assert_not_called()


def test_manual_filename_keeps_case_through_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = Mock(side_effect=['N', '3', 'McCartney', 'A'])
    monkeypatch.setattr('builtins.input', reply)
    ctx = SessionContext(False, False)
    ui = ConsoleInteractor()

    assert utils.sanitize_filename_interactive('A:B', 'old', ctx, ui) == 'SKIP'
    assert utils.sanitize_filename_interactive('A:B', 'old', ctx, ui) == 'McCartney'
    assert utils.sanitize_filename_interactive('A:B', 'old', ctx, ui) == 'AB'


def test_context_blocking(tmp_path: Path) -> None:
    path = tmp_path / 'Track.mp3'
    ctx = SessionContext(False, False)
    ctx.block_path(path, '')
    ctx.block_path(path, 'second')
    assert ctx.is_path_blocked(path)
    assert ctx.get_block_reason(path) == ''
    assert not SessionContext(False, False).is_path_blocked(path)
    assert ctx.is_path_blocked(path.with_name('track.mp3')) == (os.name == 'nt')


@windows
@pytest.mark.parametrize('old_name', ['01 title.mp3', 'Artist - Title.mp3'])
@pytest.mark.parametrize(
    'old_bytes,new_bytes', [(b'AAA', b'BBB'), (b'A', b'BBBB'), (b'AAAA', b'B')]
)
@pytest.mark.parametrize('dry_run', [False, True])
def test_conflict_preserves_bytes(
    tmp_path: Path, old_name: str, old_bytes: bytes, new_bytes: bytes, dry_run: bool
) -> None:
    old, new = tmp_path / old_name, tmp_path / 'target.mp3'
    old.write_bytes(old_bytes)
    new.write_bytes(new_bytes)
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    repo.simulate_update_tags(old, 'old', 'title')
    repo.simulate_update_tags(new, 'new', 'title')
    admit(ctx, old)
    status, result = utils.safe_rename(old, new, ctx, repo, ui)
    assert status == 'duplicate_exists'
    assert result == new
    assert old.read_bytes() == old_bytes and new.read_bytes() == new_bytes
    assert repo.get_tags(old).complete_tags == AudioTags('old', 'title')
    assert repo.get_tags(new).complete_tags == AudioTags('new', 'title')
    assert ctx.is_path_blocked(old) and not ctx.is_path_blocked(new)
    assert not ctx.get_stat('renamed_by_tags')


@windows
@pytest.mark.parametrize('new_name', ['track.mp3', 'Track.MP3', 'Other.mp3'])
def test_real_windows_rename(tmp_path: Path, new_name: str) -> None:
    old, new = tmp_path / 'Track.mp3', tmp_path / new_name
    old.write_bytes(b'audio bytes')
    temporary = tmp_path / f'{new.stem}__temp__{new.suffix}'
    temporary.write_bytes(b'occupied temporary')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    assert repo.update_tags(old, 'artist', 'title')
    admit(ctx, old)
    status, result = utils.simple_rename(old, new, ctx, repo, ui)
    assert status == 'renamed', ui.messages
    assert result == new
    actual_names = [path.name for path in tmp_path.iterdir()]
    assert new_name in actual_names
    assert 'Track.mp3' not in actual_names
    assert new.read_bytes() == b'audio bytes'
    assert temporary.read_bytes() == b'occupied temporary'
    assert repo.get_tags(new).complete_tags == AudioTags('artist', 'title')


@windows
def test_failed_rename_blocks_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.simulate_update_tags(old, 'artist', 'title')
    monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))
    admit(ctx, old)
    status, result = utils.safe_rename(old, new, ctx, repo, ui)
    assert status == 'skipped' and result is None
    assert (
        utils.handle_rename_result(status, old, result, ctx, 'renamed_by_tags') is None
    )
    assert old.read_bytes() == b'original' and not new.exists()
    assert repo.get_tags(old).complete_tags == AudioTags('artist', 'title')
    assert ctx.is_path_blocked(old) and ctx.is_path_blocked(new)
    assert ctx.get_stat('renamed_by_tags') == 0


@windows
def test_case_rename_without_actual_change_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / 'Track.mp3'
    old.write_bytes(b'original')
    new = tmp_path / 'track.mp3'
    ctx = SessionContext(False, False)
    monkeypatch.setattr(Path, 'rename', Mock(return_value=new))
    admit(ctx, old)
    ui = Interactor()
    status, result = utils.safe_rename(old, new, ctx, MemoryRepository(), ui)
    assert status == 'failed' and result is None
    assert ctx.is_path_blocked(old) and ctx.is_path_blocked(new)
    assert f'Фактическое местонахождение исходного файла: {old}' in ui.messages[-1]
    assert old.read_bytes() == b'original'


@windows
def test_target_appears_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_operations import FileExecutor

    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'source')
    original_rename = Path.rename
    calls = Mock()

    def competing_rename(source: Path, target: Path) -> Path:
        calls(source, target)
        with target.open('xb') as stream:
            stream.write(b'competitor')
        return original_rename(source, target)

    monkeypatch.setattr(Path, 'rename', competing_rename)
    ctx = SessionContext(False, False)
    record = admit(ctx, old)
    result = FileExecutor().rename(old, new, ctx, MemoryRepository(), Interactor())
    calls.assert_called_once_with(old, new)
    assert result.status is OperationStatus.CONFLICT
    assert result.path == new
    assert ctx.registry.id_for(old) == record.id
    assert ctx.registry.id_for(new) is None
    assert ctx.is_path_blocked(old) and not ctx.is_path_blocked(new)
    assert not ctx.get_stat('renamed_by_tags')
    assert old.read_bytes() == b'source' and new.read_bytes() == b'competitor'


@windows
def test_occupied_directory(tmp_path: Path) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'source')
    new.mkdir()
    ctx = SessionContext(False, False)
    admit(ctx, old)
    status, _ = utils.safe_rename(old, new, ctx, MemoryRepository(), Interactor())
    assert status == 'duplicate_exists'
    assert old.read_bytes() == b'source' and new.is_dir()


@windows
def test_hardlink_cannot_be_modified(tmp_path: Path) -> None:
    old, alias = tmp_path / 'old.mp3', tmp_path / 'alias.mp3'
    old.write_bytes(b'source')
    os.link(old, alias)
    assert not utils.modification_allowed(
        old, SessionContext(False, False), Interactor()
    )
    assert old.read_bytes() == alias.read_bytes() == b'source'


@windows
@pytest.mark.parametrize('dangling', [False, True])
def test_symlink_source_and_target(tmp_path: Path, dangling: bool) -> None:
    source, target, link = (
        tmp_path / 'source.mp3',
        tmp_path / 'target.mp3',
        tmp_path / 'link.mp3',
    )
    source.write_bytes(b'source')
    if not dangling:
        target.write_bytes(b'target')
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f'Создание symlink недоступно: {exc}')
    assert not utils.modification_allowed(
        link, SessionContext(False, False), Interactor()
    )
    ctx = SessionContext(False, False)
    admit(ctx, source)
    status, _ = utils.safe_rename(source, link, ctx, MemoryRepository(), Interactor())
    assert status == 'duplicate_exists'
    assert source.read_bytes() == b'source' and link.is_symlink()
    if not dangling:
        assert target.read_bytes() == b'target'


@windows
def test_reparse_attribute_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.mp3'
    source.write_bytes(b'source')
    original_lstat = os.lstat

    def reparse_lstat(path: str | Path, **kwargs: object) -> object:
        info = original_lstat(path)
        if Path(path) == tmp_path:
            return Mock(
                st_mode=info.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return info

    monkeypatch.setattr(os, 'lstat', reparse_lstat)
    assert not utils.modification_allowed(
        source, SessionContext(False, False), Interactor()
    )
    assert source.read_bytes() == b'source'


@windows
def test_real_junction_parent(tmp_path: Path) -> None:
    directory, junction = tmp_path / 'data', tmp_path / 'junction'
    directory.mkdir()
    source = directory / 'source.mp3'
    source.write_bytes(b'original')
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(junction), str(directory)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert junction.is_junction()
    assert not utils.modification_allowed(
        junction / source.name, SessionContext(False, False), Interactor()
    )
    assert source.read_bytes() == b'original'


@windows
def test_unconfirmed_volume_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'source.mp3'
    path.write_bytes(b'original')
    monkeypatch.setattr(utils, 'windows_local_ntfs', lambda path: False)
    ctx = SessionContext(False, False)
    status, _ = utils.safe_rename(
        path, tmp_path / 'new.mp3', ctx, MemoryRepository(), Interactor()
    )
    assert status == 'skipped' and ctx.is_path_blocked(path)
    assert path.read_bytes() == b'original'


@windows
def test_unchanged_and_dry_run_do_not_count_success(tmp_path: Path) -> None:
    path = tmp_path / 'source.mp3'
    path.write_bytes(b'original')
    repo, ui, ctx = MemoryRepository(), Interactor(), SessionContext(True, False)
    repo.simulate_update_tags(path, 'artist', 'title')
    admit(ctx, path)
    status, result = utils.safe_rename(path, path, ctx, repo, ui)
    assert status == 'unchanged'
    assert (
        utils.handle_rename_result(status, path, result, ctx, 'renamed_by_tags') == path
    )
    target = tmp_path / 'target.mp3'
    status, result = utils.safe_rename(path, target, ctx, repo, ui)
    assert status == 'planned'
    assert (
        utils.handle_rename_result(status, path, result, ctx, 'renamed_by_tags')
        == target
    )
    assert not ctx.get_stat('renamed_by_tags')
    assert path.read_bytes() == b'original' and not target.exists()


@pytest.mark.skipif(
    os.name == 'nt', reason='Отказ POSIX/WSL проверяется реальным POSIX Python'
)
def test_posix_refuses_modification(tmp_path: Path) -> None:
    old = tmp_path / 'old.mp3'
    old.write_bytes(b'source')
    ui, ctx = Interactor(), SessionContext(False, False)
    status, _ = utils.safe_rename(
        old, tmp_path / 'new.mp3', ctx, MemoryRepository(), ui
    )
    assert status == 'skipped' and ctx.is_path_blocked(old)
    assert utils.WSL_MODIFICATION_DISABLED in '\n'.join(ui.messages)
    assert old.read_bytes() == b'source'


@pytest.mark.parametrize('answer', ['', 'y', 'yes'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_candidates_are_information_only(
    tmp_path: Path, answer: str, dry_run: bool
) -> None:
    files = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    repo, ui, ctx = (
        MemoryRepository(),
        Interactor(answer),
        SessionContext(dry_run, False),
    )
    for path, data in zip(files, [b'AAA', b'B'], strict=True):
        path.write_bytes(data)
        repo.simulate_update_tags(path, 'artist', 'title')
    assert utils.process_duplicates(files, ctx, repo, ui) == files
    assert [path.read_bytes() for path in files] == [b'AAA', b'B']
    assert all(
        repo.get_tags(path).complete_tags == AudioTags('artist', 'title')
        for path in files
    )
    assert not ui.prompts and not ctx.get_stat('duplicates_removed')
    assert all(path.name in '\n'.join(ui.messages) for path in files)


def test_missing_success_path_is_not_counted(tmp_path: Path) -> None:
    ctx = SessionContext(False, False)
    assert (
        utils.handle_rename_result(
            'renamed', tmp_path / 'file.mp3', None, ctx, 'renamed_by_tags'
        )
        is None
    )
    assert ctx.is_path_blocked(tmp_path / 'file.mp3')
    assert not ctx.get_stat('renamed_by_tags')


@windows
def test_batch_conflict_remains_available_for_display(tmp_path: Path) -> None:
    old, new = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    old.write_bytes(b'source')
    new.write_bytes(b'target')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor()
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(new, 'Artist', 'Title')
    admit(ctx, old, new)
    result = process_rename_batch(
        [old, new],
        [old],
        lambda stem: 'new',
        'rename',
        ctx,
        repo,
        ui,
        'renamed_by_tags',
    )
    assert result == [old, new] and ctx.is_path_blocked(old)
    assert utils.find_duplicates(result, repo) == [[old, new]]
    assert old.read_bytes() == b'source' and new.read_bytes() == b'target'


@windows
def test_symlink_attributes_reject_source_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / 'source.mp3', tmp_path / 'target.mp3'
    source.write_bytes(b'source')
    target.write_bytes(b'target')
    original_lstat = os.lstat

    def link_lstat(path: str | Path) -> object:
        if Path(path) == target:
            return Mock(st_mode=stat.S_IFLNK, st_nlink=1, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(os, 'lstat', link_lstat)
    assert not utils.modification_allowed(
        target, SessionContext(False, False), Interactor()
    )
    ctx = SessionContext(False, False)
    admit(ctx, source)
    status, _ = utils.safe_rename(source, target, ctx, MemoryRepository(), Interactor())
    assert status == 'duplicate_exists'
    assert source.read_bytes() == b'source' and target.read_bytes() == b'target'
