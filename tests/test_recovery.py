import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import main as cli
import recovery_store
from context import SessionContext
from file_state import inspect_file
from interactor import ConsoleInteractor
from pipeline_utils import safe_rename
from recovery import Fingerprint, RenameIntent, recover_renames
from recovery_store import DIRECTORY_NAME, RecoveryStore
from run_report import RunReport
from tag_repository import TagRepository

WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='системный rename Windows/NTFS')
ORIGINAL = b'synthetic original'


def _rename_in_worker(root: Path, point: str, target_name: str, signal: Path) -> None:
    source, target = root / 'Track.mp3', root / target_name
    ctx = SessionContext(False, False, target_dir=root)
    ctx.registry.register(source, identity=inspect_file(source))
    write_new = recovery_store._write_new
    write_record = RecoveryStore.write
    rename = Path.rename

    def pause() -> None:
        signal.write_bytes(b'ready')
        while True:
            time.sleep(1)

    def before_publish(path: Path, data: bytes) -> None:
        write_new(path, data)
        if point == 'before_intent' and path.suffix == '.pending':
            pause()

    def after_publish(
        store: RecoveryStore, path: Path, data: dict[str, object], *, initial: bool
    ) -> None:
        write_record(store, path, data, initial=initial)
        if point == ('after_intent' if initial else 'after_completion'):
            pause()

    def after_rename(path: Path, destination: Path) -> Path:
        result = rename(path, destination)
        if point == 'after_rename':
            pause()
        return result

    with (
        patch.object(recovery_store, '_write_new', before_publish),
        patch.object(RecoveryStore, 'write', after_publish),
        patch.object(Path, 'rename', after_rename),
    ):
        safe_rename(source, target, ctx, TagRepository(), ConsoleInteractor())
    raise RuntimeError(f'контрольная точка не достигнута: {point}')


def _worker_command(*args: object) -> list[str]:
    return [sys.executable, '-X', 'utf8', '-m', 'tests.test_recovery', *map(str, args)]


def _recover_in_worker(root: Path) -> None:
    ctx = SessionContext(False, False)
    try:
        store = RecoveryStore(root)
        with store.session():
            recover_renames(store, ctx, ConsoleInteractor())
    except OSError as exc:
        raise SystemExit(str(exc)) from exc
    sys.stdout.write(json.dumps(sorted(path.name for path in ctx.blocked_paths)) + '\n')


