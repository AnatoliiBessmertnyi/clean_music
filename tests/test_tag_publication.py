"""Публикация на Windows/NTFS через штатный файловый исполнитель."""

import errno
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Never
from unittest.mock import patch

import pytest
from mutagen.id3 import ID3, TALB

import tag_publication
from audio_verification import find_ffprobe
from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationStatus, inspect_file
from recovery import recover_renames
from recovery_store import DIRECTORY_NAME, RecoveryStore
from tag_repository import AudioTags, TagRepository
from tests.test_pipeline_utils import Interactor
from tests.test_tag_repository import write_mp3

windows = pytest.mark.skipif(os.name != 'nt', reason='требуется Windows/NTFS')


def synthetic_audio(
    path: Path, *, codec: str = 'libmp3lame', artist: str = 'Artist', title: str = 'Old'
) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None or shutil.which('ffprobe') is None:
        pytest.skip('требуются ffmpeg и ffprobe')
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
            codec,
            '-metadata',
            f'artist={artist}',
            '-metadata',
            f'title={title}',
            str(path),
        ],
        check=True,
    )


@windows
def test_write_then_rename(tmp_path: Path) -> None:
    path = tmp_path / 'Old.mp3'
    synthetic_audio(path)
    original = inspect_file(path)
    ctx = SessionContext(False, False, target_dir=tmp_path)
    record = ctx.registry.register(path, identity=original)
    repo = TagRepository()
    assert repo.get_tags(path).tags == AudioTags('Artist', 'Old')
    ui = Interactor()
    store = RecoveryStore(tmp_path)
    with store.session():
        ctx.recovery = store
        executor = FileExecutor()
        result = executor.write_tags(path, 'Artist', 'New', ctx, repo, ui)
        assert result.status is OperationStatus.SUCCESS, ui.messages
        assert ctx.registry.id_for(path) == record.id
        assert (
            ctx.registry.record_of(record.id).identity == inspect_file(path) != original
        )
        assert repo.get_tags(path).tags == AudioTags('Artist', 'New')
        assert not store.records()
        target = tmp_path / 'Artist - New.mp3'
        renamed = executor.rename(path, target, ctx, repo, ui)
        assert renamed.status is OperationStatus.SUCCESS, ui.messages
        assert ctx.registry.id_for(target) == record.id


@windows
@pytest.mark.parametrize(
    ('extension', 'codec'),
    [
        ('m4a', 'aac'),
        ('mp4', 'aac'),
        ('flac', 'flac'),
        ('ogg', 'libvorbis'),
        ('wma', 'wmav2'),
    ],
)
def test_format_writes_through_executor(
    tmp_path: Path, extension: str, codec: str
) -> None:
    path = tmp_path / f'Track.{extension}'
    synthetic_audio(path, codec=codec)
    store = RecoveryStore(tmp_path)
    ctx = SessionContext(False, False, target_dir=tmp_path, recovery=store)
    record = ctx.registry.register(path, identity=inspect_file(path))
    executor, repo, ui = FileExecutor(), TagRepository(), Interactor()
    with store.session():
        for title in ('First', 'Second'):
            assert executor.write_tags(
                path, 'Artist', title, ctx, repo, ui
            ).is_confirmed_change, ui.messages
            assert repo.get_tags(path).tags == AudioTags('Artist', title)
            assert ctx.registry.id_for(path) == record.id
            assert not store.records()


