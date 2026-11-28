"""Глубокий режим на синтетических MP3 без внешних кодеков."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest

import duplicate_removal
import main as cli
import pipeline_utils
from context import SessionContext
from duplicate_analysis import CandidateGroup
from file_state import FileRegistry, OperationStatus, inspect_file
from run_report import RunReport
from tag_repository import TagRepository
from tests.test_pipeline_utils import Interactor
from tests.test_tag_publication import windows
from tests.test_tag_repository import write_mp3
from user_settings import settings_path


def collection(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir()
    a = write_mp3(root / 'Artist - Song.mp3', artist='Artist', title='Song')
    b = root / 'Artist - Song (1).mp3'
    c = root / 'Artist - Song (2).mp3'
    shutil.copyfile(a, b)
    shutil.copyfile(a, c)
    return a, b, c


def run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
    *,
    dry_run: bool = False,
) -> tuple[Mock, str]:
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(sys, 'argv', ['main.py', *(['--dry-run'] if dry_run else [])])
    prompts = Mock(side_effect=answers)
    monkeypatch.setattr('builtins.input', prompts)
    logs = tmp_path / 'logs'
    before = set(logs.glob('*.log'))
    cli.main()
    [log] = set(logs.glob('*.log')) - before
    return prompts, log.read_text(encoding='utf-8')


@windows
def test_deep_cli_plan_apply_and_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / 'apply'
    a, b, c = collection(root)
    other = write_mp3(root / 'Other - Tune.mp3', artist='Other', title='Tune')
    first = write_mp3(
        root / 'Artist - Unclear (1).mp3', artist='Artist', title='Unclear'
    )
    second = root / 'Artist - Unclear (2).mp3'
    shutil.copyfile(first, second)
    plan = tmp_path / 'plan'
    shutil.copytree(root, plan)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    winner_id = inspect_file(a)

    prompts, planned = run_cli(
        tmp_path, monkeypatch, ['deep', 'run', str(plan)], dry_run=True
    )
    assert prompts.call_count == 3
    assert 'прогноз удаления' in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in plan.iterdir()} == before
    assert not settings_path().exists()

    prompts, applied = run_cli(tmp_path, monkeypatch, ['deep', 'run', str(root)])
    assert prompts.call_count == 3
    assert 'Точные дубли будут удалены безвозвратно' in capsys.readouterr().out
    assert not b.exists() and not c.exists()
    assert inspect_file(a) == winner_id
    assert {p: p.read_bytes() for p in (a, other, first, second)} == {
        p: before[p.name] for p in (a, other, first, second)
    }

    def deletions(log: str, result: str) -> set[str]:
        return {
            Path(op['path']).name
            for line in log.splitlines()
            if line.startswith('{')
            and (op := json.loads(line)).get('kind') == 'duplicate_delete'
            and op['result'] == result
        }

    assert (
        deletions(planned, 'ПРЕДЛОЖЕНИЕ')
        == deletions(applied, 'успех')
        == {b.name, c.name}
    )
    assert 'удалён как точная копия: 2' in applied
    prompts, _ = run_cli(tmp_path, monkeypatch, ['cancel'])
    assert prompts.call_count == 1
    assert 'Точные дубли будут удалены безвозвратно' in capsys.readouterr().out


@pytest.mark.parametrize('deep', [False, True])
def test_safe_or_wsl_never_enters_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deep: bool
) -> None:
    if deep and os.name == 'nt':
        pytest.skip('реальный запрет проверяется на монтировании WSL')
    root = tmp_path / 'music'
    paths = collection(root)
    before = {path.name: path.read_bytes() for path in paths}
    removal = Mock(side_effect=AssertionError('вызвано удаление'))
    monkeypatch.setattr(pipeline_utils, 'remove_exact_copies', removal)
    # Первый случай проверяет safe, второй — обычный deep на Windows-монте WSL.
    answers = ['deep', 'run', str(root), 'n'] if deep else ['', str(root), 'n']
    run_cli(tmp_path, monkeypatch, answers)
    assert not removal.called
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
    if deep:
        assert set(root.iterdir()) == set(paths)


def test_plan_keeps_unknown_bytes_and_updates_later_stages(tmp_path: Path) -> None:
    a, b, c = collection(tmp_path / 'music')
    paths = [a, b, c]
    ctx, repo, ui = (
        SessionContext(True, False, deep=True),
        TagRepository(),
        Interactor(),
    )
    ctx.registry.mark_content_modeled(ctx.registry.track(c).id)
    assert pipeline_utils.process_duplicates(paths, ctx, repo, ui) == paths
    assert any(
        'Будущие размер и байты неизвестны' in message for message in ui.messages
    )
    ctx = SessionContext(True, False, deep=True)
    assert pipeline_utils.process_duplicates(paths, ctx, repo, ui) == [a]
    assert ctx.registry.current_paths() == [a]
    assert ctx.registry.source_vacated(b)
    assert all(p.exists() for p in paths)


@windows
@pytest.mark.parametrize(
    'failure',
    ['replace', 'winner_changed', 'winner_missing', 'ads', 'existing_ads', 'access'],
)
def test_group_rechecks_before_any_deletion(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = collection(tmp_path / 'music')
    if failure == 'existing_ads':
        Path(f'{c}:unique').write_bytes(b'unique')
    data = a.read_bytes()
    winner_id, c_id = inspect_file(a), inspect_file(c)
    ctx, repo, ui = (
        SessionContext(False, False, deep=True),
        TagRepository(),
        Interactor(),
    )
    paths = cli.get_audio_files(a.parent, ctx, ui)
    assert paths is not None
    real_remove = duplicate_removal.remove_exact_copies
    original = tmp_path / 'original'

    def intervene(
        group: CandidateGroup,
        winner: Path,
        copies: tuple[Path, ...],
        expected: dict[Path, duplicate_removal.Stamp],
        registry: FileRegistry,
        tags: TagRepository,
    ) -> Generator[tuple[Path, OperationStatus, str]]:
        if failure == 'replace':
            c.rename(original)
            c.write_bytes(data)
        elif failure == 'winner_changed':
            a.write_bytes(data + b'changed')
        elif failure == 'winner_missing':
            a.rename(original)
        elif failure == 'ads':
            Path(f'{c}:unique').write_bytes(b'unique')
        if failure == 'access':
            with duplicate_removal.guarded_file(c, exclusive=True):
                yield from real_remove(group, winner, copies, expected, registry, tags)
        else:
            yield from real_remove(group, winner, copies, expected, registry, tags)

    monkeypatch.setattr(pipeline_utils, 'remove_exact_copies', intervene)
    pipeline_utils.process_duplicates(paths, ctx, repo, ui)
    assert ctx.get_stat('duplicates_removed') == 0
    assert b.read_bytes() == c.read_bytes() == data
    assert (inspect_file(c) != c_id) == (failure == 'replace')
    assert a.exists() == (failure != 'winner_missing')
    if a.exists():
        assert inspect_file(a) == winner_id
        assert a.read_bytes() == (
            data + b'changed' if failure == 'winner_changed' else data
        )
    if failure in ('ads', 'existing_ads'):
        assert Path(f'{c}:unique').read_bytes() == b'unique'
    if failure == 'existing_ads':
        assert any(
            'удаление дубля остановлено' in message and c.name in message
            for message in ui.messages
        )
    assert any('обработка группы остановлена' in message for message in ui.messages), (
        ui.messages
    )


@windows
def test_partial_report_after_unconfirmed_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = collection(tmp_path / 'music')
    d = a.with_stem('Artist - Song (3)')
    shutil.copyfile(a, d)
    data, winner = a.read_bytes(), inspect_file(a)
    ctx, repo, ui = (
        SessionContext(False, False, deep=True),
        TagRepository(),
        Interactor(),
    )
    for path in (a, b, c, d):
        ctx.registry.register(path, identity=inspect_file(path))
    verify = duplicate_removal.require_path_binding

    def lose_winner_check(path: Path, handle: int, *, directory: bool = False) -> None:
        if path == a and not c.exists():
            raise PermissionError('управляемая ошибка сверки после удаления C')
        verify(path, handle, directory=directory)

    monkeypatch.setattr(duplicate_removal, 'require_path_binding', lose_winner_check)
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(a.parent)
        pipeline_utils.process_duplicates([a, b, c, d], ctx, repo, ui)
    assert not b.exists() and not c.exists()
    assert inspect_file(a) == winner and a.read_bytes() == data
    assert d.read_bytes() == data
    assert ctx.get_stat('duplicates_removed') == 1
    assert ctx.is_path_blocked(c)
    assert report.counts['удалён как точная копия'] == 1
    assert report.counts['результат удаления неопределён'] == 1
    assert [
        (Path(op.path).name, op.result)
        for op in report.operations
        if op.kind == 'duplicate_delete'
    ] == [(b.name, 'успех'), (c.name, 'неопределённость'), (d.name, 'отказ')]


@windows
def test_competing_process_cannot_write_or_rename_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = collection(tmp_path / 'music')
    original = inspect_file(a), a.read_bytes()
    ctx, repo, ui = (
        SessionContext(False, False, deep=True),
        TagRepository(),
        Interactor(),
    )
    paths = [a, b, c]
    for path in paths:
        ctx.registry.register(path, identity=inspect_file(path))
    script = """
from pathlib import Path
import sys
from tag_ntfs import _close, _open
p = Path(sys.argv[1])
print('ready', flush=True)
assert input() == 'go'
for operation in (
    lambda: _close(_open(p, 0x40000000, 7)),
    lambda: p.rename(p.with_suffix('.moved')),
):
    try:
        operation()
    except OSError as exc:
        print(exc.winerror)
    else:
        print('unexpected success')
"""
    delete = duplicate_removal.delete_open_file

    with subprocess.Popen(
        [sys.executable, '-c', script, str(a)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == 'ready'

        def compete(handle: int) -> None:
            if child.poll() is None:
                output, _ = child.communicate('go\n', timeout=15)
                assert child.returncode == 0 and output.splitlines() == ['32', '32']
            delete(handle)

        monkeypatch.setattr(duplicate_removal, 'delete_open_file', compete)
        assert pipeline_utils.process_duplicates(paths, ctx, repo, ui) == [a], (
            ui.messages
        )
    assert (inspect_file(a), a.read_bytes()) == original
    assert not b.exists() and not c.exists()