@WINDOWS
@pytest.mark.parametrize(
    ('point', 'target', 'foreign_source', 'expected'),
    [
        ('before_intent', 'New.mp3', False, 'Track.mp3'),
        ('after_intent', 'New.mp3', False, 'Track.mp3'),
        ('after_rename', 'New.mp3', False, 'New.mp3'),
        ('after_rename', 'New.mp3', True, 'New.mp3'),
        ('after_rename', 'track.mp3', False, 'track.mp3'),
        ('after_completion', 'New.mp3', False, 'New.mp3'),
    ],
)
def test_process_stop_and_two_recoveries(
    tmp_path: Path, point: str, target: str, foreign_source: bool, expected: str
) -> None:
    root = tmp_path / 'audio'
    root.mkdir()
    source = root / 'Track.mp3'
    source.write_bytes(ORIGINAL)
    identity = inspect_file(source)
    signal = tmp_path / 'ready'
    with subprocess.Popen(
        _worker_command('rename', root, point, target, signal),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while (
                not signal.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            if not signal.exists():
                if process.poll() is None:
                    process.kill()
                output, _ = process.communicate(timeout=5)
                pytest.fail(output.decode('utf-8', errors='replace'))
            if point == 'after_intent':
                competing = subprocess.run(
                    _worker_command('recover', root),
                    capture_output=True,
                    encoding='utf-8',
                    timeout=15,
                    check=False,
                )
                assert competing.returncode == 1
                assert 'блокировку' in competing.stderr
                assert source.read_bytes() == ORIGINAL
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    if foreign_source:
        source.write_bytes(b'foreign bytes')
    store = RecoveryStore(root, diagnostic=True)
    with store.session():
        records = store.records()
        if point == 'before_intent':
            assert not records
            assert len(list(store.directory.glob('*.pending'))) == 1
        else:
            (record,) = records
            assert store.read(record)['outcome'] == (
                'renamed' if point == 'after_completion' else None
            )

    for attempt in range(2):
        result = subprocess.run(
            _worker_command('recover', root),
            capture_output=True,
            encoding='utf-8',
            timeout=15,
            check=True,
        )
        assert json.loads(result.stdout.splitlines()[-1]) == []
        assert ('Восстановление rename' in result.stdout) == (
            attempt == 0 and bool(records)
        )
        if attempt == 0 and records:
            assert f'{expected}: подтверждён' in result.stdout
            if foreign_source:
                assert 'Track.mp3: другой объект' in result.stdout
        assert (root / expected).read_bytes() == ORIGINAL
        assert inspect_file(root / expected) == identity
        names = {path.name for path in root.iterdir() if path.is_file()}
        assert names == ({expected, source.name} if foreign_source else {expected})
        if foreign_source:
            assert source.read_bytes() == b'foreign bytes'
        with store.session():
            assert not store.records()


def _diagnostic_record(tmp_path: Path, source: Path, target: Path) -> Path:
    store = RecoveryStore(tmp_path, diagnostic=True)
    store.directory.mkdir()
    (store.directory / 'owner').write_bytes(recovery_store._MARKER)
    record = store.directory / ('a' * 32 + '.json')
    intent = RenameIntent(
        source.name, target.name, Fingerprint.capture(source, inspect_file(source))
    )
    data = intent.encode(store, record, None)
    digest = hashlib.sha256(recovery_store._encode(data)).hexdigest()
    record.write_bytes(recovery_store._encode({'data': data, 'sha256': digest}))
    return record


def test_read_only_diagnosis_preserves_every_file(tmp_path: Path) -> None:
    source, target = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    source.write_bytes(ORIGINAL)
    record = _diagnostic_record(tmp_path, source, target)
    source.write_bytes(b'externally edited')
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    for _ in range(2):
        ctx = SessionContext(True, False)
        store = RecoveryStore(tmp_path, diagnostic=True)
        with store.session():
            recover_renames(store, ctx, ConsoleInteractor(), diagnostic=True)
        assert ctx.is_path_blocked(source) and ctx.is_path_blocked(target)
        assert record.exists()
        assert {
            path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()
        } == before


@WINDOWS
def test_inconclusive_record_blocks_paths_again_on_second_run(tmp_path: Path) -> None:
    source, target = tmp_path / 'old.mp3', tmp_path / 'new.mp3'
    source.write_bytes(ORIGINAL)
    record = _diagnostic_record(tmp_path, source, target)
    original_record = record.read_bytes()
    source.rename(target)
    target.write_bytes(b'externally edited')
    for _ in range(2):
        ctx = SessionContext(False, False)
        store = RecoveryStore(tmp_path)
        with store.session():
            recover_renames(store, ctx, ConsoleInteractor())
        assert ctx.is_path_blocked(source) and ctx.is_path_blocked(target)
        assert not ctx.is_path_blocked(tmp_path / 'unrelated.mp3')
        assert record.read_bytes() == original_record
        assert target.read_bytes() == b'externally edited' and not source.exists()


@WINDOWS
def test_cli_holds_lock_through_final_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    root.mkdir()
    finish = RunReport.finish
    checked = False

    def check_lock(report: RunReport) -> None:
        nonlocal checked
        with pytest.raises(OSError, match='блокировку'), RecoveryStore(root).session():
            pytest.fail('блокировка освобождена до завершения отчёта')
        checked = True
        finish(report)

    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(cli, 'get_valid_path', lambda: root)
    monkeypatch.setattr(sys, 'argv', ['main.py'])
    monkeypatch.setattr(RunReport, 'finish', check_lock)
    cli.main()
    assert checked
    with RecoveryStore(root).session():
        pass


@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('foreign', [True, False])
def test_cli_stops_before_scan_if_scope_unknown(
    tmp_path: Path, foreign: bool, dry_run: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    root.mkdir()
    source = root / 'old.mp3'
    source.write_bytes(ORIGINAL)
    if foreign:
        directory = root / DIRECTORY_NAME
        directory.mkdir()
        (directory / 'user-data').write_bytes(b'preserve')
    else:
        record = _diagnostic_record(root, source, root / 'new.mp3')
        record.write_bytes(b'{corrupt')
    before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(cli, 'get_valid_path', lambda: root)
    monkeypatch.setattr(
        sys, 'argv', ['main.py', '--dry-run'] if dry_run else ['main.py']
    )
    monkeypatch.setattr(
        cli, 'get_audio_files', lambda *_: pytest.fail('сканирование началось')
    )
    cli.main()
    after = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    if os.name == 'nt' and not (foreign or dry_run):
        assert after.pop(root / DIRECTORY_NAME / 'lock') == b''
    assert after == before


if __name__ == '__main__':
    if sys.argv[1] == 'rename':
        _rename_in_worker(
            Path(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5])
        )
    else:
        _recover_in_worker(Path(sys.argv[2]))