def test_noop_and_dry_run_do_not_need_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_mp3(tmp_path / 'Track.mp3', artist='Artist', title='Old')
    original = path.read_bytes()
    repo = TagRepository()
    ui = Interactor()
    ctx = SessionContext(False, False)
    ctx.registry.register(path, identity=inspect_file(path))
    monkeypatch.setattr('file_operations.modification_allowed', lambda *_: True)

    def missing() -> str:
        raise OSError('ffprobe missing')

    monkeypatch.setattr('file_operations.find_ffprobe', missing)
    executor = FileExecutor()
    assert (
        executor.write_tags(path, 'Artist', 'Old', ctx, repo, ui).status
        is OperationStatus.NOOP
    )
    assert (
        executor.write_tags(path, 'Artist', 'New', ctx, repo, ui).status
        is OperationStatus.ERROR
    )
    assert ui.messages == ['ffprobe missing']
    assert repo.get_tags(path).tags == AudioTags('Artist', 'Old')
    dry = SessionContext(True, False)
    result = executor.write_tags(path, 'Artist', 'New', dry, repo, ui)
    assert result.status is OperationStatus.SIMULATED
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@windows
@pytest.mark.parametrize(
    'point', ['publishing', 'between', 'published', 'confirmed', 'cleaned', 'conflict']
)
def test_stopped_process_and_two_recoveries(tmp_path: Path, point: str) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Track.mp3'
    synthetic_audio(source)
    original = source.read_bytes()
    original_id = inspect_file(source)
    worker = subprocess.run(
        [
            sys.executable,
            '-X',
            'utf8',
            '-m',
            'tests.test_tag_publication',
            str(root),
            point,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert worker.returncode == 23, worker.stdout + worker.stderr
    directory = root / DIRECTORY_NAME
    assert len(list(directory.glob('*.json'))) == 1
    work = list(directory.glob('*.work.mp3'))
    original_entries = list(directory.glob('*.original'))
    assert bool(work) == (point in ('publishing', 'between', 'conflict'))
    assert bool(original_entries) == (
        point in ('between', 'published', 'confirmed', 'conflict')
    )
    if original_entries:
        assert original_entries[0].read_bytes() == original
        assert inspect_file(original_entries[0]) == original_id
    assert source.exists() == (point not in ('between', 'conflict'))
    if point == 'conflict':
        source.write_bytes(b'foreign')
    for _ in range(2):
        ctx = SessionContext(False, False)
        store = RecoveryStore(root)
        with store.session():
            recover_renames(store, ctx, Interactor())
            assert bool(store.records()) == (point == 'conflict')
        assert ctx.is_path_blocked(source) == (point == 'conflict')
        if point == 'conflict':
            assert source.read_bytes() == b'foreign'
            assert original_entries[0].read_bytes() == original and work[0].exists()
        elif point in ('publishing', 'between'):
            assert source.read_bytes() == original
            assert inspect_file(source) == original_id
        else:
            assert TagRepository().get_tags(source).tags == AudioTags('Artist', 'New')
            assert inspect_file(source) != original_id


def stop_worker(root: Path, point: str) -> None:
    source = root / 'Track.mp3'
    store = RecoveryStore(root)
    move = tag_publication.move_open_file
    delete = tag_publication.delete_open_file
    log = RecoveryStore.write

    def write(
        self: RecoveryStore, path: Path, data: dict[str, object], *, initial: bool
    ) -> None:
        log(self, path, data, initial=initial)
        if str(data.get('phase')).lower() == point:
            os._exit(23)

    def rename(handle: int, target: Path) -> None:
        move(handle, target)
        if (target == source and point == 'published') or (
            target.suffix == '.original' and point in ('between', 'conflict')
        ):
            os._exit(23)

    def remove(handle: int) -> None:
        delete(handle)
        if point == 'cleaned':
            os._exit(23)

    with (
        patch.object(RecoveryStore, 'write', write),
        patch.object(tag_publication, 'move_open_file', rename),
        patch.object(tag_publication, 'delete_open_file', remove),
        store.session(),
    ):
        tag_publication.publish_tags(
            store,
            source,
            inspect_file(source),
            TagRepository(),
            find_ffprobe(),
            None,
            'New',
        )


@windows
@pytest.mark.parametrize(
    'fault', ['copy', 'save', 'tags', 'audio', 'publish', 'conflict', 'cleanup']
)
def test_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source = tmp_path / 'Track.mp3'
    synthetic_audio(source)
    original = source.read_bytes()
    identity = inspect_file(source)
    store = RecoveryStore(tmp_path)
    ctx = SessionContext(False, False, target_dir=tmp_path, recovery=store)
    file_id = ctx.registry.register(source, identity=identity).id
    repo, ui = TagRepository(), Interactor()

    def fail(*args: object, **kwargs: object) -> Never:
        raise OSError(fault)

    if fault == 'copy':

        def partial_copy(src: BinaryIO, dst: BinaryIO, **kwargs: object) -> Never:
            dst.write(src.read(32))
            raise OSError(errno.ENOSPC, 'недостаточно места')

        monkeypatch.setattr(shutil, 'copyfileobj', partial_copy)
    elif fault == 'save':

        def partial_save(
            path: Path, stream: BinaryIO, artist: str | None, title: str | None
        ) -> Never:
            stream.write(b'partial tag data')
            raise OSError('Mutagen save failed')

        monkeypatch.setattr(repo, 'save_work_tags', partial_save)
    elif fault == 'tags':
        save = repo.save_work_tags

        def spoil_tags(
            path: Path, stream: BinaryIO, artist: str | None, title: str | None
        ) -> None:
            save(path, stream, artist, title)
            stream.seek(0)
            tags = ID3(stream)
            tags.add(TALB(encoding=3, text=['Unexpected album']))
            stream.seek(0)
            tags.save(stream)

        monkeypatch.setattr(repo, 'save_work_tags', spoil_tags)
    elif fault == 'audio':
        signature = tag_publication.audio_signature

        def altered(command: str, path: Path) -> tuple[str, str]:
            result = signature(command, path)
            return ('mismatch', result[1]) if path != source else result

        monkeypatch.setattr(tag_publication, 'audio_signature', altered)
    elif fault in ('publish', 'conflict'):
        move = tag_publication.move_open_file

        def fail_publish(handle: int, target: Path) -> None:
            if target == source and fault == 'publish':
                fail()
            move(handle, target)
            if target.suffix == '.original' and fault == 'conflict':
                source.write_bytes(b'foreign')

        monkeypatch.setattr(tag_publication, 'move_open_file', fail_publish)
    else:
        monkeypatch.setattr(tag_publication, 'delete_open_file', fail)

    with store.session():
        outcome = FileExecutor().write_tags(source, 'Artist', 'New', ctx, repo, ui)
        if fault == 'cleanup':
            assert outcome.status is OperationStatus.SUCCESS and ctx.is_path_blocked(
                source
            )
            assert (
                ctx.registry.record_of(file_id).identity
                == inspect_file(source)
                != identity
            )
            assert repo.get_tags(source).tags == AudioTags('Artist', 'New')
            assert len(store.records()) == 1
        elif fault == 'conflict':
            assert (
                outcome.status is OperationStatus.UNCERTAIN
                and source.read_bytes() == b'foreign'
            )
            assert next(store.directory.glob('*.original')).read_bytes() == original
        else:
            assert outcome.status in (OperationStatus.ERROR, OperationStatus.UNCERTAIN)
            path = (
                next(store.directory.glob('*.original'))
                if fault == 'publish'
                else source
            )
            assert path.read_bytes() == original and inspect_file(path) == identity
            if fault != 'publish':
                assert not store.records()


@pytest.mark.parametrize('dry_run', [False, True])
def test_cli_write_then_rename_or_read_only(tmp_path: Path, dry_run: bool) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Artist - Filename.mp3'
    synthetic_audio(source)
    with source.open('r+b') as stream:
        # Пустой исполнитель, заполненное название: этап 4 запишет первое,
        # затем переименует файл по полному набору тегов.
        TagRepository().save_work_tags(source, stream, '', 'New')
    original = source.read_bytes()
    argv = [
        sys.executable,
        '-X',
        'utf8',
        '-c',
        'import main, sys; main.__file__ = sys.argv[1]; main.main()',
        str(tmp_path / 'launcher.py'),
    ]
    if dry_run:
        argv.append('--dry-run')
    result = subprocess.run(
        argv,
        input=f'\n{root}\ny\nn\nn\n',
        capture_output=True,
        text=True,
        encoding='utf-8',
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if os.name == 'nt' and not dry_run:
        target = root / 'Artist - New.mp3'
        assert not source.exists(), result.stdout
        assert TagRepository().get_tags(target).tags == AudioTags('Artist', 'New')
        assert not list((root / DIRECTORY_NAME).glob('*.json'))
    else:
        assert source.read_bytes() == original
        assert list(root.iterdir()) == [source]


@windows
def test_ntfs_refuses_alternate_stream(tmp_path: Path) -> None:
    source = tmp_path / 'Track.mp3'
    synthetic_audio(source)
    ads = Path(f'{source}:synthetic')
    ads.write_bytes(b'preserve me')
    store = RecoveryStore(tmp_path)
    ctx = SessionContext(False, False, target_dir=tmp_path, recovery=store)
    ctx.registry.register(source, identity=inspect_file(source))
    with store.session():
        outcome = FileExecutor().write_tags(
            source, 'Artist', 'New', ctx, TagRepository(), Interactor()
        )
        assert outcome.status is OperationStatus.ERROR
        assert 'именованный поток' in outcome.reason
        assert ads.read_bytes() == b'preserve me'
        assert not store.records()


@windows
def test_external_edit_before_publication_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'Track.mp3'
    synthetic_audio(source)
    identity = inspect_file(source)
    original = source.read_bytes()
    store = RecoveryStore(tmp_path)
    ctx = SessionContext(False, False, target_dir=tmp_path, recovery=store)
    ctx.registry.register(source, identity=identity)
    original_guard = tag_publication.guarded_file
    edited = False

    @contextmanager
    def guard(
        path: Path, *, delete: bool = False, write: bool = False
    ) -> Iterator[tuple[int, BinaryIO]]:
        nonlocal edited
        with original_guard(path, delete=delete, write=write) as owned:
            yield owned
        if path == source and not edited and not delete:
            with source.open('ab') as external:
                external.write(b'external edit')
            edited = True

    monkeypatch.setattr(tag_publication, 'guarded_file', guard)
    with store.session():
        result = FileExecutor().write_tags(
            source, 'Artist', 'New', ctx, TagRepository(), Interactor()
        )
        assert result.status is OperationStatus.UNCERTAIN
        assert source.read_bytes() == original + b'external edit'
        assert inspect_file(source) == identity
        assert not list(store.directory.glob('*.original'))
        assert len(list(store.directory.glob('*.work.mp3'))) == 1
        assert len(store.records()) == 1


if __name__ == '__main__':
    stop_worker(Path(sys.argv[1]), sys.argv[2])
